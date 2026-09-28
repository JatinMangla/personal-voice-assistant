"""Prepare Jarvis on your own PC (roadmap stage A): the counterpart of setup_env.py.

Nothing is installed here: you install Python packages (requirements-home.txt), Ollama
(ollama.com) and optionally ngrok once, by hand (README, "Run on your own PC"). This
file starts Ollama if needed, downloads the models once (kept in PVA_HOME_DIR), and
picks GPU or CPU:
- NVIDIA GPU: the same models as on Kaggle (Qwen3.5 9B, Whisper large-v3-turbo);
- no GPU: Qwen3 4B and Whisper small (int8), slower but private and free.
"""
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import setup_env
from setup_env import ModelPaths, _download_speech_models, _pull

ollama_devices = setup_env.ollama_devices  # same log parsing (when we started Ollama)


def install_basics() -> None:
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)


def gpu_count() -> int:
    try:
        import torch
        return torch.cuda.device_count() if torch.cuda.is_available() else 0
    except ImportError:
        return 0


def _ollama_up() -> bool:
    try:
        return requests.get(f"{config.OLLAMA_URL}/api/version", timeout=3).ok
    except requests.RequestException:
        return False


def start_ollama(models_dir=None) -> None:
    """Use the Ollama that is already running (the Windows/macOS app starts it), or
    start `ollama serve` with our settings."""
    if _ollama_up():
        return
    binary = shutil.which("ollama")
    if not binary:
        raise RuntimeError("Ollama is not installed: get it from https://ollama.com, "
                           "then start Jarvis again.")
    env = {**os.environ, "OLLAMA_KEEP_ALIVE": "60m", "OLLAMA_NUM_PARALLEL": "2",
           "OLLAMA_HOST": config.OLLAMA_URL.removeprefix("http://"),
           "OLLAMA_FLASH_ATTENTION": "1" if config.OLLAMA_FLASH_ATTENTION else "0"}
    subprocess.Popen([binary, "serve"], env=env, stdout=open(config.WORK_DIR / "ollama.log", "a"),
                     stderr=subprocess.STDOUT)
    for _ in range(60):
        if _ollama_up():
            return
        time.sleep(1)
    raise RuntimeError(f"Ollama did not start. See {config.WORK_DIR / 'ollama.log'}")


def ollama_log_tail(lines: int = 25) -> str:
    try:
        return "\n".join((config.WORK_DIR / "ollama.log").read_text(errors="ignore")
                         .splitlines()[-lines:])
    except OSError:
        return "(Ollama was already running, so its log is in the Ollama app's own folder)"


def reinstall_ollama_online() -> None:
    raise RuntimeError("The LLM did not answer. Restart the Ollama app (or reinstall it from "
                       "https://ollama.com) and start Jarvis again.")


def choose_llm(gpus: int) -> str:
    """The configured model, or the small one on a PC without a GPU (unless you chose one
    yourself with PVA_LLM_MODEL)."""
    if gpus == 0 and not os.environ.get("PVA_LLM_MODEL"):
        return config.HOME_CPU_LLM
    return config.LLM_MODEL


def ensure_llm(cache=None, models_dir=None) -> str:
    import llm
    model = choose_llm(gpu_count())
    if llm.is_ready(model):
        return model
    t = time.time()
    print(f"[setup] downloading {model} (first start only; several GB)")
    if not _pull(model):
        raise RuntimeError(f"Could not download {model}: check the internet connection, or "
                           f"run 'ollama pull {model}' yourself.")
    print(f"[setup] {model} downloaded in {time.time() - t:.0f}s")
    return model


def install_tuned_model(key: bytes):
    return None  # Phase 5 models are trained and attached on Kaggle


def _prepare_llm() -> str:
    import llm
    model = ensure_llm()
    t = time.time()
    llm.load_model(model)
    print(f"[setup] {model} loaded in {time.time() - t:.0f}s")
    return model


def model_paths(gpus: int) -> ModelPaths:
    """Speech + embedding models, downloaded once into PVA_HOME_DIR/models."""
    base = config.HOME_DIR / "models"
    marker = base / ("gpu.done" if gpus else "cpu.done")
    folder = base / ("gpu" if gpus else "cpu")
    if marker.exists():  # downloaded before: start without needing the internet
        whisper, kokoro, embed = str(folder / "whisper"), folder / "kokoro", str(folder / "embed")
    else:
        t = time.time()
        whisper_repo = config.WHISPER_REPO if gpus else config.WHISPER_REPO_CPU
        whisper, kokoro, embed = _download_speech_models(folder, whisper_repo)
        print(f"[setup] speech models downloaded in {time.time() - t:.0f}s")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    index = config.SPEECH_GPU_INDEX if gpus > 1 else 0
    return ModelPaths(whisper, kokoro, embed, index, "cuda" if gpus else "cpu")


def prepare():
    """Same contract as setup_env.prepare(): (speech model paths, LLM job)."""
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    gpus = gpu_count()
    print(f"[setup] home PC: {gpus or 'no'} NVIDIA GPU(s)"
          + ("" if gpus else "; using the smaller CPU models (slower replies)"))
    start_ollama()
    pool = ThreadPoolExecutor(max_workers=1)
    llm_job = pool.submit(_prepare_llm)
    pool.shutdown(wait=False)
    return model_paths(gpus), llm_job
