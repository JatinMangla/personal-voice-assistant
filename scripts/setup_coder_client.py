"""Connect this machine's Claude Code to the Coder, in one command.

    python scripts/setup_coder_client.py

It is safe to run twice: every step checks before it changes anything. It:
  1. makes a .venv (if there is none) and installs the three packages the tools need;
  2. writes .mcp.json with the right python path FOR THIS operating system
     (Windows uses .venv\\Scripts\\python.exe, macOS and Linux use .venv/bin/python);
  3. stores CODER_KEY so it survives a reboot (Windows: a user variable via setx;
     macOS/Linux: a line in your shell profile), asking for it only if it is not already set;
  4. builds the local code index, which needs no GPU and no network;
  5. checks the link to the Coder and prints the key fingerprint, so a wrong key is obvious.

The Coder model itself runs on Kaggle's free GPU, not here: this only sets up the client.
Nothing here needs admin rights, and nothing is sent anywhere except to your own Coder.
"""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ["numpy", "httpx", "mcp"]
DEFAULT_URL = "https://volley-gnarly-constable.ngrok-free.dev"
WINDOWS = os.name == "nt"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,}$")   # what scripts/make_key.py prints
PROFILES = (".zshrc", ".bashrc", ".bash_profile", ".profile")


def say(step: str, detail: str = "") -> None:
    print(f"[setup] {step}" + (f": {detail}" if detail else ""), flush=True)


def venv_python() -> Path:
    """Where this OS keeps the venv's interpreter."""
    return ROOT / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def ensure_venv() -> Path:
    python = venv_python()
    if python.exists():
        say("virtual environment", "already there")
        return python
    say("virtual environment", "creating .venv")
    subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    if not python.exists():
        sys.exit(f"[setup] FAILED: no interpreter at {python}")
    return python


def ensure_packages(python: Path) -> None:
    missing = []
    for name in PACKAGES:
        probe = subprocess.run([str(python), "-c", f"import {name}"], capture_output=True)
        if probe.returncode:
            missing.append(name)
    if not missing:
        say("packages", "numpy, httpx and mcp are installed")
        return
    say("packages", "installing " + ", ".join(missing))
    subprocess.run([str(python), "-m", "pip", "install", "-q", *missing], check=True)


def write_mcp_config(python: Path) -> None:
    """.mcp.json, with a python path that works on THIS system (the repo's committed copy is
    Windows-only, which is the single thing that breaks the setup on macOS and Linux)."""
    path = ROOT / ".mcp.json"
    relative = python.relative_to(ROOT).as_posix()
    config = {"mcpServers": {"coder": {
        "command": relative,
        "args": ["scripts/coder_mcp.py"],
        "env": {"CODER_URL": "${CODER_URL:-" + DEFAULT_URL + "}", "CODER_KEY": "${CODER_KEY:-}"}}}}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            current = None
        if current == config:
            say(".mcp.json", f"already correct for this system ({relative})")
            return
        existing = current.get("mcpServers", {}) if isinstance(current, dict) else {}
        for name, server in existing.items():        # keep any other MCP servers the user added
            config["mcpServers"].setdefault(name, server)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    say(".mcp.json", f"written for this system ({relative})")


def ask_for_key() -> str:
    print("\n[setup] CODER_KEY is not set on this machine.")
    print("        It is the key from your password manager (the same one as the GitHub secret).")
    print("        Paste it and press Enter, or press Enter alone to skip and set it later.")
    try:
        value = input("        CODER_KEY: ").strip().lstrip("﻿").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if value and not KEY_PATTERN.match(value):
        print("[setup] That does not look like a key from make_key.py (letters, digits, - and _,"
              " at least 32 characters). Nothing was saved; run this again.")
        return ""
    return value


def remember_key(key: str) -> None:
    """Store the key so a new terminal still has it, without needing admin rights."""
    if WINDOWS:
        subprocess.run(["setx", "CODER_KEY", key], check=True, capture_output=True)
        say("CODER_KEY", "saved as a user variable (reopen your editor to pick it up)")
        return
    home = Path.home()
    profile = next((home / name for name in PROFILES if (home / name).exists()), home / ".profile")
    line = f'export CODER_KEY="{key}"'
    text = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if "CODER_KEY" in text:
        say("CODER_KEY", f"already mentioned in {profile.name}; left it alone")
        return
    with profile.open("a", encoding="utf-8") as handle:
        handle.write(f"\n# the Jarvis Coder helper for Claude Code\n{line}\n")
    say("CODER_KEY", f"added to {profile.name} (open a new terminal to pick it up)")


def build_index(python: Path) -> None:
    """The local index: no GPU, no network. Without it the first question would be slow."""
    say("code index", "reading the allowed folders")
    code = ("import sys; sys.path.insert(0, 'scripts')\n"
            "from coder import service\n"
            "c = service.Coder(service.default_home(), service.default_roots(__import__('pathlib')"
            ".Path('.').resolve()), client=None)\n"
            "print('[setup] indexed:', c.index.refresh())\n"
            "print('[setup] totals:', c.index.stats())\n")
    result = subprocess.run([str(python), "-c", code], cwd=ROOT, capture_output=True, text=True)
    print((result.stdout or result.stderr).strip()[:1500])
    if result.returncode:
        sys.exit("[setup] FAILED to build the index (see the message above).")


def check_link(python: Path, key: str) -> None:
    say("the Coder link", "checking")
    code = ("import sys; sys.path.insert(0, 'scripts')\n"
            "from coder.client import CoderClient\n"
            "c = CoderClient.from_env()\n"
            "print('[setup] ' + (c.link_status() if c else 'CODER_URL/CODER_KEY not in this shell'))\n")
    env = {**os.environ, "CODER_KEY": key} if key else dict(os.environ)
    env.setdefault("CODER_URL", DEFAULT_URL)
    result = subprocess.run([str(python), "-c", code], cwd=ROOT, capture_output=True, text=True,
                            env=env)
    print((result.stdout or result.stderr).strip()[:800])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", default="", help="CODER_KEY, instead of being asked for it")
    parser.add_argument("--no-save-key", action="store_true",
                        help="use the key for this check only, do not store it")
    parser.add_argument("--skip-index", action="store_true", help="do not build the index now")
    args = parser.parse_args()

    print(f"[setup] {platform.system()} {platform.release()}, python {sys.version.split()[0]}")
    print(f"[setup] project: {ROOT}")
    python = ensure_venv()
    ensure_packages(python)
    write_mcp_config(python)

    key = args.key.strip().lstrip("﻿").strip() or os.environ.get("CODER_KEY", "").strip()
    if key:
        say("CODER_KEY", "found" + (" (given on the command line)" if args.key else
                                    " in this shell's environment"))
        if args.key and not args.no_save_key:
            remember_key(key)
    else:
        key = ask_for_key()
        if key and not args.no_save_key:
            remember_key(key)

    if not args.skip_index:
        build_index(python)
    if key:
        check_link(python, key)
    else:
        say("the Coder link", "not checked: no CODER_KEY yet")

    print("\n[setup] done. What happens next:")
    print("  - Reopen your editor (so it reads .mcp.json and the saved CODER_KEY).")
    print("  - Ask Claude Code to run `coder_status`. Offline is normal: the model only runs")
    print("    while a Kaggle run is up (GitHub -> Actions -> Start Coder).")
    print("  - `search_code` and `repo_map` work offline, with no GPU and no network.")
    print("  - To ask about another project too, set CODER_ALLOWED_DIRS to the folders you want,")
    print("    separated by ';' (see the README, 'Coder helper').")


if __name__ == "__main__":
    main()
