# Place this at the top with your other imports
from transformers import LlamaTokenizerFast
import os
import sys
import json
import torch
import argparse
import re
import pandas as pd
from datetime import datetime
from tqdm import tqdm
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig
from peft import PeftModel # <<< NEW: Import PeftModel
sys.path.insert(0, os.getcwd())
from medrax.tools.classification import ECGClassifierTool, ECGAnalysisTool
from events import Event
from loop import ECG_EVALUATION_PROMPT, load_model_and_tokenizer, load_ground_truth_data, get_live_tool_output, parse_generated_response, generate_full_response, format_assistant_turn_for_messages, run_user_turn, make_generation_config

def normalize_ecg_filename(name):
    """Canonicalize ECG filenames so padded and unpadded ids join reliably.
    The HF dataset zero-pads record ids ('HR00056.mat') while the committed
    summary CSVs use unpadded ids ('HR56.mat'); the exact-match join in
    get_precomputed_tool_output silently misses on ~45% of records without
    this."""
    m = re.match(r'^HR0*(\d+)(?:\.mat)?$', str(name).strip())
    return f"HR{m.group(1)}.mat" if m else str(name).strip()

def build_aligned_turns(gt_dialogue, gen_dialogue):
    """
    Create a per-turn alignment of assistant turns between GT and Generated dialogues.
    Also attaches the most recent user message before each assistant turn (from GT).
    Returns: (aligned_turns, num_gt_assistant, num_gen_assistant)
    """
    gt_assistant_turns = [t for t in gt_dialogue if t.get("role") == "assistant"]
    gen_assistant_turns = [t for t in gen_dialogue if t.get("role") == "assistant"]

    # Track the latest user message before each assistant turn (from GT)
    user_contexts = []
    last_user_content = None
    for t in gt_dialogue:
        if t.get("role") == "user":
            last_user_content = t.get("content", "")
        elif t.get("role") == "assistant":
            user_contexts.append(last_user_content)

    n_max = max(len(gt_assistant_turns), len(gen_assistant_turns))
    aligned = []
    for i in range(n_max):
        gt_t = gt_assistant_turns[i] if i < len(gt_assistant_turns) else {}
        gen_t = gen_assistant_turns[i] if i < len(gen_assistant_turns) else {}

        gt_action = gt_t.get("action")
        gen_action = gen_t.get("action")
        gt_content = gt_t.get("content")
        gen_content = gen_t.get("content")

        # Convenience flags for quick metric scripts
        action_correct = (gt_action is not None and gen_action is not None and str(gt_action).strip() == str(gen_action).strip())
        response_exact_match = (gt_content is not None and gen_content is not None and str(gt_content).strip() == str(gen_content).strip())

        aligned.append({
            "turn_index": i,
            "user_content": user_contexts[i] if i < len(user_contexts) else None,

            "gt_action": gt_action,
            "gt_thought": gt_t.get("thought"),
            "gt_content": gt_content,
            "gt_tool_output": gt_t.get("tool_output"),

            "gen_action": gen_action,
            "gen_thought": gen_t.get("thought"),
            "gen_content": gen_content,
            "gen_tool_output": gen_t.get("tool_output"),

            "action_correct": action_correct,
            "response_exact_match": response_exact_match,
        })

    return aligned, len(gt_assistant_turns), len(gen_assistant_turns)

# ### MAIN ###
# <<< CHANGED: Main function signature updated >>>
def run_inference_on_test_set(base_model_path, adapter_path, output_file=None, max_samples=None, inference_mode='without_gt', filter_action=None, resume=False):
    # <<< CHANGED: Pass the new paths to the loading function >>>
    model, tokenizer = load_model_and_tokenizer(base_model_path, adapter_path)
    gt_data = load_ground_truth_data()
    sample_events = []
    emit = lambda event: sample_events.append(event)
    tools = lambda action, ecg_handle: get_live_tool_output(action, ecg_handle, gt_data, emit)

    generation_config = make_generation_config(tokenizer)

    dataset = load_dataset("gustmd0121/12-lead-ecg-mtd-dataset")['test']
    if filter_action:
        print(f"Filtering dataset to only include samples with the action: '{filter_action}'")
        
        def contains_action(example):
            try:
                # Load the dialogue from the JSON string
                dialogue = json.loads(example['dialogue'])
                # Check if any turn in the dialogue has the target action
                for turn in dialogue:
                    if turn.get('action') == filter_action:
                        return True
                return False
            except (json.JSONDecodeError, TypeError):
                # If the dialogue string is invalid, exclude the sample
                return False

        original_size = len(dataset)
        dataset = dataset.filter(contains_action, num_proc=4) # Use num_proc for faster filtering
        print(f"✅ Filtering complete. Found {len(dataset)} matching samples out of {original_size}.")


    if max_samples:
        dataset = dataset.select(range(min(max_samples, len(dataset))))

    print(f"Running inference on {len(dataset)} samples in '{inference_mode}' mode...")

    if output_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_name_tag = os.path.basename(adapter_path) # Use adapter path for a unique name
        output_file = f"inference_{model_name_tag}_{timestamp}_{inference_mode}.jsonl"

    # Resume support: skip samples already present in the output file
    start_index = 0
    if resume and os.path.exists(output_file):
        with open(output_file, 'r', encoding='utf-8') as f_in:
            start_index = sum(1 for _ in f_in)
        if start_index > 0:
            print(f"Resuming: {start_index} samples already in {output_file}, skipping them.")
            dataset = dataset.select(range(start_index, len(dataset)))

    # Open once in append mode; append one JSONL line per sample and persist immediately
    with open(output_file, 'a', encoding='utf-8') as f_out, open(output_file + ".events.jsonl", 'a', encoding='utf-8') as f_events:
        for i, example in enumerate(tqdm(dataset, desc="Generating Dialogues"), start=start_index):
            sample_events.clear()
            try:
                gt_dialogue = json.loads(example['dialogue'])
                ecg_files_str = example.get('ecg_files')
                file_list = json.loads(ecg_files_str) if ecg_files_str else []
                ecg_filename = file_list[0] if file_list else None

                generated_dialogue = []
                messages = [{"role": "system", "content": ECG_EVALUATION_PROMPT}]

                for turn in gt_dialogue:
                    if turn['role'] == 'user':
                        generated_dialogue.append(turn)
                        generated_dialogue.extend(run_user_turn(model, tokenizer, generation_config, messages, turn.get('content', ''), ecg_filename, tools, emit))

                        # Update history
                        if inference_mode == 'with_gt':
                            messages.pop()  # remove last user
                            if len(messages) > 1 and messages[-1]['role'] == 'assistant':
                                messages.pop()
                            current_gt_index = len(generated_dialogue)
                            messages = [{"role": "system", "content": ECG_EVALUATION_PROMPT}]
                            for gt_turn in gt_dialogue[:current_gt_index]:
                                role = gt_turn['role']
                                content = format_assistant_turn_for_messages(gt_turn) if role == 'assistant' else gt_turn.get('content', '')
                                messages.append({'role': role, 'content': content})

                # Build aligned per-turn view
                aligned_turns, n_gt_asst, n_gen_asst = build_aligned_turns(gt_dialogue, generated_dialogue)

                record = {
                    "sample_id": i,
                    "ecg_file": example.get("ecg_files"),
                    "source_category": example.get("source_category"),
                    "turns": aligned_turns,
                    "generated_dialogue": generated_dialogue,
                    "ground_truth_dialogue": gt_dialogue,
                    "summary": {
                        "num_gt_assistant_turns": n_gt_asst,
                        "num_generated_assistant_turns": n_gen_asst,
                    }
                }

                # Append one JSON line per sample and fsync
                f_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                f_out.flush()
                os.fsync(f_out.fileno())
                f_events.write(json.dumps({"sample_id": i, "events": [{"type": e.type, "value": e.value} for e in sample_events]}, ensure_ascii=False, default=str) + "\n")
                f_events.flush()

            except Exception as e:
                import traceback
                print(f"Error processing sample {i} (ECG: {ecg_filename}): {e}\n{traceback.format_exc()}")

    print(f"Inference complete. Results appended to {output_file}")

# ----------------------------
# <<< CHANGED: Argparse updated for base and adapter model paths >>>
# ----------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Run inference with an unmerged (base + adapter) ECG dialogue model.")
    parser.add_argument(
        "--base-model-path",
        type=str,
        default="unsloth/Llama-3.1-8B-Instruct", # Make this required
        help="Path to the base model (e.g., 'meta-llama/Meta-Llama-3-8B-Instruct')."
    )
    parser.add_argument(
        "--adapter-path",
        type=str,
        default="path/to/adapter_path", # Make this required
        help="Path to the fine-tuned adapter folder (containing adapter_config.json)."
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=None,
        help="Optional output .jsonl path. If omitted, a timestamped file is created."
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Limit the number of test samples to run."
    )
    parser.add_argument(
        "--inference-mode",
        type=str,
        choices=["without_gt", "with_gt"],
        default="without_gt",
        help="Use model-generated history (without_gt) or ground-truth history between turns (with_gt)."
    )
    
    parser.add_argument(
        "--filter-action",
        type=str,
        default=None,
        help="Optional. Only run inference on samples that contain this action in their ground-truth dialogue (e.g., 'response_fail')."
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip samples already present in --output-file and append from there."
    )

    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    # <<< CHANGED: Pass the new arguments to the main function >>>
    run_inference_on_test_set(
        base_model_path=args.base_model_path,
        adapter_path=args.adapter_path,
        output_file=args.output_file,
        max_samples=args.max_samples,
        inference_mode=args.inference_mode,
        filter_action=args.filter_action, # <<< ADD THIS LINE
        resume=args.resume,
    )