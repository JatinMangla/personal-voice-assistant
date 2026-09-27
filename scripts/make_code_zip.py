"""Build assistant-code.zip (the src folder) for uploading to Kaggle.

    python scripts/make_code_zip.py

Use this instead of Windows "Compress-Archive": that writes paths with backslashes,
which Kaggle (Linux) does not unpack into folders.
"""
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
out = root / "assistant-code.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted((root / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            z.write(path, path.relative_to(root).as_posix())
print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
