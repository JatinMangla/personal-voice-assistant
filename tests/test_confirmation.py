"""Risky tools run only after the user's own spoken "yes" (never the model's say-so)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import tools
from intents import parse_confirmation
from memory import Memory
from test_app import FakeStore, FakeTTS, events
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))


class ScriptedSTT:
    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, audio, quiet=False):
        return self.texts.pop(0), "en"


@pytest.fixture
def light(monkeypatch):
    switched = []
    monkeypatch.setattr(tools, "REGISTRY", {})
    monkeypatch.setattr(tools, "pending", None)
    tools.register(tools.Tool("light_off", "Turn off a light",
                              {"type": "object", "properties": {"room": {"type": "string"}}},
                              lambda room: switched.append(room) or f"{room} light is off.",
                              confirm_text="turn off the {room} light"))
    return switched


def make(embedder, monkeypatch, texts):
    def model_wants_the_light_off(messages, model=None):
        result = tools.run("light_off", {"room": "kitchen"})  # as the LLM's tool call would
        assert "Should I turn off the kitchen light?" in result
        yield "Should I turn off the kitchen light?"
    monkeypatch.setattr(app_module.llm, "stream_reply", model_wants_the_light_off)
    monkeypatch.setattr(app_module.learn, "learn", lambda *a: [])
    a = app_module.Assistant(ScriptedSTT(texts), FakeTTS(), Memory(embedder), FakeStore(),
                             SessionTimer(datetime.now(IST), 600))
    return a, TestClient(app_module.create_app(a))


def spoken(response):
    return " ".join(e["text"] for e in events(response) if e["type"] == "sentence")


def test_action_runs_only_after_a_spoken_yes(embedder, monkeypatch, light):
    a, client = make(embedder, monkeypatch, ["turn off the kitchen light", "yes please"])
    assert spoken(client.post("/api/turn", content=b"a")) == "Should I turn off the kitchen light?"
    assert light == []                       # asked, not done
    assert spoken(client.post("/api/turn", content=b"a")) == "Done."
    assert light == ["kitchen"] and tools.pending is None
    assert a.memory.recent_turns(1) == [("yes please", "Done.")]


def test_no_cancels(embedder, monkeypatch, light):
    _, client = make(embedder, monkeypatch, ["turn off the kitchen light", "nahi"])
    client.post("/api/turn", content=b"a")
    assert spoken(client.post("/api/turn", content=b"a")) == "Okay, I won't do it."
    assert light == [] and tools.pending is None


def test_changing_the_subject_drops_the_action(embedder, monkeypatch, light):
    _, client = make(embedder, monkeypatch, ["turn off the kitchen light",
                                             "what's the weather", "yes"])
    client.post("/api/turn", content=b"a")
    client.post("/api/turn", content=b"a")   # something else: the old action is dropped...
    tools.cancel()                            # (...and the fake model asked again; clear it)
    client.post("/api/turn", content=b"a")   # ...so a later "yes" can't run it
    assert light == []


def test_a_late_yes_does_not_count(embedder, monkeypatch, light):
    tools.run("light_off", {"room": "hall"})
    tools.pending.created -= tools.CONFIRM_WINDOW_SEC + 1
    assert tools.confirm() == (False, "Nothing is waiting for confirmation.")
    assert light == []


def test_the_model_cannot_confirm_by_calling_again(light):
    tools.run("light_off", {"room": "hall"})
    tools.run("light_off", {"room": "hall"})  # a second call only asks again
    assert light == []


def test_failed_action_says_so(monkeypatch):
    monkeypatch.setattr(tools, "REGISTRY", {})
    tools.register(tools.Tool("boom", "x", {}, lambda: 1 / 0))
    tools.run("boom", {})
    ok, result = tools.confirm()
    assert not ok and "failed" in result


@pytest.mark.parametrize("text,answer", [
    ("Yes.", "yes"), ("yes please", "yes"), ("Jarvis, yes", "yes"), ("haan kar do", "yes"),
    ("हाँ।", "yes"), ("ठीक है", "yes"), ("ok go ahead", "yes"), ("No.", "no"), ("nahi", "no"),
    ("मत करो", "no"), ("rehne do", "no"),
    ("yes but first tell me the weather", None), ("turn off the light", None),
    ("what time is it", None),
])
def test_parse_confirmation(text, answer):
    assert parse_confirmation(text) == answer
