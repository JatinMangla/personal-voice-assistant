"""Start the Coder helper on Kaggle without opening the notebook editor.

Used by the "Start Coder" button (.github/workflows/start-coder.yml); also works from a laptop
with the secrets below set as environment variables:

    python scripts/start_coder.py

Same method as scripts/start_jarvis.py (encrypted one-time secrets, free-limit check BEFORE a
GPU machine is started), with these differences:
- it pushes notebooks/coder.ipynb as the notebook `coder` (CODER_NOTEBOOK to change the name);
- it refuses while Jarvis is running (they share one ngrok address and one memory file);
- it needs the CODER_KEY secret (make one with: python scripts/make_key.py).
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import start_jarvis as sj  # noqa: E402

KEY_CHARS = re.compile(r"[A-Za-z0-9_=-]+")   # what make_key.py prints (URL-safe base64)
REQUIRED = ["KAGGLE_USERNAME", "KAGGLE_API_TOKEN", "MEMORY_KEY", "NGROK_AUTHTOKEN",
            "NGROK_DOMAIN", "CODER_KEY"]
OPTIONAL = ["CODER_FORCE"]
MIN_KEY_LENGTH = 32


def settings() -> dict:
    s = sj.settings()
    s["notebook"] = os.environ.get("CODER_NOTEBOOK", "coder").strip()
    s["jarvis"] = os.environ.get("JARVIS_NOTEBOOK", "jarvis").strip()
    return s


def collect_secrets(force: bool = False) -> dict:
    # "\ufeff" is the invisible byte-order mark that PowerShell can put in front of piped text
    values = {n: os.environ.get(n, "").strip().lstrip("\ufeff").strip() for n in REQUIRED + OPTIONAL}
    missing = [n for n in REQUIRED if not values[n]]
    if missing:
        sys.exit("These GitHub repository secrets are missing: " + ", ".join(missing) + "\n"
                 "Add them in: repo -> Settings -> Secrets and variables -> Actions -> 'New "
                 "repository secret'. CODER_KEY: run  python scripts/make_key.py  and use the "
                 "printed text (keep a copy in your password manager).")
    if len(values["CODER_KEY"]) < MIN_KEY_LENGTH:
        sys.exit("CODER_KEY is too short to be safe. Make one with: python scripts/make_key.py")
    if not KEY_CHARS.fullmatch(values["CODER_KEY"]):
        sys.exit("CODER_KEY contains a character that make_key.py never prints (a hidden or "
                 "special character, often from copy and paste). Set the secret again with exactly "
                 "the text from: python scripts/make_key.py")
    if force:
        values["CODER_FORCE"] = "1"      # the notebook then skips its own 'Jarvis is running' check
    return {k: v for k, v in values.items() if v}


def kernel_metadata(s: dict) -> dict:
    user = s["user"]
    return {
        "id": f"{user}/{s['notebook']}",
        "title": "Coder",
        "code_file": "coder.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "machine_shape": "NvidiaTeslaT4",  # GPU T4 x2
        "enable_internet": True,
        "dataset_sources": [f"{user}/{s['code']}", f"{user}/{sj.SECRETS_SLUG}"],
        "kernel_sources": [],
        "competition_sources": [],
        "model_sources": [],
    }


def notebook_with_key(run_key: str) -> str:
    text = (ROOT / "notebooks" / "coder.ipynb").read_text(encoding="utf-8")
    placeholder = json.dumps(sj.RUN_KEY_LINE)[1:-1]
    if placeholder not in text:
        sys.exit("notebooks/coder.ipynb has no RUN_KEY line; update it from the repo.")
    return text.replace(placeholder, json.dumps(f'RUN_KEY = "{run_key}"')[1:-1], 1)


def running(user: str, slug: str) -> bool:
    status = sj.kaggle("kernels", "status", f"{user}/{slug}")
    return status.returncode == 0 and sj.is_running(status.stdout)


def main() -> None:
    s = settings()
    force = os.environ.get("CODER_FORCE_START", "").lower() in ("1", "true", "yes")
    secrets = collect_secrets(force)
    kernel = f"{s['user']}/{s['notebook']}"

    status = sj.kaggle("kernels", "status", kernel)
    print(f"Kaggle status of {kernel}: {(status.stdout or status.stderr).strip()}")
    if status.returncode == 0 and sj.is_running(status.stdout) and not force:
        sys.exit("The Coder helper is already running or queued, so NOTHING was started.\n"
                 "- If you started it in the last 5 minutes: it is still starting; wait.\n"
                 "- If it is stuck: press 'Run workflow' again with 'force' ticked.")
    if not force and running(s["user"], s["jarvis"]):
        sys.exit("Jarvis is running, so NOTHING was started. Coder and Jarvis share one address "
                 "and one memory file: close Jarvis first (say goodbye or wait for its 10-minute "
                 "close), then press Start Coder again. ('force' ticked skips this check.)")

    blocked = sj.limit_blocker(s["user"], secrets["MEMORY_KEY"])
    if blocked:
        sys.exit(blocked)

    run_key = sj.upload_secrets(s["user"], secrets)
    print("Encrypted secrets uploaded.")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "coder.ipynb").write_text(notebook_with_key(run_key), encoding="utf-8")
        (Path(tmp) / "kernel-metadata.json").write_text(json.dumps(kernel_metadata(s), indent=1))
        for attempt in range(1, 4):
            result = sj.kaggle("kernels", "push", "-p", tmp)
            output = (result.stdout + result.stderr).strip()
            print(output)
            if sj.push_ok(result.returncode, output):
                print("Started. The Coder is ready in about 5-8 minutes; the Jarvis log button "
                      "shows the address and speed once it is up.")
                return
            if "concurrencyviolation" not in output.lower():
                break
            print(f"Kaggle reports a simultaneous change; retrying ({attempt}/3)...")
            time.sleep(10)
    sys.exit("Could not start the Coder. Close any open Kaggle editor tab of it and try again; "
             "also check the notebook name (CODER_NOTEBOOK) and the token.")


if __name__ == "__main__":
    main()
