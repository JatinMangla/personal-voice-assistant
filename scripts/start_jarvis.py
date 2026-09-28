"""Start Jarvis on Kaggle without opening the notebook editor (no GPU wasted).

Used by the "Start Jarvis" button (.github/workflows/start-jarvis.yml), and it also
works on a laptop (with the secrets below set as environment variables):

    python scripts/start_jarvis.py

It pushes notebooks/assistant.ipynb as a new version of your Jarvis notebook with
GPU T4 x2 + Internet, attached to the LATEST code dataset and model cache, and
Kaggle runs it ("Save & Run All"). If Jarvis is already running, it does nothing.

Secrets: Kaggle gives no Kaggle Secrets to runs started this way. So this script
encrypts the secrets with a NEW random one-time key, uploads them as the private
dataset "jarvis-secrets" (replacing the previous upload), and puts the one-time key
into the notebook it starts. Either part alone reveals nothing; a key is useless
after the next start.

Names can be changed with environment variables (defaults in brackets):
    JARVIS_NOTEBOOK   notebook slug from its address kaggle.com/code/<user>/<slug> [jarvis]
    JARVIS_CODE       code dataset slug                                   [assistant-code]
    JARVIS_CACHE      model cache notebook slug                           [build-model-cache]
    JARVIS_EXTRA      more notebook slugs to attach, comma separated,
                      e.g. your fine-tune notebook after Phase 5          []
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SECRETS_SLUG = "jarvis-secrets"
SECRETS_FILE = "jarvis-secrets.enc"  # same name as config.SECRETS_BUNDLE_FILE
# What the notebook needs. Required ones must be set as GitHub repository secrets.
REQUIRED = ["KAGGLE_USERNAME", "KAGGLE_API_TOKEN", "MEMORY_KEY", "NGROK_AUTHTOKEN",
            "NGROK_DOMAIN"]
OPTIONAL = ["ALLOWED_EMAIL", "APP_PASSWORD", "START_URL"]
RUN_KEY_LINE = 'RUN_KEY = ""'


def settings() -> dict:
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    if not user or not (os.environ.get("KAGGLE_API_TOKEN") or os.environ.get("KAGGLE_KEY")):
        sys.exit("KAGGLE_USERNAME or KAGGLE_API_TOKEN is empty.\n"
                 "On GitHub: repo -> Settings -> Secrets and variables -> Actions -> 'Secrets' "
                 "tab -> 'New repository secret' (not Codespaces/Dependabot/Environment). Add "
                 "KAGGLE_USERNAME and KAGGLE_API_TOKEN, then re-run.")
    extra = [s.strip() for s in os.environ.get("JARVIS_EXTRA", "").split(",") if s.strip()]
    return {
        "user": user,
        "notebook": os.environ.get("JARVIS_NOTEBOOK", "jarvis").strip(),
        "code": os.environ.get("JARVIS_CODE", "assistant-code").strip(),
        "cache": os.environ.get("JARVIS_CACHE", "build-model-cache").strip(),
        "extra": extra,
    }


def collect_secrets() -> dict:
    values = {n: os.environ.get(n, "").strip() for n in REQUIRED + OPTIONAL}
    missing = [n for n in REQUIRED if not values[n]]
    if not values["ALLOWED_EMAIL"] and not values["APP_PASSWORD"]:
        missing.append("ALLOWED_EMAIL")
    if missing:
        sys.exit("These GitHub repository secrets are missing: " + ", ".join(missing) + "\n"
                 "Add them in: repo -> Settings -> Secrets and variables -> Actions -> "
                 "'New repository secret' (same values as your Kaggle secrets), then re-run.")
    return {k: v for k, v in values.items() if v}


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
        "dataset_sources": [f"{user}/{s['code']}", f"{user}/{SECRETS_SLUG}"],
        "kernel_sources": [f"{user}/{s['cache']}"] + [f"{user}/{e}" for e in s["extra"]],
        "competition_sources": [],
        "model_sources": [],
    }


def notebook_with_key(run_key: str) -> str:
    """The Jarvis notebook with this run's one-time key filled in."""
    text = (ROOT / "notebooks" / "assistant.ipynb").read_text(encoding="utf-8")
    placeholder = json.dumps(RUN_KEY_LINE)[1:-1]  # as it appears inside the JSON
    if placeholder not in text:
        sys.exit("notebooks/assistant.ipynb has no RUN_KEY line; update it from the repo.")
    return text.replace(placeholder, json.dumps(f'RUN_KEY = "{run_key}"')[1:-1], 1)


def is_running(status_output: str) -> bool:
    text = status_output.lower()
    return "running" in text or "queued" in text


def link_is_online(domain: str) -> bool | None:
    """Does the Jarvis address answer? None if unknown (no domain / network problem).
    ngrok marks "nothing is connected" answers with an ngrok-error-code header."""
    import urllib.error
    import urllib.request
    domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
    if not domain:
        return None

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None  # a redirect (to the Google sign-in) already means "online"

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(f"https://{domain}/",
                                     headers={"ngrok-skip-browser-warning": "1"})
    try:
        with opener.open(request, timeout=15) as resp:
            return not resp.headers.get("ngrok-error-code")
    except urllib.error.HTTPError as e:
        return not e.headers.get("ngrok-error-code")
    except OSError:
        return None


def push_ok(returncode: int, output: str) -> bool:
    """The kaggle tool can print an error but still exit with 0."""
    text = output.lower()
    return returncode == 0 and "error" not in text and "violation" not in text


def kaggle(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kaggle", *args], capture_output=True, text=True)


def upload_secrets(user: str, secrets: dict) -> str:
    """Encrypt the secrets with a fresh key, upload them, return the key (base64)."""
    import crypto
    run_key = crypto.generate_key()
    blob = crypto.encrypt(json.dumps(secrets).encode(), crypto.load_key(run_key))
    dataset = f"{user}/{SECRETS_SLUG}"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / SECRETS_FILE).write_bytes(blob)
        (Path(tmp) / "dataset-metadata.json").write_text(json.dumps(
            {"title": SECRETS_SLUG, "id": dataset, "licenses": [{"name": "other"}]}))
        if kaggle("datasets", "status", dataset).returncode == 0:
            result = kaggle("datasets", "version", "-p", tmp, "-m", "start",
                            "--delete-old-versions")
        else:
            result = kaggle("datasets", "create", "-p", tmp)  # private by default
    output = (result.stdout + result.stderr).strip()
    if not push_ok(result.returncode, output):
        sys.exit(f"Could not upload the encrypted secrets: {output[-300:]}")
    # Wait until Kaggle has processed it, so the run gets THIS upload.
    for _ in range(36):
        time.sleep(5)
        status = kaggle("datasets", "status", dataset)
        if "ready" in status.stdout.lower():
            time.sleep(10)
            return run_key
    sys.exit("Kaggle is slow to process the secrets upload; press Start Jarvis again.")


def main() -> None:
    s = settings()
    secrets = collect_secrets()
    kernel = f"{s['user']}/{s['notebook']}"

    status = kaggle("kernels", "status", kernel)
    print(f"Kaggle status of {kernel}: {(status.stdout or status.stderr).strip()}")
    force = os.environ.get("JARVIS_FORCE", "").lower() in ("1", "true", "yes")
    if status.returncode == 0 and is_running(status.stdout) and not force:
        online = link_is_online(secrets.get("NGROK_DOMAIN", ""))
        if online:
            print("Jarvis is already running and your link works. Open it.")
            return
        sys.exit(
            "Kaggle says Jarvis is running or queued, but your link is not online, so NOTHING "
            "was started.\n"
            "- If you started it in the last 5 minutes: it is still starting; wait.\n"
            "- If a Kaggle editor tab of Jarvis is open: stop that session (power icon), close "
            "the tab, and press Start again.\n"
            "- Otherwise an older run is stuck: press 'Run workflow' again with 'force' ticked.")

    run_key = upload_secrets(s["user"], secrets)
    print("Encrypted secrets uploaded.")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "assistant.ipynb").write_text(notebook_with_key(run_key), encoding="utf-8")
        (Path(tmp) / "kernel-metadata.json").write_text(json.dumps(kernel_metadata(s), indent=1))
        for attempt in range(1, 4):
            result = kaggle("kernels", "push", "-p", tmp)
            output = (result.stdout + result.stderr).strip()
            print(output)
            if push_ok(result.returncode, output):
                print("Started. Jarvis is ready in about 4-5 minutes; then open your link.")
                return
            if "concurrencyviolation" not in output.lower():
                break
            # The notebook changed at the same moment (usually an open Kaggle editor tab
            # saving its draft). Wait and try again.
            print(f"Kaggle reports a simultaneous change; retrying ({attempt}/3)...")
            time.sleep(10)
    sys.exit("Could not start Jarvis. Close any open Kaggle editor tab of Jarvis and try "
             "again; also check the notebook name (JARVIS_NOTEBOOK) and the token.")


if __name__ == "__main__":
    main()
