import os
import sys
import json
import argparse
import torch
import re
import pandas as pd
from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig
from peft import PeftModel # <<< NEW: Import PeftModel
sys.path.insert(0, os.getcwd())
from medrax.tools.classification import ECGClassifierTool, ECGAnalysisTool
from events import Event, Emitter, render

# --- Constants for external data paths (update if necessary) ---

CLASSIFIER_CHECKPOINT_PATH = os.path.expanduser("~/data/ptbxl/ckpt_diagnosis/checkpoint_best.pt")
ECG_DIR = os.path.expanduser("~/data/ptbxl/ptbxl_10s_padded")
PROBABILITY_THRESHOLD = 0.5

# CORRECTED Master Evaluation Prompt
ECG_EVALUATION_PROMPT = """
Instruction:
You will be provided with part of a dialogue between the user and the system. The dialogue is conducted with a ECG wearable device user and the system, therefore the system should provide direct answers to the user's inquries without acting like a medical professional.
In this dialogue, the user is inquiring about an ECG reading, and the system will either respond directly or make appropriate tool calls to retrieve the necessary information to respond.
Your task is to generate appropriate thoughts, actions, and responses based on the dialogue history and the user's most recent utterance.

<Action list>
- call_classification_tool: Use this to identify arrhythmias, abnormalities, and findings from the provided ECG data
- call_measurement_tool: Use this to measure and output the heart rate, PR interval, QRS duration, and QTc interval.
- response: Provide comprehensive answer using results from tool outputs, combining technical findings with clinical interpretation
- response_fail: Indicate that the requested analysis cannot be performed due to tool limitations or because the question is not within the scope of the System and requires medical professional consultation.
- response_followup: Responds to the user's followup questions by providing additional information, clarification, or related insights about previous discussed ECG findings without requiring new tool calls.
- system_bye: Acknowledges the user's gratitude end the conversation politely

<General Rules>
1. Each generated dialogue (except for system_bye) is during the conversation with the user, therefore the responses should only address the user's last utterance and should not be too long.
2. Generated dialogue should be like a natural conversation between a user and ECG wearable assistant.

Based on the rules above, you will get an input as below:
Input:
<Dialogue history>
User: <User's last utterance> or Assistant: <Assistant's last Tool_Output>

And here is the format for what you should return in two different cases:

Case1. When a tool must be called based on the user's inquiry:
Action: <Assistant's chosen action, one of the tools>
Thought: <Assistant's reasoning process>
Tool_Output: <This will be provided externally.>

Case2. When providing a response based on previous tool or any other action not requiring a tool call:
Action: <Assistant's chosen action>
Thought: <Assistant's reasoning process>
Content: <Assistant's response>

Now, generate appropriate reasoning trace and responding message to user. Only generate in the proper format above, without indicating the chosen case.
"""

# <<< CHANGED: This function now accepts base and adapter paths >>>
def load_model_and_tokenizer(base_model_path, adapter_path):
    """Loads the base model and applies the PEFT adapter."""
    print(f"Loading base model from: {base_model_path}")
    print(f"Applying adapter from: {adapter_path}")

    # Use bfloat16 if supported for better performance, otherwise float16
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16

    # Load the base model
    model = AutoModelForCausalLM.from_pretrained(
        base_model_path,
        torch_dtype=dtype,
        device_map="auto",  # Automatically distributes model across available GPUs
    )

    # Load the tokenizer from the adapter path (it's often saved there during fine-tuning)
    tokenizer = AutoTokenizer.from_pretrained(base_model_path, use_fast=True)
    
    # Load the PEFT model (adapter) and apply it to the base model
    model = PeftModel.from_pretrained(model, adapter_path)
    
    # Set to evaluation mode
    model.eval()

    # Llama models often don't have a pad token; set it to the EOS token
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    return model, tokenizer

def load_ground_truth_data():
    """Instantiates the live tools (classifier from checkpoint, neurokit2 measurement)."""
    print("Loading live tools...")
    try:
        classification_tool = ECGClassifierTool(model_path=CLASSIFIER_CHECKPOINT_PATH)
        measurement_tool = ECGAnalysisTool()
        print("✅ Successfully loaded live tools.")
        return {
            "measurement": measurement_tool,
            "classification": classification_tool
        }
    except FileNotFoundError as e:
        print(f"🛑 Error: Could not find the classifier checkpoint. Please check your paths.")
        print(f"Details: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"🛑 An error occurred while loading tools: {e}")
        sys.exit(1)

def get_live_tool_output(action, ecg_filename, gt_data, emit=lambda _event: None):
    """Runs the requested tool on the ECG file and formats its output."""
    if not ecg_filename:
        return "[Error: ECG filename not provided]"
    ecg_path = os.path.join(ECG_DIR, ecg_filename)
    try:
        if action == "call_classification_tool":
            tool = gt_data["classification"]
            outputs, _ = tool._run(ecg_path=ecg_path)
            emit(Event("tool_return", outputs))
            if outputs and "error" not in outputs:
                filtered_classes = {label: prob for label, prob in outputs.items() if prob > PROBABILITY_THRESHOLD}
                sorted_classes = sorted(filtered_classes.items(), key=lambda item: item[1], reverse=True)
                if sorted_classes:
                    top_classes_val = ", ".join([f"{label} ({prob:.2%})" for label, prob in sorted_classes])
                else:
                    top_classes_val = f"None > {PROBABILITY_THRESHOLD:.0%}"
            else:
                top_classes_val = "Classification Error"
            return str([c.strip() for c in top_classes_val.split(',')]) if pd.notna(top_classes_val) else "[]"
        elif action == "call_measurement_tool":
            tool = gt_data["measurement"]
            outputs = tool._run(ecg_path=ecg_path)
            emit(Event("tool_return", outputs))
            rec = outputs if (outputs and "error" not in outputs) else {}
            measurements = {
                "heart_rate": f"{rec.get('Heart_Rate'):.2f}" if pd.notna(rec.get('Heart_Rate')) else None,
                "pr_interval": f"{rec.get('PR_Interval_ms'):.0f}" if pd.notna(rec.get('PR_Interval_ms')) else None,
                "qrs_duration": f"{rec.get('QRS_Duration_ms'):.0f}" if pd.notna(rec.get('QRS_Duration_ms')) else None,
                "qtc_interval": f"{rec.get('QTc_ms'):.2f}" if pd.notna(rec.get('QTc_ms')) else None,
            }
            return json.dumps(measurements)
            return "[]"
    except Exception:
        return f"[Error: Failed to retrieve data for {ecg_filename}]"
    return "[Error: Unknown tool action]"

def parse_generated_response(generated_text):
    """
    Parses the full text output from the model to extract action, thought, and content.
    This version is highly robust and handles:
    - Tags with or without square brackets [].
    - Case-insensitivity (Action:, action:, etc.).
    - Multi-line thoughts and content.
    - Tool outputs containing JSON-like structures with {}.
    """
    text = generated_text.strip()
    action, thought, content = '', '', ''

    # Define robust patterns with optional brackets and careful multiline handling
    # The action is expected to be a single line.
    action_pattern = r"(?im)^\s*\[?\s*Action\s*[:\-]\s*([^\n\r\]]+)"
    
    # The thought can be multiline. We use a "positive lookahead" (?=...) to make it
    # capture everything until the *next* tag is found or the text ends.
    thought_pattern = r"^\s*\[?Thought:\s*(.*?)(?=\n\s*\[?(?:Action|Content|Tool_Output):|$)"
    
    # Content and Tool_Output can be multiline and capture everything to the end.
    content_pattern = r"^\s*\[?Content:\s*(.*)"
    tool_output_pattern = r"^\s*\[?Tool_Output:\s*(.*)"

    # Define the regex flags to be used
    single_line_flags = re.IGNORECASE | re.MULTILINE
    multi_line_flags = re.IGNORECASE | re.MULTILINE | re.DOTALL

    # Find matches for all parts
    action_match = re.search(action_pattern, text, single_line_flags)
    thought_match = re.search(thought_pattern, text, multi_line_flags)
    content_match = re.search(content_pattern, text, multi_line_flags)
    tool_output_match = re.search(tool_output_pattern, text, multi_line_flags)

    # Extract data from matches
    if action_match:
        action = action_match.group(1).strip()
    
    if thought_match:
        thought = thought_match.group(1).strip()

    # Intelligently determine the final content
    if content_match:
        # If there is an explicit "Content:" tag, that's our content.
        content = content_match.group(1).strip()
    elif not (action_match or thought_match or tool_output_match):
        # If NO tags were found at all, the entire text is the user-facing content.
        content = text
    # Otherwise, `content` remains a blank string (''), which is the correct behavior
    # for a tool call turn that doesn't have an explicit "Content:" section.
        
    return {
        'role': 'assistant',
        'action': action,
        'thought': thought,
        'content': content
    }

def generate_full_response(model, tokenizer, messages, generation_config):
    """
    Generates a response using the provided message history.
    It now takes a list of message dicts and uses the chat template.
    """
    # Apply the chat template
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

    with torch.no_grad():
        outputs = model.generate(**inputs, generation_config=generation_config)

    # Decode only the newly generated tokens
    new_tokens = outputs[0][inputs['input_ids'].shape[1]:]
    decoded_text = tokenizer.decode(new_tokens, skip_special_tokens=True)

    return decoded_text.strip()

def format_assistant_turn_for_messages(turn):
    """
    Formats an assistant turn into the string content for the message list.
    """
    action = turn.get('action', '')
    thought = turn.get('thought', '')

    assistant_content = f"Action: {action}\nThought: {thought}\n"

    if 'tool_output' in turn:
        assistant_content += f"Tool_Output: {turn['tool_output']}"
    elif 'content' in turn:
        assistant_content += f"Content: {turn.get('content', '')}"

    return assistant_content.strip()

def run_user_turn(model, tokenizer, gen_cfg, messages, user_text, ecg_handle, tools, emit):
    """One user turn: append the user message, run the two-generate turn shape,
    append the final assistant turn to `messages` (mutated in place), and return
    the generated assistant turn dicts. Body moved from run_inference_on_test_set."""
    # Add user turn
    user_turn = {"role": "user", "content": user_text}
    messages.append(user_turn)

    # First assistant step
    model_output_str = generate_full_response(model, tokenizer, messages, gen_cfg)
    emit(Event("generation", model_output_str))
    parsed_turn = parse_generated_response(model_output_str)
    emit(Event("parse", parsed_turn))
    emit(Event("action", parsed_turn['action']))

    model_generated_turns_for_this_user_prompt = []

    # Tool call or direct response?
    if parsed_turn['action'] in ["call_classification_tool", "call_measurement_tool"]:
        emit(Event("tool_call", (parsed_turn['action'], ecg_handle)))
        tool_call_turn = {
            "role": "assistant",
            "action": parsed_turn['action'],
            "thought": parsed_turn['thought'],
            "tool_output": tools(parsed_turn['action'], ecg_handle)
        }
        emit(Event("observation_rendered", tool_call_turn["tool_output"]))
        model_generated_turns_for_this_user_prompt.append(tool_call_turn)

        # Final response using tool output
        messages.append({"role": "assistant", "content": format_assistant_turn_for_messages(tool_call_turn)})
        final_content_str = generate_full_response(model, tokenizer, messages, gen_cfg)
        emit(Event("generation", final_content_str))
        parsed_final_turn = parse_generated_response(final_content_str)
        emit(Event("parse", parsed_final_turn))
        emit(Event("action", parsed_final_turn.get("action", "response")))

        response_turn = {
            "role": "assistant",
            "action": parsed_final_turn.get("action", "response"), 
            "thought": parsed_final_turn.get("thought", "No thought generated."), # Provide a descriptive default thought.
            "content": parsed_final_turn.get("content", "") # The only part we truly need from the model's second output.
        }
        emit(Event("response", response_turn))
        model_generated_turns_for_this_user_prompt.append(response_turn)
    else:
        direct_response_turn = {
            "role": "assistant",
            "action": parsed_turn['action'],
            "thought": parsed_turn['thought'],
            "content": parsed_turn.get("content", "")
        }
        emit(Event("response", direct_response_turn))
        model_generated_turns_for_this_user_prompt.append(direct_response_turn)

    # Update history
    if len(model_generated_turns_for_this_user_prompt) == 1:
        messages.append({"role": "assistant", "content": format_assistant_turn_for_messages(model_generated_turns_for_this_user_prompt[0])})
    elif len(model_generated_turns_for_this_user_prompt) == 2:
        messages.append({"role": "assistant", "content": format_assistant_turn_for_messages(model_generated_turns_for_this_user_prompt[1])})

    return model_generated_turns_for_this_user_prompt

def make_generation_config(tokenizer):
    """Block moved verbatim from run_inference_on_test_set."""
    # Deterministic generation for eval
    eot_id = tokenizer.convert_tokens_to_ids("<|eot_id|>")
    eos_ids = [t for t in [tokenizer.eos_token_id, eot_id] if t is not None]
    generation_config = GenerationConfig(
        max_new_tokens=512,
        temperature=0.0,
        top_p=1.0,
        do_sample=False,
        eos_token_id=eos_ids or None,
        pad_token_id=tokenizer.pad_token_id,
    )
    return generation_config


class Session(Emitter):
    """Conversation state that outlives a single input() call."""
    def __init__(self, observer=None):
        super().__init__(observer)
        self.messages = [{"role": "system", "content": ECG_EVALUATION_PROMPT}]
        self.ecg_handle = None
        self.turn_count = 0
        self.event_log = []


def main():
    parser = argparse.ArgumentParser(description="Interactive ECG-Agent session with live tools. Run from src/.")
    parser.add_argument("--base-model-path", type=str, required=True)
    parser.add_argument("--adapter-path", type=str, required=True)
    parser.add_argument("--ecg", type=str, default=None, help="ECG filename under ECG_DIR, e.g. HR00056.mat.")
    args = parser.parse_args()

    model, tokenizer = load_model_and_tokenizer(args.base_model_path, args.adapter_path)
    gt_data = load_ground_truth_data()
    generation_config = make_generation_config(tokenizer)

    def observer(event):
        session.event_log.append(event)
        render(event)

    session = Session(observer)
    session.ecg_handle = args.ecg
    emit = lambda event: session._emit(event)
    tools = lambda action, ecg_handle: get_live_tool_output(action, ecg_handle, gt_data, emit)
    print("Ready. Ctrl-D to exit.")

    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            break
        if not text:
            continue
        run_user_turn(model, tokenizer, generation_config, session.messages, text, session.ecg_handle, tools, emit)
        session.turn_count += 1


if __name__ == "__main__":
    main()
