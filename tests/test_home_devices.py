"""Home Assistant devices and cameras: allowlist only, spoken yes for risky actions."""
import json

import pytest

import home
import llm
import tools

DEVICES = {
    "devices": [
        {"name": "Living room light", "entity": "light.syska_bulb",
         "actions": ["turn_on", "turn_off"], "confirm": False},
        {"name": "air fryer", "entity": "switch.air_fryer_plug", "actions": ["turn_on", "turn_off"]},
        {"name": "washing machine", "entity": "sensor.washer_job_state", "actions": []},
    ],
    "cameras": [{"name": "front door", "entity": "camera.front_door"},
                {"name": "bad", "entity": "light.not_a_camera"}],
}
JPEG = b"\xff\xd8\xff" + b"0" * 100


class FakeHA:
    """requests.get/post answering like Home Assistant's REST API."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers["Authorization"]))
        if "/api/camera_proxy/" in url:
            return Resp(200, content=JPEG)
        if url.endswith("sensor.washer_job_state"):
            return Resp(200, {"state": "rinse", "attributes": {"remaining_time": "0:25"}})
        return Resp(200, {"state": "on", "attributes": {"brightness": 200}})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return Resp(200, [])


class Resp:
    def __init__(self, status, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise home.requests.HTTPError(str(self.status_code))


@pytest.fixture
def ha(monkeypatch):
    fake = FakeHA()
    monkeypatch.setattr(home.requests, "get", fake.get)
    monkeypatch.setattr(home.requests, "post", fake.post)
    monkeypatch.setattr(tools, "REGISTRY", {})
    monkeypatch.setattr(tools, "pending", None)
    tools.take_images()
    h = home.HomeAssistant("http://192.168.1.20:8123/", "tok", DEVICES)
    home.register(h)
    return h, fake


@pytest.mark.parametrize("url,ok", [
    ("https://myhome.duckdns.org", True), ("http://192.168.1.20:8123", True),
    ("http://homeassistant.local:8123", True), ("http://homeassistant:8123", True),
    ("http://myhome.duckdns.org", False), ("http://8.8.8.8", False), ("ftp://x", False),
])
def test_address_rules(url, ok):
    assert home.address_allowed(url) is ok


def test_harmless_device_runs_at_once(ha):
    _, fake = ha
    assert tools.run("control_device", {"device": "living room light", "action": "turn_off"}) \
        == "Done: turn off Living room light."
    assert fake.calls[-1] == ("POST", "http://192.168.1.20:8123/api/services/light/turn_off",
                              {"entity_id": "light.syska_bulb"})


def test_risky_device_waits_for_a_spoken_yes(ha):
    _, fake = ha
    answer = tools.run("control_device", {"device": "air fryer", "action": "turn_on"})
    assert 'Should I turn on the air fryer?' in answer and fake.calls == []
    ok, result = tools.confirm()
    assert ok and result == "Done: turn on air fryer."
    assert fake.calls[-1][1].endswith("/api/services/switch/turn_on")


def test_unknown_devices_and_actions_never_reach_home_assistant(ha):
    _, fake = ha
    assert tools.run("control_device", {"device": "front door lock", "action": "turn_on"},
                     confirmed=True).startswith("Unknown device")
    assert "not allowed" in tools.run("control_device", {"device": "washing machine",
                                                         "action": "turn_on"}, confirmed=True)
    assert "not allowed" in tools.run("control_device", {"device": "Living room light",
                                                         "action": "unlock"}, confirmed=True)
    assert fake.calls == []


def test_status_and_tool_specs_show_names_not_entity_ids(ha):
    assert tools.run("device_status", {"device": "washing machine"}) == \
        "washing machine is rinse (remaining time 0:25)."
    specs = json.dumps(tools.ollama_specs())
    assert "air fryer" in specs and "switch.air_fryer_plug" not in specs


def test_camera_snapshot_goes_to_the_model_once(ha, monkeypatch):
    h, _ = ha
    assert set(h.cameras) == {"front door"}  # a non-camera entity is ignored
    sent = []
    replies = [
        [{"message": {"tool_calls": [{"function": {"name": "look_at_camera",
                                                    "arguments": {"camera": "front door"}}}]}},
         {"done": True}],
        [{"message": {"content": "A delivery person is at the door."}}, {"done": True}],
    ]

    class R:
        def __init__(self, chunks):
            self.lines = [json.dumps(c).encode() for c in chunks]
            self.status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter(self.lines)

    def fake_post(url, json=None, **kw):
        sent.append(json)
        return R(replies.pop(0))
    monkeypatch.setattr(llm.requests, "post", fake_post)
    text = "".join(llm.stream_reply([{"role": "user", "content": "who's at the door?"}], "m"))
    assert text == "A delivery person is at the door."
    last = sent[1]["messages"][-1]
    assert last["role"] == "user" and last["images"] and "untrusted" in last["content"]
    assert tools.take_images() == []  # used once, not kept


def test_home_assistant_down_is_said_plainly(monkeypatch):
    def down(*a, **k):
        raise home.requests.ConnectionError("no route")
    monkeypatch.setattr(home.requests, "get", down)
    monkeypatch.setattr(tools, "REGISTRY", {})
    home.register(home.HomeAssistant("https://h.example", "t", DEVICES))
    assert tools.run("device_status", {"device": "air fryer"}) == \
        "Home Assistant did not answer (ConnectionError)."


def test_setup_needs_both_secrets_and_a_safe_address(monkeypatch):
    import config
    monkeypatch.setattr(config, "_bundle_cache", {})
    monkeypatch.setattr(tools, "REGISTRY", {})
    for name in ("HA_URL", "HA_TOKEN", "HOME_DEVICES"):
        monkeypatch.delenv(name, raising=False)
    assert home.setup() is None
    monkeypatch.setenv("HA_URL", "http://example.com")
    monkeypatch.setenv("HA_TOKEN", "t")
    monkeypatch.setenv("HOME_DEVICES", json.dumps(DEVICES))
    assert home.setup() is None and tools.REGISTRY == {}  # plain http on the internet: refused
    monkeypatch.setenv("HA_URL", "https://example.com")
    assert home.setup() is not None
    assert set(tools.REGISTRY) == {"device_status", "control_device", "look_at_camera"}
