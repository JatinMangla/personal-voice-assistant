"""The plain laptop page (no PWA any more) and the automatic code upload."""
import importlib.util
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

    def transcribe(self, audio, quiet=False):
        return "", "en"


def client(embedder, gate=None, start_url=None):
    a = app_module.Assistant(Fake(), Fake(), Memory(embedder), Fake(),
                             SessionTimer(datetime.now(IST), 600))
    a.start_url = start_url
    return TestClient(app_module.create_app(a, gate))


def test_page_is_plain_not_an_installable_app(embedder):
    html = (ROOT / "src" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' not in html and "apple-touch-icon" not in html
    assert "serviceWorker.register" not in html and "unregister()" in html
    c = client(embedder)
    assert c.get("/manifest.json").status_code == 404
    assert c.get("/icon-512.png").status_code == 404
    assert c.get("/").status_code == 200


def test_old_service_worker_is_replaced_by_one_that_removes_itself(embedder):
    c = client(embedder)
    r = c.get("/sw.js")
    assert r.headers["content-type"].startswith("text/javascript")
    assert "registration.unregister()" in r.text and "caches.delete" in r.text
    assert "fetch" not in r.text  # it never serves anything from a cache any more
    icon = c.get("/icon-192.png")
    assert icon.status_code == 200 and icon.content[:8].startswith(b"\x89PNG")


def test_favicon_and_sw_open_but_data_locked_behind_password(embedder):
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
                   "src/static/qrcode.js", "src/devices.py", "src/learn.py"):
        assert needed in names
    assert not any("\\" in n or "__pycache__" in n for n in names)


def test_upload_metadata():
    meta = load_script("upload_code").dataset_metadata("jatinmangla123", "assistant-code")
    assert meta["id"] == "jatinmangla123/assistant-code" and meta["title"] == "assistant-code"
