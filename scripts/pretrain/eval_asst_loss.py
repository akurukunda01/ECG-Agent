"""
Stress test 02: score a trained adapter on the validation split with cross-entropy over
assistant-role tokens only, so masked and unmasked recipes are compared on one metric.

Usage: python eval_asst_loss.py --adapter <adapter_dir> [--limit N]
"""
import os
import sys

os.environ["UNSLOTH_RETURN_LOGITS"] = "1"
import multiprocessing
multiprocessing.cpu_count = lambda: 4
os.cpu_count = lambda: 4

import argparse

from unsloth import FastLanguageModel  # must import before trl/transformers

sys.path.insert(0, "/mnt/c/Users/kuruk/Desktop/VS_Projects/ECG-Agent/ECG-Agent/src")

import torch
from trl import SFTTrainer, SFTConfig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--limit", type=int, default=None, help="cap validation examples")
    args = ap.parse_args()

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.adapter,
        max_seq_length=4096,
        dtype=None,
        load_in_4bit=False,
    )

    from finetune_ecg_dialogue_unsloth import load_and_preprocess_dataset
    _, val_ds, _ = load_and_preprocess_dataset(tokenizer)
    if args.limit:
        val_ds = val_ds.select(range(min(args.limit, len(val_ds))))
    print(f"validation examples: {len(val_ds)}")

    cfg = SFTConfig(
        output_dir="/tmp/asst_eval",
        per_device_eval_batch_size=8,
        bf16=torch.cuda.is_bf16_supported(),
        fp16=not torch.cuda.is_bf16_supported(),
        report_to="none",
    )
    cfg.dataset_num_proc = 8

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=val_ds.select(range(8)),  # unused; required by the constructor
        eval_dataset=val_ds,
        args=cfg,
    )

    from unsloth.chat_templates import train_on_responses_only
    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|start_header_id|>user<|end_header_id|>\n\n",
        response_part="<|start_header_id|>assistant<|end_header_id|>\n\n",
    )

    lab = list(trainer.eval_dataset[0]["labels"])
    assert lab.count(-100) > 0, "eval masking did not apply"
    print(f"[MASK CHECK] example 0: {lab.count(-100)}/{len(lab)} tokens masked")

    metrics = trainer.evaluate()
    print(f"RESULT adapter={args.adapter}")
    print(f"RESULT assistant-only eval_loss = {metrics['eval_loss']:.5f}")


if __name__ == "__main__":
    main()
