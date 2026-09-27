"""Publish the local app at your fixed ngrok URL, behind Google sign-in.

Only the Google accounts listed in ALLOWED_EMAIL get through. Everyone else is
stopped at ngrok's servers and never reaches this notebook.
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import config

NGROK_BIN = "/usr/local/bin/ngrok"


def traffic_policy(allowed_emails: list[str]) -> dict:
    return {
        "on_http_request": [
            {"actions": [{"type": "oauth", "config": {"provider": "google"}}]},
            {
                "expressions": [
                    f"!(actions.ngrok.oauth.identity.email in {json.dumps(allowed_emails)})"
                ],
                "actions": [{"type": "deny", "config": {"status_code": 403}}],
            },
        ]
    }


def start(authtoken: str, domain: str, allowed_emails: list[str] | None) -> subprocess.Popen:
    """Start the ngrok agent. Without allowed_emails there is no Google sign-in
    and the app's own password page (fallback mode) protects it instead."""
    domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
    args = [NGROK_BIN, "http", f"{config.APP_HOST}:{config.APP_PORT}",
            "--url", f"https://{domain}", "--log", "stdout", "--log-format", "json"]
    if allowed_emails:
        policy_file = config.WORK_DIR / "ngrok-policy.json"
        policy_file.write_text(json.dumps(traffic_policy(allowed_emails)))
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
