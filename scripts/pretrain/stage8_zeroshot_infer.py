"""
Stress test 05: run the upstream inference harness with NO adapter (stock instruct model).
The harness is imported unchanged; only the PEFT adapter step is made a no-op.
--adapter-path is still required by the harness's argparse and only supplies the output tag.

Usage (CWD must contain results/*.csv, e.g. repo src/):
  python stage8_zeroshot_infer.py --base-model-path unsloth/Llama-3.2-1B-Instruct \
      --adapter-path Llama-3.2-1B-zeroshot --output-file ... --inference-mode without_gt
Env: ZEROSHOT_NO_THINK=1 passes enable_thinking=False to the chat template (Qwen3).
"""
import os, sys
sys.path.insert(0, os.getcwd())
import inference_ecg_dialogue as harness


def _no_adapter(model, adapter_path, *a, **k):
    print(f"[ZERO-SHOT] adapter step skipped (tag only: {adapter_path})")
    return model


harness.PeftModel.from_pretrained = staticmethod(_no_adapter)

if os.environ.get("ZEROSHOT_NO_THINK") == "1":
    _orig_gen = harness.generate_full_response

    def _gen(model, tokenizer, messages, generation_config):
        _orig_apply = tokenizer.apply_chat_template
        tokenizer.apply_chat_template = lambda *a, **k: _orig_apply(*a, enable_thinking=False, **k)
        try:
            return _orig_gen(model, tokenizer, messages, generation_config)
        finally:
            tokenizer.apply_chat_template = _orig_apply

    harness.generate_full_response = _gen
    print("[ZERO-SHOT] enable_thinking=False patched into apply_chat_template")

if __name__ == "__main__":
    args = harness.parse_args()
    harness.run_inference_on_test_set(
        base_model_path=args.base_model_path, adapter_path=args.adapter_path,
        output_file=args.output_file, max_samples=args.max_samples,
        inference_mode=args.inference_mode, filter_action=args.filter_action,
        resume=args.resume)
