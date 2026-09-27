import json
import sys

cells = []


def md(text):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(True)})


def code(text):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                  "source": text.strip("\n").splitlines(True)})


md("""
# Phase 5: teach Jarvis your style (fine-tuning)

Trains **Qwen3-8B** on the replies you rated 👍, so it talks the way you like. Facts are **not** trained in: they stay in the encrypted memory.

**When:** once the assistant has **500+ 👍 replies** (this notebook checks and stops if not). At most once a month.

**Settings:** Accelerator = **GPU T4 x2**, Internet = **On**, keep it **Private**.
**Input:** your `assistant-code` dataset. **Secrets:** `KAGGLE_USERNAME`, `KAGGLE_API_TOKEN`, `MEMORY_KEY`.
**Run:** Save Version → **Save & Run All**. It takes about 45–90 minutes.
**Don't** run the assistant at the same time.

**Free-limit safety:**
- It checks your GPU budget first.
- Training stops early if it would pass the 99% limit.
- It adds its GPU time to the same daily/weekly counters the assistant uses.

**After it finishes:** in the assistant notebook, **Add Input → Your Work → Notebooks → this notebook**. The assistant then runs a blind 👍/👎 test between the old and new model, and keeps the better one automatically.
""")

code("""
import sys, time, json, shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

START = time.time()
import zipfile
src = next((p.parent for p in Path("/kaggle/input").glob("**/app.py")
            if (p.parent / "main.py").exists()), None)
for z in ([] if src else Path("/kaggle/input").glob("**/assistant-code*.zip")):
    zipfile.ZipFile(z).extractall("/tmp/pva-code")  # zip kept as a file
    src = Path("/tmp/pva-code/src")
    break
assert src, "Attach your 'assistant-code' dataset: Add Input -> Your Work -> Datasets."
sys.path.insert(0, str(src))

import config, crypto, setup_env
setup_env.install_basics()
from budget import current_meters
from finetune_data import build_examples, training_minutes_allowed
from llm import system_prompt
from memory import Memory
from persistence import MemoryStore

key = crypto.load_key(config.get_secret("MEMORY_KEY"))
user, kaggle_env = config.kaggle_credentials()
store = MemoryStore(user, key, kaggle_env)


def now_ist():
    return datetime.now(ZoneInfo(config.TIMEZONE))


def record_gpu_time():
    \"\"\"Add this notebook's GPU time to the assistant's free-limit counters.\"\"\"
    memory = Memory(None, store.load())  # the latest saved memory
    memory.add_usage(now_ist().date().isoformat(),
                     time.time() - START + config.SESSION_OVERHEAD_MIN * 60)
    print("GPU time recorded in memory:", "ok" if store.save(memory.to_bytes()) else "FAILED")


memory = Memory(None, store.load() or None)
good = memory.good_turns()
examples = build_examples(good)
print(f"{len(good)} replies rated 👍; {len(examples)} usable (private-looking ones are skipped)")

minutes = training_minutes_allowed(current_meters(memory, now_ist()))
print(f"GPU minutes available for training before the 99% free limit: {minutes}")
del memory, good

if len(examples) < config.FINETUNE_MIN_GOOD_TURNS:
    record_gpu_time()
    raise SystemExit(f"Not enough yet: need {config.FINETUNE_MIN_GOOD_TURNS} usable 👍 replies. "
                     "Keep rating replies in the assistant and try again later.")
if minutes < 30:
    record_gpu_time()
    raise SystemExit("Not enough free GPU time left today/this week. Try again tomorrow.")
DEADLINE = START + minutes * 60
""")

code("""
if "DEADLINE" not in globals():  # the step above stopped (not enough data or GPU time)
    raise SystemExit("Stopped in the step above.")

# Everything below is inside try/finally, so the GPU time is recorded even if it fails.
try:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "unsloth"], check=True)

    from unsloth import FastLanguageModel, is_bfloat16_supported
    from unsloth.chat_templates import train_on_responses_only
    from datasets import Dataset
    from transformers import TrainerCallback
    from trl import SFTConfig, SFTTrainer

    # 1. Base model (4-bit) + LoRA adapters
    model, tokenizer = FastLanguageModel.from_pretrained(
        config.FINETUNE_BASE, max_seq_length=2048, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=16, lora_dropout=0, bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407)

    # 2. Training data: same chat format as the assistant, thinking switched off
    texts = [tokenizer.apply_chat_template(ex, tokenize=False, enable_thinking=False)
             for ex in examples]
    dataset = Dataset.from_dict({"text": texts})

    class StopAtFreeLimit(TrainerCallback):
        def on_step_end(self, args, state, control, **kwargs):
            if time.time() > DEADLINE:
                print("Free GPU budget reached: stopping training early.")
                control.should_training_stop = True

    trainer = SFTTrainer(
        model=model, tokenizer=tokenizer, train_dataset=dataset,
        callbacks=[StopAtFreeLimit()],
        args=SFTConfig(
            dataset_text_field="text",
            per_device_train_batch_size=2, gradient_accumulation_steps=4,
            num_train_epochs=2, warmup_steps=5, learning_rate=2e-4,
            fp16=not is_bfloat16_supported(), bf16=is_bfloat16_supported(),
            optim="adamw_8bit", weight_decay=0.01, lr_scheduler_type="linear",
            logging_steps=10, seed=3407, output_dir="/tmp/ft/checkpoints",
            save_strategy="no", report_to="none"))
    # Learn only from the assistant's replies, not from your questions.
    trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\\n",
                                      response_part="<|im_start|>assistant\\n")
    trainer.train()

    # 3. Quick check with two general questions (nothing personal is printed)
    FastLanguageModel.for_inference(model)
    for lang, question in [("en", "Good morning! Any tip for a productive day?"),
                           ("hi", "आज शाम को क्या करूँ?")]:
        msgs = [{"role": "system", "content": system_prompt(lang, [], [], now_ist())},
                {"role": "user", "content": question}]
        ids = tokenizer.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True,
                                            enable_thinking=False, return_tensors="pt").to("cuda")
        out = model.generate(input_ids=ids, max_new_tokens=80, temperature=0.7, top_p=0.8, top_k=20)
        print(question, "->", tokenizer.decode(out[0][ids.shape[1]:], skip_special_tokens=True))

    # 4. Export for Ollama (4-bit GGUF), then encrypt it; the plain file never leaves /tmp
    model.save_pretrained_gguf("/tmp/ft/gguf", tokenizer, quantization_method="q4_k_m")
    ggufs = sorted(Path("/tmp/ft").rglob("*.gguf"), key=lambda p: ("q4_k_m" not in p.name.lower(),
                                                                   -p.stat().st_size))
    assert ggufs, "GGUF export failed"
    out_dir = Path("/kaggle/working/tuned")
    out_dir.mkdir(parents=True, exist_ok=True)
    crypto.encrypt_file(ggufs[0], out_dir / "jarvis-style.gguf.enc", key)
    name = "jarvis-style-" + now_ist().strftime("%Y%m%d-%H%M")
    (out_dir / config.TUNED_MARKER).write_text(json.dumps({
        "ollama_name": name, "file": "jarvis-style.gguf.enc", "base": config.FINETUNE_BASE,
        "built": now_ist().isoformat(timespec="minutes"), "examples": len(examples)}))
    print(f"Done: encrypted model '{name}' saved to the notebook output.")
finally:
    shutil.rmtree("/tmp/ft", ignore_errors=True)
    record_gpu_time()
""")

nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                                  "name": "python3"},
                                   "language_info": {"name": "python"}},
      "nbformat": 4, "nbformat_minor": 5}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(nb, f, ensure_ascii=False, indent=1)
    f.write("\n")
