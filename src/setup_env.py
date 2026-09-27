"""Prepare a fresh Kaggle machine: packages, Ollama, ngrok and model files.

Uses the model cache (the output of notebooks/build_model_cache.ipynb, attached
as an input) when it is there, and downloads everything otherwise (slower).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import config

PIP_PACKAGES = [
    "faster-whisper==1.2.1", "kokoro==0.9.4", "soundfile", "fastapi", "uvicorn",
    "cryptography", "kaggle", "sentence-transformers",
]
NGROK_URL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"
INPUT_ROOT = Path("/kaggle/input")


@dataclass
class ModelPaths:
    whisper: str
    kokoro: Path
    embed: str
    speech_device_index: int


def sh(cmd: str, **kwargs) -> None:
    subprocess.run(cmd, shell=True, check=True, **kwargs)


def find_input(marker: str) -> Path | None:
    """Find an attached input (dataset or notebook output) by a file inside it."""
    for path in INPUT_ROOT.glob(f"**/{marker}"):
        return path.parent
    return None


def install_basics() -> None:
    """Just what the free-limit check needs (usually already on Kaggle: seconds)."""
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    # -U kaggle: the new-style API token needs a recent kaggle CLI. cryptography is
    # NOT upgraded: Kaggle's own packages (pyopenssl etc.) need the version it has.
    pip = [sys.executable, "-m", "pip", "install", "-q"]
    subprocess.run(pip + ["-U", "kaggle"], check=True)
    subprocess.run(pip + ["cryptography"], check=True)


def install_packages(cache: Path | None) -> None:
    pip = [sys.executable, "-m", "pip", "install", "-q"]
    if cache and (cache / "wheels").is_dir():
        offline = pip + ["--no-index", "--find-links", str(cache / "wheels"), *PIP_PACKAGES]
        if subprocess.run(offline, capture_output=True).returncode == 0:
            return
        print("[setup] cached packages incomplete; installing from the internet")
    subprocess.run(pip + PIP_PACKAGES, check=True)


def install_ngrok(cache: Path | None) -> None:
    target = Path("/usr/local/bin/ngrok")
    if target.exists():
        return
    if cache and (cache / "ngrok" / "ngrok").exists():
        shutil.copy(cache / "ngrok" / "ngrok", target)
    else:
        sh(f"curl -fsSL {NGROK_URL} | tar -xz -C /usr/local/bin")
    target.chmod(0o755)


def install_ollama(cache: Path | None) -> Path:
    """Install the Ollama binary and link cached model files. Returns the models dir."""
    models_dir = config.WORK_DIR / "ollama-models"
    if not Path("/usr/local/bin/ollama").exists():
        cached_bin = cache / "ollama" / "bin" / "ollama" if cache else None
        if cached_bin and cached_bin.exists():
            shutil.copy(cached_bin, "/usr/local/bin/ollama")
            os.chmod("/usr/local/bin/ollama", 0o755)
            lib = Path("/usr/local/lib/ollama")
            if not lib.exists():
                lib.symlink_to(cache / "ollama" / "lib" / "ollama")
        else:
            # The Ollama installer now needs "zstd" to unpack; Kaggle machines may not have it.
            sh("command -v zstd || apt-get install -y -qq zstd "
               "|| (apt-get update -qq && apt-get install -y -qq zstd)", stdout=subprocess.DEVNULL)
            sh("curl -fsSL https://ollama.com/install.sh | sh", stdout=subprocess.DEVNULL)
    if cache and (cache / "ollama" / "models").is_dir() and not models_dir.exists():
        # A tree of symlinks: instant, and nothing is copied off the read-only input.
        sh(f"cp -rs '{cache / 'ollama' / 'models'}' '{models_dir}'")
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir


_ollama_models_dir: Path | None = None


def start_ollama(models_dir: Path | None = None) -> None:
    global _ollama_models_dir
    _ollama_models_dir = models_dir or _ollama_models_dir
    subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
    env = {**os.environ,
           "CUDA_VISIBLE_DEVICES": config.OLLAMA_GPU,
           "OLLAMA_MODELS": str(_ollama_models_dir),
           "OLLAMA_KEEP_ALIVE": "60m",
           "OLLAMA_MAX_LOADED_MODELS": "2",  # base + fine-tuned during the A/B test
           "OLLAMA_HOST": config.OLLAMA_URL.removeprefix("http://")}
    subprocess.Popen(["ollama", "serve"], env=env,
                     stdout=open(config.WORK_DIR / "ollama.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            subprocess.run(["curl", "-sf", f"{config.OLLAMA_URL}/api/version"],
                           check=True, capture_output=True)
            return
        except subprocess.CalledProcessError:
            time.sleep(1)
    raise RuntimeError("Ollama did not start. See /tmp/pva/ollama.log")


def ensure_llm() -> str:
    """Make sure the chosen model is available; pull it (or the fallback) if not."""
    import llm
    for model in (config.LLM_MODEL, config.LLM_FALLBACK_MODEL):
        if llm.is_ready(model):
            return model
        print(f"[setup] downloading {model} (not in cache, this takes a few minutes)")
        env = {**os.environ, "OLLAMA_HOST": config.OLLAMA_URL.removeprefix("http://")}
        if subprocess.run(["ollama", "pull", model], env=env, capture_output=True).returncode == 0:
            return model
    raise RuntimeError("Could not get any LLM model into Ollama.")


# Chat format for the fine-tuned Qwen3 model. The empty <think></think> switches off
# Qwen3's hidden reasoning, exactly as in training (enable_thinking=False).
TUNED_MODELFILE = '''FROM ./model.gguf
TEMPLATE """{{- range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
<think>

</think>

"""
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"
PARAMETER temperature 0.7
PARAMETER top_p 0.8
PARAMETER top_k 20
'''


def install_tuned_model(key: bytes) -> str | None:
    """Load the encrypted fine-tuned model (Phase 5) into Ollama, if it is attached.
    Returns its Ollama name, or None."""
    import crypto
    folder = find_input(config.TUNED_MARKER)
    if folder is None or config.TUNED_MODE == "base":
        return None
    info = json.loads((folder / config.TUNED_MARKER).read_text())
    name = info["ollama_name"]
    work = config.WORK_DIR / "tuned"
    work.mkdir(parents=True, exist_ok=True)
    print(f"[setup] loading fine-tuned model {name}")
    try:
        crypto.decrypt_file(folder / info["file"], work / "model.gguf", key)
        (work / "Modelfile").write_text(TUNED_MODELFILE)
        env = {**os.environ, "OLLAMA_HOST": config.OLLAMA_URL.removeprefix("http://")}
        result = subprocess.run(["ollama", "create", name, "-f", "Modelfile"], cwd=work, env=env,
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[setup] fine-tuned model not loaded: {result.stderr.strip()[-200:]}")
            return None
        return name
    except Exception as e:  # never block the assistant because of the optional model
        print(f"[setup] fine-tuned model not loaded: {type(e).__name__}: {e}")
        return None
    finally:
        (work / "model.gguf").unlink(missing_ok=True)  # Ollama keeps its own copy


def model_paths(cache: Path | None) -> ModelPaths:
    import torch
    speech_gpu = config.SPEECH_GPU_INDEX if torch.cuda.device_count() > 1 else 0
    hf = cache / "hf" if cache else None
    if hf and (hf / "whisper").is_dir():
        return ModelPaths(str(hf / "whisper"), hf / "kokoro", str(hf / "embed"), speech_gpu)

    from huggingface_hub import snapshot_download
    print("[setup] model cache not attached: downloading speech models")
    base = config.WORK_DIR / "hf"
    whisper = snapshot_download("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                                local_dir=base / "whisper")
    kokoro = snapshot_download(config.KOKORO_REPO, local_dir=base / "kokoro",
                               allow_patterns=["config.json", "*.pth",
                                               *[f"voices/{v}.pt" for _, v in config.TTS_VOICES.values()]])
    embed = snapshot_download(config.EMBED_MODEL, local_dir=base / "embed")
    return ModelPaths(whisper, Path(kokoro), embed, speech_gpu)


def prepare() -> tuple[ModelPaths, str]:
    """Run everything. Returns (model paths, LLM model name)."""
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    cache = find_input(config.MODELS_MARKER)
    if cache:
        info = json.loads((cache / config.MODELS_MARKER).read_text())
        print(f"[setup] using model cache from {info.get('built', '?')}")
        if info.get("llm") != config.LLM_MODEL:
            print(f"[setup] note: cache has {info.get('llm')}, config wants {config.LLM_MODEL}")
    else:
        print("[setup] no model cache attached (see README step 5); everything will download")

    install_packages(cache)
    install_ngrok(cache)
    models_dir = install_ollama(cache)
    start_ollama(models_dir)
    llm_model = ensure_llm()
    return model_paths(cache), llm_model
