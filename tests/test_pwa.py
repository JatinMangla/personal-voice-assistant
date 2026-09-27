"""Installable app (PWA) files and the automatic code upload."""
import importlib.util
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
from app import PasswordGate
from memory import Memory
from timer import SessionTimer

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fake:
    pipelines = {"en": None}
    last_saved = None

    def transcribe(self, audio):
        return "", "en"


def client(embedder, gate=None, start_url=None):
    a = app_module.Assistant(Fake(), Fake(), Memory(embedder), Fake(),
                             SessionTimer(datetime.now(IST), 600))
    a.start_url = start_url
    return TestClient(app_module.create_app(a, gate))


def test_app_files_are_served_with_right_types(embedder):
    c = client(embedder)
    assert c.get("/sw.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/manifest.json").headers["content-type"].startswith("application/manifest+json")
    for icon in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        r = c.get(f"/{icon}")
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_manifest_is_installable():
    m = json.loads((ROOT / "src" / "static" / "manifest.json").read_text())
    assert m["display"] == "standalone" and m["start_url"]
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for i in m["icons"]:
        assert (ROOT / "src" / "static" / i["src"]).exists()


def test_app_files_open_but_data_locked_behind_password(embedder):
    c = client(embedder, gate=PasswordGate("pw", b"k" * 32))
    assert c.get("/sw.js").status_code == 200
    assert c.get("/icon-192.png").status_code == 200
    assert c.get("/api/status").status_code == 401


def test_status_gives_start_button_address(embedder):
    url = "https://github.com/me/personal-voice-assistant/actions/workflows/start-jarvis.yml"
    assert client(embedder, start_url=url).get("/api/status").json()["start_url"] == url


def test_code_zip_has_everything_with_linux_paths(tmp_path):
    out = load_script("make_code_zip").build(tmp_path / "code.zip")
    names = zipfile.ZipFile(out).namelist()
    for needed in ("src/main.py", "src/static/index.html", "src/static/sw.js",
                   "src/static/icon-512.png", "src/learn.py"):
        assert needed in names
    assert not any("\\" in n or "__pycache__" in n for n in names)


def test_upload_metadata():
    meta = load_script("upload_code").dataset_metadata("jatinmangla123", "assistant-code")
    assert meta["id"] == "jatinmangla123/assistant-code" and meta["title"] == "assistant-code"
