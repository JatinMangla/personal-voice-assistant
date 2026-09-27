"""Start Jarvis on Kaggle without opening the notebook editor (no GPU wasted).

Used by the "Start Jarvis" button (.github/workflows/start-jarvis.yml), and it also
works on a laptop:

    set KAGGLE_USERNAME=... and KAGGLE_API_TOKEN=...   (or have kaggle.json)
    python scripts/start_jarvis.py

It pushes notebooks/assistant.ipynb as a new version of your Jarvis notebook with
GPU T4 x2 + Internet, attached to the LATEST code dataset and model cache, and
Kaggle runs it ("Save & Run All"). If Jarvis is already running, it does nothing.

Names can be changed with environment variables (defaults in brackets):
    JARVIS_NOTEBOOK   notebook slug from its address kaggle.com/code/<user>/<slug> [jarvis]
    JARVIS_CODE       code dataset slug                                   [assistant-code]
    JARVIS_CACHE      model cache notebook slug                           [build-model-cache]
    JARVIS_EXTRA      more notebook slugs to attach, comma separated,
                      e.g. your fine-tune notebook after Phase 5          []
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def settings() -> dict:
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    if not user:
        sys.exit("Set KAGGLE_USERNAME (and KAGGLE_API_TOKEN).")
    extra = [s.strip() for s in os.environ.get("JARVIS_EXTRA", "").split(",") if s.strip()]
    return {
        "user": user,
        "notebook": os.environ.get("JARVIS_NOTEBOOK", "jarvis").strip(),
        "code": os.environ.get("JARVIS_CODE", "assistant-code").strip(),
        "cache": os.environ.get("JARVIS_CACHE", "build-model-cache").strip(),
        "extra": extra,
    }


def kernel_metadata(s: dict) -> dict:
    user = s["user"]
    return {
        "id": f"{user}/{s['notebook']}",
        "title": "Jarvis",
        "code_file": "assistant.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "machine_shape": "NvidiaTeslaT4",  # GPU T4 x2
        "enable_internet": True,
        "dataset_sources": [f"{user}/{s['code']}"],
        "kernel_sources": [f"{user}/{s['cache']}"] + [f"{user}/{e}" for e in s["extra"]],
        "competition_sources": [],
        "model_sources": [],
    }


def is_running(status_output: str) -> bool:
    text = status_output.lower()
    return "running" in text or "queued" in text


def kaggle(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kaggle", *args], capture_output=True, text=True)


def main() -> None:
    s = settings()
    kernel = f"{s['user']}/{s['notebook']}"

    status = kaggle("kernels", "status", kernel)
    if status.returncode == 0 and is_running(status.stdout):
        print(f"Jarvis is already running ({status.stdout.strip()}). Open your link.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(ROOT / "notebooks" / "assistant.ipynb", Path(tmp) / "assistant.ipynb")
        (Path(tmp) / "kernel-metadata.json").write_text(json.dumps(kernel_metadata(s), indent=1))
        result = kaggle("kernels", "push", "-p", tmp)
    print((result.stdout + result.stderr).strip())
    if result.returncode != 0:
        sys.exit("Could not start Jarvis. Check the notebook name (JARVIS_NOTEBOOK) and token.")
    print("Started. Jarvis is ready in about 4-5 minutes; then open your link.")


if __name__ == "__main__":
    main()
