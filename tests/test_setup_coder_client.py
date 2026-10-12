"""scripts/setup_coder_client.py: the one-command client setup.

Its whole job is to remove human steps, so the parts a person cannot easily check themselves
(the per-OS python path, not clobbering other MCP servers, refusing a mistyped key) are tested.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("setup_coder_client",
                                              SCRIPTS / "setup_coder_client.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_the_python_path_matches_the_operating_system():
    """The committed .mcp.json is Windows-only; that single line is what breaks the setup on
    macOS and Linux, so the script must write the right one for the machine it runs on."""
    path = setup.venv_python().as_posix()
    if setup.WINDOWS:
        assert path.endswith(".venv/Scripts/python.exe")
    else:
        assert path.endswith(".venv/bin/python")


def test_the_written_config_points_at_the_tool_server_and_carries_both_variables(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    setup.write_mcp_config(python)
    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    coder = written["mcpServers"]["coder"]
    assert coder["command"] == ".venv/bin/python"       # relative, so the repo can move
    assert coder["args"] == ["scripts/coder_mcp.py"]
    assert "CODER_KEY" in coder["env"] and "CODER_URL" in coder["env"]
    assert setup.DEFAULT_URL in coder["env"]["CODER_URL"]


def test_another_mcp_server_in_the_file_is_kept(tmp_path, monkeypatch):
    """Someone may already use other MCP servers; setting up the Coder must not delete them."""
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"something-else": {"command": "node", "args": ["other.js"]}}}),
        encoding="utf-8")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    setup.write_mcp_config(python)
    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "something-else" in written["mcpServers"], "another server was thrown away"
    assert "coder" in written["mcpServers"]


def test_a_damaged_config_is_replaced_rather_than_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    (tmp_path / ".mcp.json").write_text("{not json at all", encoding="utf-8")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    setup.write_mcp_config(python)
    assert json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["coder"]


def test_running_it_twice_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    setup.write_mcp_config(python)
    first = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    setup.write_mcp_config(python)
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == first


@pytest.mark.parametrize("bad", ["short", "has spaces in it", "ends-with-a-quote\"",
                                 "tab\tinside", "semi;colon"])
def test_a_mistyped_key_is_refused(bad):
    """A BOM or a stray character once broke the key silently for a whole Kaggle run (gotcha 32g),
    so a key that make_key.py could never have printed is rejected before it is stored."""
    assert not setup.KEY_PATTERN.match(bad), bad


def test_a_real_looking_key_is_accepted():
    assert setup.KEY_PATTERN.match("Zx8_tQ-7bV3nK1pL9sR4wY6mA2cE5uH0jD" * 1)


def test_the_three_packages_are_the_ones_the_tools_import():
    assert setup.PACKAGES == ["numpy", "httpx", "mcp"]
