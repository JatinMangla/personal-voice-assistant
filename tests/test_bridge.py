"""The home helper link (src/bridge.py) and the phone helper (home_bridge/samsung_bridge.py)."""
import importlib.util
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cbor2
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as app_module
import bridge
import tools
import tunnel
from memory import Memory
from test_app import FakeStore, FakeTTS
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))
KEY = "k" * 43
spec = importlib.util.spec_from_file_location(
    "samsung_bridge", Path(__file__).resolve().parent.parent / "home_bridge" / "samsung_bridge.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


# --- Jarvis side ----------------------------------------------------------------------
@pytest.fixture
def jarvis(embedder, monkeypatch):
    monkeypatch.setattr(tools, "REGISTRY", {})
    monkeypatch.setattr(tools, "pending", None)
    a = app_module.Assistant(None, FakeTTS(), Memory(embedder), FakeStore(),
                             SessionTimer(datetime.now(IST), 600))
    a.bridge = bridge.HomeBridge(KEY)
    bridge.register(a.bridge)
    return a, TestClient(app_module.create_app(a))


def test_wrong_key_is_refused(jarvis):
    _, client = jarvis
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/bridge/ws", headers={"Authorization": "Bearer nope"}) as ws:
            ws.receive_text()


def test_not_connected_is_said_plainly(jarvis):
    assert tools.run("appliance_status", {"device": "washing machine"}) == bridge.NOT_CONNECTED


def test_status_and_confirmed_start_go_through_the_helper(jarvis):
    a, client = jarvis
    with client.websocket_connect("/api/bridge/ws", headers={"Authorization": f"Bearer {KEY}"}) as ws:
        ws.send_text(json.dumps({"hello": {"devices": ["Washing Machine"]}}))
        for _ in range(100):  # wait until the hello is processed
            if a.bridge.devices:
                break
            threading.Event().wait(0.01)
        answers = {}

        def ask(name, args, confirmed=False):
            answers[name] = tools.run(name, args, confirmed=confirmed)
        t = threading.Thread(target=ask, args=("appliance_status", {"device": "washing machine"}))
        t.start()
        request = json.loads(ws.receive_text())
        assert request["op"] == "status" and request["device"] == "washing machine"
        ws.send_text(json.dumps({"id": request["id"], "ok": True, "text": "It is running."}))
        t.join(5)
        assert answers["appliance_status"] == "It is running."

        asked = tools.run("appliance_control", {"device": "washing machine", "action": "start"})
        assert "Should I start the washing machine?" in asked  # nothing sent yet
        t = threading.Thread(target=lambda: answers.update(confirm=tools.confirm()))
        t.start()
        request = json.loads(ws.receive_text())
        assert request["op"] == "start"
        ws.send_text(json.dumps({"id": request["id"], "ok": True, "text": "Done: running."}))
        t.join(5)
        assert answers["confirm"] == (True, "Done: running.")
    assert tools.run("appliance_status", {"device": "fridge"}) == bridge.NOT_CONNECTED


def test_unknown_device_and_action(jarvis):
    a, _ = jarvis
    a.bridge.devices = ["washing machine"]
    assert a.bridge.status("fridge").startswith("Unknown appliance")
    assert "not possible" in a.bridge.control("washing machine", "spin_dry")


def test_wake_topic_matches_on_both_sides():
    assert bridge.wake_topic(KEY) == helper.wake_topic(KEY)
    assert bridge.wake_topic(KEY) != bridge.wake_topic("x" * 43) and KEY not in bridge.wake_topic(KEY)


def test_ngrok_policy_skips_google_sign_in_only_for_the_helper_path():
    plain = tunnel.traffic_policy(["me@gmail.com"], app_header=False)
    assert "expressions" not in plain["on_http_request"][0]
    policy = tunnel.traffic_policy(["me@gmail.com"], bridge_path=True, app_header=False)
    oauth, deny = policy["on_http_request"]
    assert oauth["expressions"] == ["!req.url.path.startsWith('/api/bridge/')"]
    assert deny["expressions"][0].startswith("!req.url.path.startsWith('/api/bridge/') && ")
    assert "me@gmail.com" in deny["expressions"][0]


def test_setup_needs_a_real_key(monkeypatch):
    import config
    monkeypatch.setattr(config, "_bundle_cache", {})
    monkeypatch.setattr(tools, "REGISTRY", {})
    monkeypatch.setenv("BRIDGE_KEY", "short")
    assert bridge.setup() is None
    monkeypatch.setenv("BRIDGE_KEY", KEY)
    assert bridge.setup() is not None and "appliance_control" in tools.REGISTRY


# --- phone helper ---------------------------------------------------------------------
def rep(state="Run", progress="Rinse", left="00:25:00", percent="60"):
    return {"x.com.samsung.da.state": state, "x.com.samsung.da.progress": progress,
            "x.com.samsung.da.remainingTime": left, "x.com.samsung.da.progressPercentage": percent}


@pytest.mark.parametrize("r,expected", [
    (rep(), ("running", "The washing machine is running (rinse), about 25 minutes left, 60% done.")),
    ({"rep": rep()}, ("running", "The washing machine is running (rinse), about 25 minutes left, 60% done.")),
    (rep(progress="Finish", left="00:01:00"), ("finished", "The washing machine has finished.")),
    (rep(state="Pause", left="01:05:00"), ("paused", "The washing machine is paused, 65 minutes left.")),
    (rep(state="Ready", progress="None", left="00:00:00"), ("idle", "The washing machine is not running.")),
])
def test_describe(r, expected):
    assert helper.describe(r) == expected


class FakeSession:
    """Answers like the washer over CoAP-DTLS."""

    def __init__(self, state="Ready", remote=True, reverts=False):
        self.state, self.remote, self.reverts, self.posts, self.closed = state, remote, reverts, [], False

    def get(self, path):
        if path == helper.STATE_PATH:
            return 69, cbor2.dumps(rep(state=self.state, progress="Wash" if self.state == "Run" else "None"))
        if path == ("remotectrl", "0"):
            return 69, cbor2.dumps({"value": self.remote})
        return 132, b""  # 4.04

    def post(self, path, body):
        self.posts.append((path, cbor2.loads(body)))
        if not self.reverts:
            self.state = cbor2.loads(body)["x.com.samsung.da.state"]
        return 68, b""  # 2.04 Changed

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The helper waits 3 s before reading a command back; not in tests."""
    monkeypatch.setattr(helper.time, "sleep", lambda s: None)


def washer(session):
    return helper.Washer("192.168.1.50", "c", "k", session_factory=lambda: session)


def test_start_needs_smart_control_on():
    s = FakeSession(remote=False)
    assert "Smart Control is off" in washer(s).run("start")["text"] and s.posts == []


def test_start_is_sent_and_read_back():
    s = FakeSession()
    result = washer(s).run("start")
    assert result == {"ok": True, "text": "Done: the washing machine is running."}
    assert s.posts == [(helper.STATE_PATH, {"x.com.samsung.da.state": "Run"})] and s.closed


def test_silently_reverted_command_is_reported():
    assert washer(FakeSession(reverts=True)).run("start") == {
        "ok": False, "text": "I asked it to start, but it still says idle."}


def test_unreachable_washer():
    def down():
        raise TimeoutError()
    w = helper.Washer("192.168.1.50", "c", "k", session_factory=down)
    assert "couldn't reach" in w.run("status")["text"]


def test_handle_routes_requests_by_device_name():
    devices = {"washing machine": washer(FakeSession(state="Run"))}
    reply = helper.handle({"id": 7, "op": "status", "device": "Washing Machine"}, devices)
    assert reply["id"] == 7 and reply["ok"] and "running" in reply["text"]
    assert helper.handle({"id": 8, "op": "status", "device": "fridge"}, devices)["ok"] is False


def test_finish_notification(monkeypatch):
    s = FakeSession(state="Run")
    w = washer(s)
    said = []
    monkeypatch.setattr(helper, "notify", said.append)
    states = iter(["Run", "Run", "End"])

    def fake_sleep(seconds):
        try:
            s.state = next(states)
        except StopIteration:
            raise KeyboardInterrupt  # end the endless loop
    monkeypatch.setattr(helper.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        helper.watch_for_finish(w, every=0)
    assert said == ["The washing machine has finished."]


def test_settings_file(tmp_path, monkeypatch):
    for name in ("JARVIS_URL", "BRIDGE_KEY", "WASHER_IP"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "bridge.env").write_text("# my home\nJARVIS_URL=https://x.ngrok-free.dev\n"
                                         "BRIDGE_KEY='abc'\nWASHER_IP=192.168.1.50\n")
    s = helper.load_settings(tmp_path / "bridge.env")
    assert s["JARVIS_URL"] == "https://x.ngrok-free.dev" and s["BRIDGE_KEY"] == "abc"
    assert s["DEVICE_NAME"] == "washing machine" and s["CERT"].endswith("client_fullchain.pem")
