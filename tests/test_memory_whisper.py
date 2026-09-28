"""Back-learning from old conversations, the Memory panel, honesty about memory, Whisper mode."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi.testclient import TestClient

import app as app_module
import llm
from memory import Memory
from stt import normalize
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 20, 10, 0, tzinfo=IST)


class STT:
    def __init__(self):
        self.quiet_calls = []

    def transcribe(self, audio, quiet=False):
        self.quiet_calls.append(quiet)
        return "hello there friend", "en"


class TTS:
    pipelines = {"en": None}

    def synthesize(self, text, lang):
        return b"RIFF"


class Store:
    last_saved = None


def make(embedder, monkeypatch, learned=()):
    monkeypatch.setattr(app_module.llm, "stream_reply", lambda m, model=None: iter(["Hi."]))
    calls = []

    def fake_learn(memory, text, now):
        calls.append(text)
        return list(learned)
    monkeypatch.setattr(app_module.learn, "learn", fake_learn)
    stt = STT()
    a = app_module.Assistant(stt, TTS(), Memory(embedder), Store(),
                             SessionTimer(datetime.now(IST), 600))
    return a, TestClient(app_module.create_app(a)), stt, calls


def test_old_conversations_are_read_once_for_facts(embedder, monkeypatch):
    a, _, _, calls = make(embedder, monkeypatch, learned=["User works as a teacher."])
    a.memory.add_turn("I work as a teacher in Delhi", "Nice!", "en", T0)   # from before learning
    a.memory.add_turn("what's the weather", "Sunny.", "en", T0)
    assert a.backlearn_one() and a.backlearn_one()
    assert calls == ["I work as a teacher in Delhi", "what's the weather"]
    assert a.backlearn_one() is False           # nothing left
    assert a.backlearn_count == [2, 2]
    assert a.memory.unlearned_turns(10) == []


def test_new_turns_are_marked_learned(embedder, monkeypatch):
    a, client, _, calls = make(embedder, monkeypatch)
    client.post("/api/turn", content=b"audio")
    assert calls == ["hello there friend"]
    assert a.memory.unlearned_turns(10) == []  # won't be read again by back-learning


def test_memory_panel_lists_and_deletes(embedder, monkeypatch):
    a, client, _, _ = make(embedder, monkeypatch)
    a.memory.add_fact("User is allergic to peanuts.", T0, "health", "auto")
    a.memory.add_fact("my car is red", T0)
    facts = client.get("/api/facts").json()["facts"]
    assert [(f["text"], f["category"]) for f in facts] == [
        ("User is allergic to peanuts.", "health"), ("my car is red", "remembered")]
    assert client.post("/api/facts/delete", json={"id": facts[0]["id"]}).json() == {"ok": True}
    assert [f["text"] for f in client.get("/api/facts").json()["facts"]] == ["my car is red"]
    assert client.post("/api/facts/delete", json={}).status_code == 400


def test_whisper_mode_reaches_speech_recognition(embedder, monkeypatch):
    _, client, stt, _ = make(embedder, monkeypatch)
    client.post("/api/turn", content=b"audio", headers={"X-Quiet": "1"})
    client.post("/api/turn", content=b"audio")
    assert stt.quiet_calls == [True, False]


def test_soft_audio_is_made_louder_loud_audio_untouched():
    soft = (np.sin(np.linspace(0, 100, 16000)) * 0.04).astype(np.float32)
    assert abs(float(np.max(np.abs(normalize(soft)))) - 0.7) < 0.01
    very_soft = (np.sin(np.linspace(0, 100, 16000)) * 0.01).astype(np.float32)
    assert abs(float(np.max(np.abs(normalize(very_soft)))) - 0.3) < 0.01  # capped at 30x
    loud = (np.sin(np.linspace(0, 100, 16000)) * 0.9).astype(np.float32)
    assert normalize(loud) is loud
    silence = np.zeros(16000, dtype=np.float32)
    assert normalize(silence) is silence  # noise floor is never blown up


def test_prompt_is_honest_about_memory():
    empty = llm.system_prompt("en", [], [], T0)
    assert "haven't learned any facts about the user yet" in empty
    assert "don't claim you can only remember this chat" in empty
    known = llm.system_prompt("en", ["User is a teacher."], [], T0)
    assert "User is a teacher." in known and "You DO remember the user" in known
