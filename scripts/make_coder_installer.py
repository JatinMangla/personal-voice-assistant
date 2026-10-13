"""Build ONE self-contained file that connects any machine's Claude Code to the Coder.

    .venv\\Scripts\\python scripts\\make_coder_installer.py

Writes `dist/jarvis_coder_installer.py`: the whole coder client (the `coder/` package and
`coder_mcp.py`) embedded as a base64 zip inside a plain Python file. Copy that one file to any
computer -- an office VM, a borrowed laptop, a container -- run it with your CODER_KEY, and Claude
Code there gets the coder tools. No clone of this private repo, no GitHub access, no second secret.

The payload is GENERATED, never hand-maintained, so the installer can't drift from the source: run
this again after changing anything under scripts/coder/ and commit the result.
"""
import base64
import hashlib
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "scripts" / "coder"
ENTRY = ROOT / "scripts" / "coder_mcp.py"
OUT = ROOT / "dist" / "jarvis_coder_installer.py"
TEMPLATE = Path(__file__).resolve().parent / "coder_installer_template.py"
PAYLOAD_MARK = "PAYLOAD = \"\"\n"      # the line in the template that gets the zip
DIGEST_MARK = "PAYLOAD_SHA256 = \"\"\n"
WRAP = 116


def client_files() -> list[Path]:
    """Every file that has to travel: the package's modules plus the MCP entry point."""
    modules = sorted(p for p in CLIENT_DIR.glob("*.py"))
    if not modules:
        raise SystemExit(f"no client modules found in {CLIENT_DIR}")
    return modules + [ENTRY]


def build_payload(files: list[Path]) -> bytes:
    """A deterministic zip: same sources in, byte-identical zip out, so committing the installer
    does not churn the diff when nothing really changed."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            inside = ("coder/" + path.name) if path.parent == CLIENT_DIR else path.name
            info = zipfile.ZipInfo(inside, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return buffer.getvalue()


def main() -> None:
    files = client_files()
    payload = build_payload(files)
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    wrapped = "\n".join(encoded[i:i + WRAP] for i in range(0, len(encoded), WRAP))

    template = TEMPLATE.read_text(encoding="utf-8")
    for mark in (PAYLOAD_MARK, DIGEST_MARK):
        if mark not in template:
            raise SystemExit(f"the template has no {mark.strip()} line to fill in")
    text = template.replace(PAYLOAD_MARK, f'PAYLOAD = """\\\n{wrapped}\n"""\n')
    text = text.replace(DIGEST_MARK, f'PAYLOAD_SHA256 = "{digest}"\n')

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    kb = len(text.encode("utf-8")) / 1024
    print(f"[build] {len(files)} client files -> {len(payload) / 1024:.0f} KB zipped")
    print(f"[build] wrote {OUT.relative_to(ROOT).as_posix()} ({kb:.0f} KB)")
    print(f"[build] payload sha256 {digest[:16]}...")
    for path in files:
        print(f"           {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
