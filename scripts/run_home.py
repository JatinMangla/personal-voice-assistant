"""Run Jarvis on your own PC (roadmap stage A): no cloud, no GPU-hour limits, always on.

    python scripts/run_home.py

Secrets come from environment variables or from the file PVA_HOME_DIR/secrets.env
(default: a ".jarvis" folder in your user folder), one per line:

    MEMORY_KEY=...          required (python scripts/make_key.py makes one)
    NGROK_AUTHTOKEN=...     optional: reach Jarvis from your phone, anywhere
    NGROK_DOMAIN=...        (with the token)
    ALLOWED_EMAIL=...       Google sign-in at ngrok (or APP_PASSWORD=...)
    NTFY_TOPIC=...          optional: reminder notifications on your phone

Without ngrok, open http://127.0.0.1:8000 on this computer (microphones only work on
https pages or on the computer itself). Your memory is the encrypted file
PVA_HOME_DIR/memory.db.enc; keep MEMORY_KEY safe, it can't be recovered.
To move your memory from Kaggle: download memory.db.enc from your private Kaggle
dataset "assistant-memory" into that folder (same MEMORY_KEY).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_secrets_file(path: Path) -> int:
    """KEY=VALUE lines into the environment (real environment variables win)."""
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name and value and not os.environ.get(name):
            os.environ[name] = value.strip('"').strip("'")
            count += 1
    return count


def main() -> None:
    os.environ["PVA_HOSTING"] = "home"  # before config is imported
    home = Path(os.environ.get("PVA_HOME_DIR") or Path.home() / ".jarvis")
    home.mkdir(parents=True, exist_ok=True)
    found = load_secrets_file(home / "secrets.env")
    print(f"[home] folder {home}; {found} secret(s) read from secrets.env")  # names only
    sys.path.insert(0, str(ROOT / "src"))
    import main as jarvis
    jarvis.run()


if __name__ == "__main__":
    main()
