"""Prepare a fresh Kaggle machine: packages, Ollama, ngrok and model files.

Uses the model cache (the output of notebooks/build_model_cache.ipynb, attached
as an input) when it is there, and downloads everything otherwise (slower).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import config

FIRST_CACHE_PACKAGES = [  # what the first model cache (2026-09-27) contains
    "faster-whisper==1.2.1", "kokoro==0.9.4", "soundfile", "fastapi", "uvicorn",
    "cryptography", "kaggle", "sentence-transformers",
]
PIP_PACKAGES = FIRST_CACHE_PACKAGES + ["ddgs"]
NGROK_URL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"
INPUT_ROOT = Path("/kaggle/input")


@dataclass
class ModelPaths:
    whisper: str
    kokoro: Path
    embed: str
    speech_device_index: int
    device: str = "cuda"  # "cpu" on a home PC without an NVIDIA GPU


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
        offline = pip + ["--no-index", "--find-links", str(cache / "wheels")]
        if subprocess.run(offline + PIP_PACKAGES, capture_output=True).returncode == 0:
            return
        # Cache built before a package was added: install what it has, fetch the rest.
        subprocess.run(offline + FIRST_CACHE_PACKAGES, capture_output=True)
        print("[setup] a few packages are not in the model cache; downloading them")
    subprocess.run(pip + PIP_PACKAGES, check=True)


def install_ngrok(cache: Path | None) -> None:
    target = Path("/usr/local/bin/ngrok")
    if target.exists():
        return
    if cache and (cache / "ngrok" / "ngrok").exists():
        shutil.copy(cache / "ngrok" / "ngrok", target)  # chmod below restores "executable"
    else:
        sh(f"curl -fsSL {NGROK_URL} | tar -xz -C /usr/local/bin")
    target.chmod(0o755)


def _install_ollama_online() -> None:
    # The Ollama installer now needs "zstd" to unpack; Kaggle machines may not have it.
    for cmd in ("command -v zstd || apt-get install -y -qq zstd "
                "|| (apt-get update -qq && apt-get install -y -qq zstd)",
                "curl -fsSL https://ollama.com/install.sh | sh"):
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=6 * 60)
        if result.returncode != 0:
            raise RuntimeError(f"Ollama install failed: {result.stderr.strip()[-300:]}")


def _install_ollama_from_cache(cache: Path) -> None:
    shutil.copy(cache / "ollama" / "bin" / "ollama", "/usr/local/bin/ollama")
    os.chmod("/usr/local/bin/ollama", 0o755)
    # Copied, not linked: /kaggle/input may not allow loading libraries from it.
    # cuda_v13 is skipped: Kaggle's T4 driver uses cuda_v12.
    shutil.copytree(cache / "ollama" / "lib" / "ollama", "/usr/local/lib/ollama",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("cuda_v13*"))
    # Kaggle's saved output drops the "executable" permission, but Ollama runs
    # helper programs from this folder (e.g. llama-server).
    sh("chmod -R a+rx /usr/local/lib/ollama")


def install_ollama(cache: Path | None) -> Path:
    """Install the Ollama program. Returns the (local) models folder.

    Internet first: Kaggle's network is fast and steady (~20 s), while reading the same
    files from the model cache (/kaggle/input) took anywhere from 33 s to 275 s."""
    models_dir = config.WORK_DIR / "ollama-models"
    models_dir.mkdir(parents=True, exist_ok=True)
    if Path("/usr/local/bin/ollama").exists():
        return models_dir
    sources = [("internet", _install_ollama_online)]
    if cache and (cache / "ollama" / "bin" / "ollama").exists():
        sources.append(("model cache", lambda: _install_ollama_from_cache(cache)))
    if not config.DOWNLOAD_FIRST:
        sources.reverse()
    for i, (name, install) in enumerate(sources):
        try:
            install()
            return models_dir
        except Exception as e:
            if i == len(sources) - 1:
                raise
            print(f"[setup] Ollama from the {name} failed ({type(e).__name__}); trying the next source")
    return models_dir


def _link_cached_llm(cache: Path | None, models_dir: Path) -> bool:
    """Make the model cache's LLM files visible to Ollama (a tree of symlinks: instant)."""
    source = cache / "ollama" / "models" if cache else None
    if not source or not source.is_dir():
        return False
    sh(f"cp -rsf '{source}/.' '{models_dir}/'")
    return True


_ollama_models_dir: Path | None = None


def start_ollama(models_dir: Path | None = None) -> None:
    global _ollama_models_dir
    _ollama_models_dir = models_dir or _ollama_models_dir
    # The online installer may start its own Ollama service; ours needs the port.
    subprocess.run("systemctl stop ollama 2>/dev/null; pkill -f 'ollama serve'; true", shell=True)
    time.sleep(1)
    env = {**os.environ,
           "CUDA_VISIBLE_DEVICES": config.OLLAMA_GPU,
           "OLLAMA_MODELS": str(_ollama_models_dir),
           "OLLAMA_KEEP_ALIVE": "60m",
           "OLLAMA_MAX_LOADED_MODELS": "2",  # base + fine-tuned during the A/B test
           # 2 slots: the fact-learning call after a reply doesn't wipe the chat's
           # reusable prompt, so the next question starts faster.
           "OLLAMA_NUM_PARALLEL": "2",
           # Off by default: Ollama enables it itself only on Ampere+ GPUs, and on
           # older cards like the T4 it can be slower. Try it via config and compare
           # the [turn] speed lines.
           "OLLAMA_FLASH_ATTENTION": "1" if config.OLLAMA_FLASH_ATTENTION else "0",
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


def ollama_log_tail(lines: int = 25) -> str:
    """Last lines of Ollama's own log (server messages only, no conversations)."""
    try:
        return "\n".join((config.WORK_DIR / "ollama.log").read_text(errors="ignore")
                         .splitlines()[-lines:])
    except OSError:
        return "(no Ollama log)"


def ollama_devices() -> list[str]:
    """What Ollama will compute on, from its start-up log, e.g. ["cuda: Tesla T4"]."""
    try:
        log = (config.WORK_DIR / "ollama.log").read_text(errors="ignore")
    except OSError:
        return []
    devices = []
    for line in log.splitlines():
        if 'msg="inference compute"' in line:
            fields = {k: v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|\S+)', line)}
            devices.append(f"{fields.get('library', '?').lower()}: "
                           f"{fields.get('description') or fields.get('name', '?')}")
    return devices


def reinstall_ollama_online() -> None:
    """Fallback when the cached Ollama doesn't work: fresh install, then restart."""
    subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
    shutil.rmtree("/usr/local/lib/ollama", ignore_errors=True)
    Path("/usr/local/bin/ollama").unlink(missing_ok=True)
    _install_ollama_online()
    start_ollama()


def _pull(model: str) -> bool:
    env = {**os.environ, "OLLAMA_HOST": config.OLLAMA_URL.removeprefix("http://")}
    try:  # a hanging download must not block the start forever
        return subprocess.run(["ollama", "pull", model], env=env, capture_output=True,
                              timeout=12 * 60).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def ensure_llm(cache: Path | None = None, models_dir: Path | None = None) -> str:
    """Get the LLM onto the local disk: download it (fast, steady) or, if that fails,
    use the model cache. Falls back to the smaller model if needed."""
    import llm
    t = time.time()
    for model in (config.LLM_MODEL, config.LLM_FALLBACK_MODEL):
        if llm.is_ready(model):
            return model
        if not config.DOWNLOAD_FIRST and models_dir and _link_cached_llm(cache, models_dir) \
                and llm.is_ready(model):
            return model
        if _pull(model):
            print(f"[setup] {model} downloaded in {time.time() - t:.0f}s")
            return model
        print(f"[setup] could not download {model}; trying the model cache")
        if models_dir and _link_cached_llm(cache, models_dir) and llm.is_ready(model):
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


def _download_speech_models(base: Path | None = None,
                            whisper_repo: str | None = None) -> tuple[str, Path, str]:
    from huggingface_hub import snapshot_download
    base = base or config.WORK_DIR / "hf"
    whisper = snapshot_download(whisper_repo or config.WHISPER_REPO,
                                local_dir=base / "whisper")
    kokoro = snapshot_download(config.KOKORO_REPO, local_dir=base / "kokoro",
                               allow_patterns=["config.json", "*.pth",
                                               *[f"voices/{v}.pt" for _, v in config.TTS_VOICES.values()]])
    embed = snapshot_download(config.EMBED_MODEL, local_dir=base / "embed",
                              ignore_patterns=["*.onnx", "onnx/*", "*.h5", "*.msgpack", "*.ot",
                                               "openvino/*"])
    return whisper, Path(kokoro), embed


def model_paths(cache: Path | None) -> ModelPaths:
    """Speech + embedding model folders: downloaded (fast, steady) or from the cache."""
    import torch
    speech_gpu = config.SPEECH_GPU_INDEX if torch.cuda.device_count() > 1 else 0
    hf = cache / "hf" if cache else None
    cached = (str(hf / "whisper"), hf / "kokoro", str(hf / "embed")) \
        if hf and (hf / "whisper").is_dir() else None
    if cached and not config.DOWNLOAD_FIRST:
        return ModelPaths(*cached, speech_gpu)
    t = time.time()
    try:
        paths = _download_speech_models()
        print(f"[setup] speech models downloaded in {time.time() - t:.0f}s")
        return ModelPaths(*paths, speech_gpu)
    except Exception as e:
        if not cached:
            raise
        print(f"[setup] speech model download failed ({type(e).__name__}); using the model cache")
        return ModelPaths(*cached, speech_gpu)


def prepare():
    """Run everything. Returns (speech model paths, LLM job): the job is a Future that
    gives the LLM model name once it is downloaded, loaded and warmed up."""
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    cache = find_input(config.MODELS_MARKER)
    if cache:
        info = json.loads((cache / config.MODELS_MARKER).read_text())
        print(f"[setup] using model cache from {info.get('built', '?')}")
        if info.get("llm") != config.LLM_MODEL:
            print(f"[setup] note: cache has {info.get('llm')}, config wants {config.LLM_MODEL}")
    else:
        print("[setup] no model cache attached (see README step 5); everything will download")

    steps, t = [], time.time()

    def step(name: str) -> None:  # how long each part took, to find slow steps
        nonlocal t
        steps.append(f"{name} {time.time() - t:.0f}s")
        t = time.time()

    install_packages(cache)
    step("packages")
    install_ngrok(cache)
    step("ngrok")
    models_dir = install_ollama(cache)
    step("ollama install")
    start_ollama(models_dir)
    step("ollama start")
    # The LLM gets downloaded, loaded onto GPU 0 and warmed up in the background, while
    # the speech models download here (and then load onto GPU 1 in main.py).
    pool = ThreadPoolExecutor(max_workers=1)
    llm_job = pool.submit(_prepare_llm, cache, models_dir)
    pool.shutdown(wait=False)
    paths = model_paths(cache)
    step("speech downloads")
    print(f"[setup] steps: {', '.join(steps)}")
    return paths, llm_job


def _prepare_llm(cache: Path | None, models_dir: Path) -> str:
    """Download + load + first answer of the LLM (the first answer is slow: GPU start-up)."""
    import llm
    model = ensure_llm(cache, models_dir)
    t = time.time()
    llm.load_model(model)
    print(f"[setup] {model} loaded in {time.time() - t:.0f}s")
    t = time.time()
    "".join(llm.stream_reply([{"role": "user", "content": "Say OK."}], model, max_tokens=5))
    print(f"[setup] {model} first answer in {time.time() - t:.0f}s")
    return model
