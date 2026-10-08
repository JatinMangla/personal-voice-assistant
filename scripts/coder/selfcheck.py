"""Checks a code draft BEFORE Claude Code sees it, so broken drafts cost no tokens.

Always on (nothing is executed): paths must be inside the project, Python must parse, ruff finds
real errors (if installed), `node --check` for JavaScript (if installed).
Opt-in (CODER_RUN_TESTS=1): the tests that belong to the changed files, run in a temporary COPY
of the project. That runs code written by a model, so it is off by default; the answer says so.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import chunk, safety

TEST_TIMEOUT_SEC = 150
MAX_FILES = 8
MAX_FILE_CHARS = 120_000
SKIP_COPY = shutil.ignore_patterns(*safety.SKIP_DIRS, "android", "*.zip", "*.apk", "*.enc")


def target_path(path: str, roots: list[Path]) -> tuple[Path, Path] | None:
    """(root, absolute path) for a draft's relative path, or None if it is not allowed: absolute,
    '..', a secret file, or not a text/source type. With several roots the path starts with the
    root's folder name; with one root it is relative to it."""
    clean = path.replace("\\", "/").strip()
    if clean.startswith("./"):
        clean = clean[2:]
    parts = Path(clean).parts
    if not clean or clean.startswith("/") or ":" in clean or ".." in parts:
        return None
    root, relative = roots[0], Path(clean)
    if len(roots) > 1:
        match = next((r for r in roots if parts and parts[0] == r.name), None)
        if match is None:
            return None
        root, relative = match, Path(*parts[1:]) if len(parts) > 1 else Path("")
    full = (root / relative).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return None
    if safety.is_secret(full) or full.suffix.lower() not in chunk.TEXT_EXTENSIONS or full == root.resolve():
        return None
    return root, full


def check_python(code: str, name: str) -> list[str]:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return [f"{name}: syntax error on line {e.lineno}: {e.msg}"]
    ruff = shutil.which("ruff")
    if not ruff:
        return []
    result = subprocess.run([ruff, "check", "--select", "E9,F63,F7,F82", "--stdin-filename", name,
                             "--output-format", "concise", "-"], input=code, capture_output=True,
                            text=True, timeout=30)
    if result.returncode == 0:
        return []
    return [line[:200] for line in result.stdout.splitlines() if line.strip()
            and not line.startswith(("Found ", "[*]", "All checks"))][:5]


def check_js(code: str, name: str) -> list[str]:
    node = shutil.which("node")
    if not node:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "draft.js"
        f.write_text(code, encoding="utf-8")
        result = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        return []
    return [f"{name}: " + " ".join(result.stderr.strip().splitlines()[-2:])[:200]]


def related_tests(copy_root: Path, relative_paths: list[str]) -> list[str]:
    tests_dir = copy_root / "tests"
    if not tests_dir.is_dir():
        return []
    found: list[str] = []
    for rel in relative_paths:
        stem = Path(rel).stem
        if not rel.endswith(".py"):
            continue
        if stem.startswith("test_") and (copy_root / rel).exists():
            found.append(rel)
            continue
        found += [p.relative_to(copy_root).as_posix() for p in sorted(tests_dir.glob(f"test_*{stem}*.py"))]
    return list(dict.fromkeys(found))[:6]


def run_tests(root: Path, files: dict[str, str]) -> str:
    """Run the tests that belong to the changed Python files in a temporary copy of the project."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project"
        shutil.copytree(root, copy, ignore=SKIP_COPY)
        for rel, content in files.items():
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            (copy / rel).write_text(content, encoding="utf-8")
        tests = related_tests(copy, list(files))
        if not tests:
            return "no related tests found"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header",
                                     "-p", "no:cacheprovider", *tests], cwd=copy, env=env,
                                    capture_output=True, text=True, timeout=TEST_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return f"tests timed out after {TEST_TIMEOUT_SEC}s"
        tail = "\n".join(result.stdout.strip().splitlines()[-8:])[-900:]
        return ("tests passed: " if result.returncode == 0 else "tests FAILED: ") + tail


def check_draft(roots: list[Path], files: list[dict], run_tests_too: bool | None = None) -> dict:
    """{'problems': [...], 'tests': str, 'clean': {relative path: content}}. 'clean' only has
    files that passed the path rules. Problems are phrased so the model can fix them."""
    if run_tests_too is None:
        run_tests_too = os.environ.get("CODER_RUN_TESTS") == "1"
    problems: list[str] = []
    accepted: dict[Path, dict[str, str]] = {}
    if not files:
        problems.append("the draft contains no files")
    for f in files[:MAX_FILES]:
        found = target_path(f["path"], roots)
        if not found:
            problems.append(f"{f['path']}: path is not allowed (use a relative path to a source file "
                            "inside the project, no secrets)")
            continue
        root, full = found
        if len(f["content"]) > MAX_FILE_CHARS:
            problems.append(f"{f['path']}: file is too long")
            continue
        suffix = full.suffix.lower()
        problems += (check_python(f["content"], f["path"]) if suffix == ".py"
                     else check_js(f["content"], f["path"]) if suffix == ".js" else [])
        accepted.setdefault(root, {})[full.relative_to(root.resolve()).as_posix()] = f["content"]
    tests = "not run (CODER_RUN_TESTS=1 turns it on; it runs model-written code on this PC)"
    if run_tests_too and not problems:
        results = [run_tests(root, rel_files) for root, rel_files in accepted.items()]
        tests = "; ".join(results)
        if "FAILED" in tests or "timed out" in tests:
            problems.append(tests)
    return {"problems": problems, "tests": tests,
            "clean": {rel: c for rel_files in accepted.values() for rel, c in rel_files.items()}}
