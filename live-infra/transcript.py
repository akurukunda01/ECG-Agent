
import os
import json
import subprocess
from datetime import datetime

TRANSCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcripts")
RULE = "─" * 78


def loop_commit():
    """Short hash of the checked-out commit, or 'unknown' outside a git checkout."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=os.path.dirname(os.path.abspath(__file__)),
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


class Transcript:
    def __init__(self, base_model_path, adapter_path, ecg_handle, generation_config,
                 system_prompt, directory=TRANSCRIPT_DIR):
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        adapter_name = os.path.basename(os.path.normpath(adapter_path))
        self.path = os.path.join(directory, f"{stamp}_{adapter_name}")
        self.txt = open(self.path + ".txt", "a", encoding="utf-8")
        self.jsonl = open(self.path + ".jsonl", "a", encoding="utf-8")
        header = {
            "type": "header",
            "time": datetime.now().isoformat(timespec="seconds"),
            "base_model_path": base_model_path,
            "adapter_path": adapter_path,
            "ecg": ecg_handle,
            "generation_config": generation_config.to_dict(),
            "loop_commit": loop_commit(),
            "system_prompt": system_prompt,
        }
        self._write(header, render_header(header))

    def write_turn(self, turn, user_text, ecg_handle, events, messages_added,
                   elapsed, history_before, history_after, error=None):
        record = {
            "type": "turn",
            "turn": turn,
            "time": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": round(elapsed, 2),
            "ecg": ecg_handle,
            "user": user_text,
            "history_before": history_before,
            "history_after": history_after,
            "messages_added": messages_added,
            "events": [{"type": e.type, "value": e.value} for e in events],
            "error": error,
        }
        self._write(record, render_turn(record))

    def _write(self, record, text):
        self.jsonl.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.jsonl.flush()
        self.txt.write(text)
        self.txt.flush()

    def close(self):
        self.txt.close()
        self.jsonl.close()


def render_header(h):
    lines = [
        RULE,
        f"ECG-Agent live session  {h['time']}   loop.py @ {h['loop_commit']}",
        f"base model : {h['base_model_path']}",
        f"adapter    : {h['adapter_path']}",
        f"ecg        : {h['ecg']}",
        f"generation : {json.dumps(h['generation_config'])}",
        RULE,
        "SYSTEM PROMPT",
        h["system_prompt"].strip(),
        "",
    ]
    return "\n".join(lines) + "\n"


def indent(text, prefix="    | "):
    return "\n".join(prefix + line for line in text.splitlines())


def render_turn(r):
    events = r["events"]
    generations = [e["value"] for e in events if e["type"] == "generation"]
    tool = {e["type"]: e["value"] for e in events
            if e["type"] in ("tool_call", "tool_return", "observation_rendered")}
    assistant_messages = [m["content"] for m in r["messages_added"] if m["role"] == "assistant"]

    out = [
        RULE,
        f"turn {r['turn']} · {r['ecg']} · {r['elapsed_s']} s · "
        f"history {r['history_before']} → {r['history_after']}",
        "",
        "USER",
        r["user"],
        "",
    ]
    for i, raw in enumerate(generations):
        if i < len(assistant_messages):
            out += ["ASSISTANT", assistant_messages[i]]
        else:
            out += ["ASSISTANT (not added to history)"]
        out += ["  model wrote:", indent(raw)]
        if i == 0 and tool:
            out += [f"  tool returned:  {tool.get('tool_return')}",
                    f"  shown to model: {tool.get('observation_rendered')}"]
        out.append("")
    if r["error"]:
        out += ["ERROR", r["error"],
                f"history rolled back to {r['history_after']} messages", ""]
    return "\n".join(out) + "\n"
