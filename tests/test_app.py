"""Web server tests with fake speech/LLM parts (no GPU, no Ollama)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import Assistant, PasswordGate, create_app
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))


class FakeSTT:
    def __init__(self, text="", lang="en"):
        self.text, self.lang = text, lang

    def transcribe(self, audio):
        return self.text, self.lang


class FakeTTS:
    pipelines = {"en": None, "hi": None}

    def synthesize(self, text, lang):
        return b"RIFFfakewav"


class FakeStore:
    last_saved = None

    def __init__(self):
        self.saved = []

    def save(self, data):
        self.saved.append(data)
        return True


@pytest.fixture
def make(embedder, monkeypatch):
    def _make(text="", lang="en", reply=("Hello there.", " How can I help?"), gate=None):
        monkeypatch.setattr(app_module.llm, "stream_reply", lambda messages, model=None: iter(reply))
        now = datetime.now(IST)
        timer = SessionTimer(now, 600)
        memory = Memory(embedder)
        assistant = Assistant(FakeSTT(text, lang), FakeTTS(), memory, FakeStore(), timer)
        return assistant, TestClient(create_app(assistant, gate))
    return _make


def events(response):
    return [json.loads(l) for l in response.text.splitlines() if l.strip()]


def test_turn_streams_transcript_and_sentences(make):
    assistant, client = make(text="hello jarvis")
    evs = events(client.post("/api/turn", content=b"audio"))
    assert evs[0] == {"type": "transcript", "text": "hello jarvis", "lang": "en"}
    assert [e["text"] for e in evs if e["type"] == "sentence"] == ["Hello there.", "How can I help?"]
    assert evs[-1]["type"] == "done"
    assert assistant.memory.recent_turns(1) == [("hello jarvis", "Hello there. How can I help?")]


def test_remember_command_is_stored_as_fact_not_turn(make):
    assistant, client = make(text="Remember that my pin is 4321")
    evs = events(client.post("/api/turn", content=b"audio"))
    assert any(e.get("text") == "Okay, I'll remember that." for e in evs)
    assert assistant.memory.all_facts(5) == ["my pin is 4321"]
    assert assistant.memory.turn_count() == 0


def test_silence_gives_not_heard(make):
    _, client = make(text="")
    evs = events(client.post("/api/turn", content=b"audio"))
    assert evs[-1] == {"type": "done", "heard": False}


def test_llm_failure_speaks_error(make, monkeypatch):
    assistant, client = make(text="hello")

    def boom(messages, model=None):
        raise ConnectionError("ollama down")
    monkeypatch.setattr(app_module.llm, "stream_reply", boom)
    evs = events(client.post("/api/turn", content=b"audio"))
    assert evs[-1]["error"] is True
    assert not assistant.turn_lock.locked()


def meters(status):
    return {m["name"]: m for m in status["meters"]}


def test_status(make):
    _, client = make()
    s = client.get("/api/status").json()
    assert s["state"] == "ready" and s["notify_pct"] == 90 and s["stop_pct"] == 99
    assert set(meters(s)) == {"day", "week", "requests", "data"}
    assert s["warnings"] == []


def test_free_plan_traffic_is_counted(make):
    assistant, client = make(text="hello")
    client.post("/api/turn", content=b"audio")
    client.get("/")
    s = client.get("/api/status").json()
    assert meters(s)["requests"]["used"] == 2  # this status call is counted after it answers
    requests_used, bytes_used = assistant.memory.get_net(app_module.now_ist().strftime("%Y-%m"))
    assert requests_used == 3 and bytes_used > 0


def test_warning_at_90_percent_is_shown_and_spoken_once(make):
    assistant, client = make(text="hello")
    month = app_module.now_ist().strftime("%Y-%m")
    assistant.memory.add_net(month, 18_100, 0)  # 90.5 % of 20,000
    s = client.get("/api/status").json()
    assert s["warnings"] == ["Heads up: 90% of this month's free link requests is used."]

    for m in assistant.budget.new_notices(app_module.current_meters(assistant.memory, app_module.now_ist())):
        assistant.notices.put(m)  # what the watchdog does every 5 s
    spoken = [e["text"] for e in events(client.post("/api/turn", content=b"audio"))
              if e["type"] == "sentence"]
    assert spoken[-1].startswith("Heads up: 90% of this month's free link requests")
    spoken = [e["text"] for e in events(client.post("/api/turn", content=b"audio"))
              if e["type"] == "sentence"]
    assert not any(t.startswith("Heads up") for t in spoken)


def meters_by_name(assistant):
    return {m.name: m for m in app_module.current_meters(assistant.memory, app_module.now_ist())}


def test_gpu_warning_says_minutes_left(make):
    assistant, _ = make()
    today = app_module.now_ist().date().isoformat()
    assistant.memory.add_usage(today, 240 * 60)  # 240 of 257 min
    day = meters_by_name(assistant)["day"]
    assert app_module.notice_text(day, "en") == \
        "Heads up: 93% of today's GPU time is used. About 14 minutes left."


def test_stop_at_99_percent_says_why(make, monkeypatch):
    monkeypatch.setattr(app_module.config, "GOODBYE_GRACE_SEC", 0)
    assistant, client = make()
    month = app_module.now_ist().strftime("%Y-%m")
    assistant.memory.add_net(month, 0, 995_000_000)
    over = assistant.budget.over_stop(list(meters_by_name(assistant).values()))
    assert over.name == "data"
    said = []
    assistant.tts.synthesize = lambda text, lang: said.append(text) or b"RIFF"
    assistant.close("limit", over)
    assert said == ["This month's free link data is almost used up, so I'm stopping now "
                    "to keep this free. Your memory is saved. Goodbye!"]
    assert assistant.store.saved and assistant.state == "closing"


def test_close_saves_and_blocks_new_turns(make, monkeypatch):
    monkeypatch.setattr(app_module.config, "GOODBYE_GRACE_SEC", 0)
    assistant, client = make(text="hello")
    assistant.close("idle")
    assert assistant.store.saved, "memory must be saved on close"
    assert client.get("/api/goodbye").status_code == 200
    assert client.post("/api/turn", content=b"audio").status_code == 409


def test_password_gate(make):
    gate = PasswordGate("correct horse", b"k" * 32)
    _, client = make(gate=gate)
    assert client.get("/api/status").status_code == 401
    assert "Sign in" in client.get("/").text
    assert client.post("/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/login", json={"password": "correct horse"}).status_code == 200
    # TestClient runs on http, so the Secure cookie is set but must be passed by hand
    token = gate._sign(int(datetime.now().timestamp()) + 60)
    client.cookies.set(gate.COOKIE, token)
    assert client.get("/api/status").status_code == 200


def test_password_lockout():
    gate = PasswordGate("pw", b"k" * 32)
    for _ in range(5):
        assert gate.try_login("nope") is None
    assert gate.try_login("pw") is None  # locked even with the right password
