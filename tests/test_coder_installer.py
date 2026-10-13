"""The single-file installer (scripts/make_coder_installer.py -> dist/jarvis_coder_installer.py).

It is how the owner connects a machine that cannot clone this private repo, so the things that
would silently break it are tested: a module left out of the payload, a payload that does not
unzip to the real sources, a non-standard-library import before pip has run, and an .mcp.json
merge that throws away someone's other MCP server.
"""
import ast
import base64
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
INSTALLER = ROOT / "dist" / "jarvis_coder_installer.py"

spec = importlib.util.spec_from_file_location("make_coder_installer",
                                              SCRIPTS / "make_coder_installer.py")
maker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maker)


def payload_bytes(text: str) -> bytes:
    """The embedded zip, read out of the generated file the way the installer itself does."""
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PAYLOAD":
            return base64.b64decode("".join(ast.literal_eval(node.value).split()))
    raise AssertionError("the generated file has no PAYLOAD")


@pytest.fixture(scope="module")
def generated() -> str:
    assert INSTALLER.exists(), "run scripts/make_coder_installer.py and commit dist/"
    return INSTALLER.read_text(encoding="utf-8")


def test_every_client_module_is_in_the_payload(generated):
    """A new module under scripts/coder/ that nobody added to the installer would make the
    installed client crash on a machine with no repo to fall back on."""
    with zipfile.ZipFile(io.BytesIO(payload_bytes(generated))) as zf:
        inside = set(zf.namelist())
    for path in maker.client_files():
        name = ("coder/" + path.name) if path.parent == maker.CLIENT_DIR else path.name
        assert name in inside, f"{name} is missing from the installer payload"
    assert "coder_mcp.py" in inside and "coder/service.py" in inside


def test_the_payload_unzips_to_the_real_sources_byte_for_byte(generated, tmp_path):
    with zipfile.ZipFile(io.BytesIO(payload_bytes(generated))) as zf:
        zf.extractall(tmp_path)
    for path in maker.client_files():
        rel = ("coder/" + path.name) if path.parent == maker.CLIENT_DIR else path.name
        got = (tmp_path / rel).read_bytes()
        assert hashlib.sha256(got).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest(), rel


def test_the_payload_checksum_in_the_file_matches_the_payload(generated):
    """The installer refuses a payload whose digest does not match, so a half-copied file fails
    loudly instead of installing broken code. That check is worthless if the digest is stale."""
    digest = next(ast.literal_eval(node.value)
                  for node in ast.parse(generated).body
                  if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", "") == "PAYLOAD_SHA256")
    assert hashlib.sha256(payload_bytes(generated)).hexdigest() == digest


def test_the_generated_file_is_valid_python_and_rebuilds_identically(generated):
    ast.parse(generated)                       # would raise if the template broke
    before = INSTALLER.read_bytes()
    subprocess.run([sys.executable, str(SCRIPTS / "make_coder_installer.py")],
                   check=True, capture_output=True, cwd=ROOT)
    assert INSTALLER.read_bytes() == before, "rebuilding changed the file; the zip is not deterministic"


def test_the_unpacked_client_is_importable(generated, tmp_path):
    with zipfile.ZipFile(io.BytesIO(payload_bytes(generated))) as zf:
        zf.extractall(tmp_path)
    probe = ("import sys; sys.path.insert(0, r'" + str(tmp_path) + "')\n"
             "from coder import service, search, safety, chunk\n"
             "print('ok', len(chunk.TEXT_EXTENSIONS))\n")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.startswith("ok")


def test_nothing_outside_the_standard_library_is_imported_before_pip_runs(generated):
    """The installer runs on a bare machine, so a top-level `import httpx` would kill it before it
    could install httpx. Everything it needs early must be standard library."""
    allowed = {"argparse", "base64", "hashlib", "io", "json", "os", "re", "shutil",
               "subprocess", "sys", "zipfile", "pathlib"}
    tree = ast.parse(generated)
    for node in tree.body:                      # module level only: inside functions is fine
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] in allowed, node.module


def test_it_declares_the_three_packages_and_a_sane_python_floor(generated):
    namespace: dict = {}
    exec(compile(ast.parse(generated), "installer", "exec"), namespace)  # noqa: S102
    assert namespace["PACKAGES"] == ["numpy", "httpx", "mcp"]
    assert namespace["MIN_PYTHON"] >= (3, 10)
    assert "ngrok" in namespace["DEFAULT_URL"]


@pytest.mark.parametrize("bad", ["short", "has spaces", "trailing\"quote", "semi;colon", "tab\there"])
def test_a_mistyped_key_is_refused(generated, bad):
    """A BOM or stray character once silently broke the key for a whole Kaggle run (gotcha 32g)."""
    namespace: dict = {}
    exec(compile(ast.parse(generated), "installer", "exec"), namespace)  # noqa: S102
    assert not namespace["KEY_PATTERN"].match(bad), bad


def test_a_real_looking_key_is_accepted(generated):
    namespace: dict = {}
    exec(compile(ast.parse(generated), "installer", "exec"), namespace)  # noqa: S102
    assert namespace["KEY_PATTERN"].match("Zx8tQ7bV3nK1pL9sR4wY6mA2cE5uH0jDqF")


def test_the_config_merge_keeps_another_mcp_server(generated, tmp_path, monkeypatch):
    """Someone may already run other MCP servers; connecting the Coder must not delete them."""
    namespace: dict = {}
    exec(compile(ast.parse(generated), "installer", "exec"), namespace)  # noqa: S102
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"something-else": {"command": "node", "args": ["other.js"]}}}),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    namespace["write_mcp_config"](tmp_path / "client")
    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "something-else" in config["mcpServers"]
    assert config["mcpServers"]["coder"]["args"][0].endswith("coder_mcp.py")
    namespace["write_mcp_config"](tmp_path / "client")         # twice changes nothing more
    again = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert again == config


def test_uninstall_removes_only_the_coder_entry(generated, tmp_path, monkeypatch):
    namespace: dict = {}
    exec(compile(ast.parse(generated), "installer", "exec"), namespace)  # noqa: S102
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "something-else": {"command": "node"}, "coder": {"command": "python"}}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODER_HOME", str(tmp_path / "home"))
    namespace["uninstall"]()
    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert list(config["mcpServers"]) == ["something-else"]


def test_a_damaged_payload_is_refused_rather_than_installed(generated, tmp_path, monkeypatch):
    """A truncated paste or half-finished download must fail loudly, not install broken code."""
    namespace: dict = {}
    exec(compile(ast.parse(generated), "installer", "exec"), namespace)  # noqa: S102
    namespace["PAYLOAD_SHA256"] = "0" * 64
    monkeypatch.setenv("CODER_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as caught:
        namespace["unpack"]()
    assert "damaged" in str(caught.value).lower()
