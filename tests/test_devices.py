"""The Android app's pairing and device tokens (devices.py + app.py middleware + ngrok policy)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import devices as devices_module
import tunnel
from app import Assistant, PasswordGate, create_app
from devices import Devices
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=IST)
APP = {"X-Jarvis-App": "1"}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Fake:
    pipelines = {"en": None}
    last_saved = None

    def transcribe(self, audio, quiet=False):
        return "", "en"


@pytest.fixture
def devs(embedder):
    clock = Clock()
    return Devices(Memory(embedder), clock=clock), clock


@pytest.fixture
def web(embedder):
    def _make(gate=None):
        a = Assistant(Fake(), Fake(), Memory(embedder), Fake(), SessionTimer(datetime.now(IST), 600))
        return a, TestClient(create_app(a, gate, tailnet_users=set()))
    return _make


def bearer(token):
    return {**APP, "Authorization": f"Bearer {token}"}


# --- devices.py ------------------------------------------------------------------------
def test_only_the_hash_is_stored_and_token_works_once_paired(devs):
    d, _ = devs
    token = d.mint("Pixel", NOW)
    assert len(token) >= 43
    stored = d.memory.to_bytes()
    assert token.encode() not in stored and devices_module.token_hash(token).encode() in stored
    assert d.list() == [{"id": 1, "name": "Pixel", "created": NOW.isoformat(timespec="seconds"),
                         "paired": False, "last_used": None}]
    device, locked = d.check(f"Bearer {token}", NOW)
    assert device["name"] == "Pixel" and not locked
    assert d.list()[0]["paired"] is True


def test_unused_pairing_code_expires_but_paired_device_does_not(devs):
    d, clock = devs
    unused = d.mint("A", NOW)
    used = d.mint("B", NOW)
    assert d.check(f"Bearer {used}", NOW)[0]
    clock.t += devices_module.PAIR_TTL_SEC + 1
    assert d.check(f"Bearer {unused}", NOW)[0] is None
    assert [x["name"] for x in d.list()] == ["B"]  # the expired code is gone
    clock.t += 365 * 86400
    assert d.check(f"Bearer {used}", NOW)[0]


def test_revoke_stops_the_token(devs):
    d, _ = devs
    token = d.mint("Phone", NOW)
    assert d.check(token, NOW)[0]  # "Bearer " is optional
    assert d.revoke(d.list()[0]["id"])
    assert d.check(f"Bearer {token}", NOW)[0] is None
    assert not d.revoke(99)


def test_wrong_tokens_lock_out_wrong_tokens_only(devs):
    d, clock = devs
    good = d.mint("Phone", NOW)
    for _ in range(devices_module.MAX_FAILS):
        assert d.check("Bearer wrong", NOW) == (None, False)
    assert d.check("Bearer wrong", NOW) == (None, True)
    assert d.check(f"Bearer {good}", NOW)[0]  # the owner's phone keeps working
    clock.t += devices_module.LOCK_SEC + 1
    assert d.check("Bearer wrong", NOW) == (None, False)


def test_names_are_cleaned_and_devices_capped(devs):
    d, _ = devs
    d.mint("  my   phone\n" + "x" * 100, NOW)
    assert d.list()[0]["name"].startswith("my phone") and len(d.list()[0]["name"]) == 40
    d.mint("", NOW)
    assert d.list()[1]["name"] == "Phone"
    for _ in range(devices_module.MAX_DEVICES - 2):
        assert d.mint("x", NOW)
    assert d.mint("one too many", NOW) is None


def test_devices_survive_save_and_load(devs, embedder):
    d, clock = devs
    token = d.mint("Phone", NOW)
    d.check(f"Bearer {token}", NOW)
    again = Devices(Memory(embedder, d.memory.to_bytes()), clock=clock)
    assert again.check(f"Bearer {token}", NOW)[0]["name"] == "Phone"


# --- app.py: who may call what ---------------------------------------------------------
def test_app_requests_need_a_paired_token(web):
    a, c = web()
    assert c.get("/api/status", headers=APP).status_code == 401
    assert c.get("/api/status", headers=bearer("nope")).status_code == 401
    token = c.post("/api/app/pair", json={"name": "Phone"}).json()["token"]
    hello = c.get("/api/app/hello", headers=bearer(token))
    assert hello.status_code == 200 and hello.json()["api"] == app_module.API_VERSION
    assert c.get("/api/status", headers=bearer(token)).json()["api"] == app_module.API_VERSION


def test_app_token_cannot_manage_devices(web):
    a, c = web()
    token = c.post("/api/app/pair", json={}).json()["token"]
    for method, path in (("post", "/api/app/pair"), ("get", "/api/app/devices"),
                         ("post", "/api/app/devices/revoke")):
        r = getattr(c, method)(path, headers=bearer(token))
        assert r.status_code == 403, path
    assert len(a.devices.list()) == 1


def test_page_lists_and_revokes_devices(web):
    a, c = web()
    token = c.post("/api/app/pair", json={"name": "Pixel"}).json()["token"]
    c.get("/api/app/hello", headers=bearer(token))
    devices = c.get("/api/app/devices").json()["devices"]
    assert devices[0]["name"] == "Pixel" and devices[0]["paired"]
    assert "token" not in devices[0] and "token_hash" not in devices[0]
    assert c.post("/api/app/devices/revoke", json={"id": devices[0]["id"]}).json() == {"ok": True}
    assert c.get("/api/status", headers=bearer(token)).status_code == 401
    assert c.post("/api/app/devices/revoke", json={"id": "x"}).status_code == 400


def test_too_many_wrong_tokens_gives_429(web):
    a, c = web()
    for _ in range(devices_module.MAX_FAILS):
        assert c.get("/api/status", headers=bearer("bad")).status_code == 401
    assert c.get("/api/status", headers=bearer("bad")).status_code == 429


def test_app_token_passes_the_password_page(web):
    gate = PasswordGate("pw", b"k" * 32)
    a, c = web(gate)
    assert c.get("/api/status").status_code == 401  # browser without login
    token = a.devices.mint("Phone", NOW)
    assert c.get("/api/status", headers=bearer(token)).status_code == 200
    assert c.get("/api/status", headers=APP).status_code == 401


def test_pairing_limit_answers_409(web, monkeypatch):
    monkeypatch.setattr(devices_module, "MAX_DEVICES", 1)
    a, c = web()
    assert c.post("/api/app/pair", json={}).status_code == 200
    assert c.post("/api/app/pair", json={}).status_code == 409


def test_qr_library_is_served(web):
    _, c = web()
    r = c.get("/qrcode.js")
    assert r.status_code == 200 and "Kazuhiko Arase" in r.text


# --- ngrok ---------------------------------------------------------------------------
def test_ngrok_policy_lets_only_app_header_requests_skip_google_sign_in():
    oauth, deny = tunnel.traffic_policy(["me@gmail.com"])["on_http_request"]
    assert oauth["expressions"] == ["!('x-jarvis-app' in req.headers)"]
    assert deny["expressions"][0].startswith("!('x-jarvis-app' in req.headers) && ")
    both = tunnel.traffic_policy(["me@gmail.com"], bridge_path=True)["on_http_request"][0]
    assert both["expressions"] == [
        "!req.url.path.startsWith('/api/bridge/') && !('x-jarvis-app' in req.headers)"]
