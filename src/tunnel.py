"""Publish the local app at your fixed ngrok URL, behind Google sign-in.

Only the Google accounts listed in ALLOWED_EMAIL get through. Everyone else is
stopped at ngrok's servers and never reaches this notebook.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import config

NGROK_BIN = "/usr/local/bin/ngrok"  # Kaggle; at home, the ngrok found on PATH


BRIDGE_PATH = "/api/bridge/"
APP_HEADER = "x-jarvis-app"  # sent by the Jarvis Android app (ngrok header names are lowercase)


def traffic_policy(allowed_emails: list[str], bridge_path: bool = False,
                   app_header: bool = True) -> dict:
    """Google sign-in for everything, except (the app then checks its own key instead):
    - with bridge_path: the home helper's WebSocket path (a helper can't do a Google login);
    - with app_header: requests from the Android app (they carry X-Jarvis-App and must have
      a paired device's token, which app.py requires for every such request)."""
    skips = []
    if bridge_path:
        skips.append(f"!req.url.path.startsWith('{BRIDGE_PATH}')")
    if app_header:
        skips.append(f"!('{APP_HEADER}' in req.headers)")
    skip = " && ".join(skips)
    signed_out = f"!(actions.ngrok.oauth.identity.email in {json.dumps(allowed_emails)})"
    oauth = {"actions": [{"type": "oauth", "config": {"provider": "google"}}]}
    deny = {"expressions": [signed_out], "actions": [{"type": "deny", "config": {"status_code": 403}}]}
    if skip:
        oauth["expressions"] = [skip]
        deny["expressions"] = [f"{skip} && {signed_out}"]
    return {"on_http_request": [oauth, deny]}


def start(authtoken: str, domain: str, allowed_emails: list[str] | None,
          bridge_path: bool = False) -> subprocess.Popen:
    """Start the ngrok agent. Without allowed_emails there is no Google sign-in
    and the app's own password page (fallback mode) protects it instead."""
    domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
    binary = NGROK_BIN if os.path.exists(NGROK_BIN) else (shutil.which("ngrok") or NGROK_BIN)
    args = [binary, "http", f"{config.APP_HOST}:{config.APP_PORT}",
            "--url", f"https://{domain}", "--log", "stdout", "--log-format", "json"]
    if allowed_emails:
        policy_file = config.WORK_DIR / "ngrok-policy.json"
        policy_file.write_text(json.dumps(traffic_policy(allowed_emails, bridge_path)))
        args += ["--traffic-policy-file", str(policy_file)]

    log_path = config.WORK_DIR / "ngrok.log"
    proc = subprocess.Popen(args, env={**os.environ, "NGROK_AUTHTOKEN": authtoken},
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    _wait_until_online(proc, log_path)
    return proc


def _wait_until_online(proc: subprocess.Popen, log_path: Path, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("ngrok stopped:\n" + _log_errors(log_path))
        try:  # the agent's local API lists running tunnels
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                if json.load(r).get("tunnels"):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("ngrok did not start in time:\n" + _log_errors(log_path))


def _log_errors(log_path: Path) -> str:
    lines = log_path.read_text(errors="ignore").splitlines()
    errors = [l for l in lines if '"lvl":"eror"' in l or '"err"' in l]
    return "\n".join((errors or lines)[-5:])
