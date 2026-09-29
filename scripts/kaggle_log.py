"""Show the status and the end of the log of the last Jarvis run on Kaggle.

Used by the "Jarvis log" button (.github/workflows/jarvis-log.yml), so the log can be read
without opening Kaggle. Jarvis's own log lines never contain conversations or secrets
(timings and counts only); lines that look like a secret are masked anyway.

    python scripts/kaggle_log.py [number of lines, default 200]
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SECRETISH = re.compile(r"(tskey-[\w-]+|KGAT_\w+|\b[A-Za-z0-9_-]{40,}\b)")


def kaggle(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kaggle", *args], capture_output=True, text=True)


def log_lines(raw: str) -> list[str]:
    """Kaggle's log file is a JSON list of {"stream_name", "time", "data"} (or plain text)."""
    try:
        entries = json.loads(raw)
    except ValueError:
        return raw.splitlines()
    text = "".join(str(e.get("data", "")) for e in entries if isinstance(e, dict))
    return text.splitlines()


def main() -> None:
    lines = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    kernel = f"{os.environ['KAGGLE_USERNAME'].strip()}/{os.environ.get('JARVIS_NOTEBOOK', 'jarvis')}"
    status = kaggle("kernels", "status", kernel)
    print("STATUS:", (status.stdout or status.stderr).strip())
    with tempfile.TemporaryDirectory() as tmp:
        result = kaggle("kernels", "output", kernel, "-p", tmp)
        logs = [p for p in Path(tmp).rglob("*.log")]
        if not logs:
            print("No log file came back:", (result.stdout + result.stderr).strip()[-500:])
            return
        text = log_lines(logs[0].read_text(errors="ignore"))
    print(f"--- last {min(lines, len(text))} of {len(text)} log lines ---")
    for line in text[-lines:]:
        print(SECRETISH.sub("***", line))


if __name__ == "__main__":
    main()
