"""Upload src/ to Kaggle as a new version of the assistant-code dataset.

Runs automatically on GitHub after every push that changes src/
(.github/workflows/update-kaggle-code.yml), so you never upload by hand. Also works
on a laptop with KAGGLE_USERNAME and KAGGLE_API_TOKEN set:

    python scripts/upload_code.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_code_zip  # noqa: E402


def dataset_metadata(user: str, slug: str) -> dict:
    return {"title": slug, "id": f"{user}/{slug}", "licenses": [{"name": "other"}]}


def main() -> None:
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    if not user:
        sys.exit("Set KAGGLE_USERNAME (and KAGGLE_API_TOKEN).")
    slug = os.environ.get("JARVIS_CODE", "").strip() or "assistant-code"
    note = os.environ.get("GITHUB_SHA", "manual")[:12]
    with tempfile.TemporaryDirectory() as tmp:
        make_code_zip.build(Path(tmp) / "assistant-code.zip")
        (Path(tmp) / "dataset-metadata.json").write_text(json.dumps(dataset_metadata(user, slug)))
        result = subprocess.run(["kaggle", "datasets", "version", "-p", tmp, "-m", f"code {note}"],
                                capture_output=True, text=True)
    print((result.stdout + result.stderr).strip())
    if result.returncode != 0:
        sys.exit(f"Upload failed. Is the dataset really called '{user}/{slug}'?")
    print("Uploaded. Kaggle needs about a minute to process it; the next Jarvis start uses it.")


if __name__ == "__main__":
    main()
