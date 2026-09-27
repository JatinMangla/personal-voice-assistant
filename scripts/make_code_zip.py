"""Build assistant-code.zip (the src folder) for uploading to Kaggle.

    python scripts/make_code_zip.py

Use this instead of Windows "Compress-Archive": that writes paths with backslashes,
which Kaggle (Linux) does not unpack into folders. (With the GitHub setup you don't
need this: every push uploads the code automatically, see scripts/upload_code.py.)
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(out: Path = ROOT / "assistant-code.zip") -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted((ROOT / "src").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(ROOT).as_posix())
    return out


if __name__ == "__main__":
    out = build()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
