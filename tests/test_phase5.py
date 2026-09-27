"""Phase 5: ratings, training data, A/B model choice, big-file encryption."""
import os
from datetime import datetime, timedelta, timezone

import pytest

import crypto
from budget import Meter
from finetune_data import build_examples, looks_private, training_minutes_allowed
from llm import ModelPicker
from memory import Memory

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=IST)


def test_ratings_and_good_turns(embedder):
    m = Memory(embedder)
    a = m.add_turn("tell me a joke", "Why did the chicken...", "en", T0, "base")
    b = m.add_turn("hello", "Hi there!", "en", T0, "tuned")
    m.add_turn("weather", "Sunny.", "en", T0, "base")
    assert m.rate_turn(a, 1) and m.rate_turn(b, -1)
    assert not m.rate_turn(999, 1)
    assert [t[2] for t in m.good_turns()] == ["tell me a joke"]
    assert m.rating_stats() == {"base": (1, 0), "tuned": (0, 1)}
    m.rate_turn(a, 0)  # undo
    assert m.good_turns() == []


def test_old_database_gets_model_column(embedder):
    import sqlite3
    old = sqlite3.connect(":memory:")
    old.executescript("CREATE TABLE turns (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
                      "user_text TEXT NOT NULL, reply TEXT NOT NULL, lang TEXT, emb BLOB, "
                      "good INTEGER DEFAULT 0);")
    m = Memory(embedder, old.serialize())
    assert m.add_turn("q", "a", "en", T0, "base") == 1


@pytest.mark.parametrize("text, private", [
    ("my pin is 4821", True),
    ("call me on 98 1122 3344", True),
    ("mail me at raj@example.com", True),
    ("what's my password", True),
    ("tell me a joke about cats", False),
    ("मेरा दिन अच्छा था", False),
])
def test_private_looking_turns_are_detected(text, private):
    assert looks_private(text) is private


def test_build_examples_skips_private_and_uses_chat_format():
    rows = [("2026-09-27T10:00:00+05:30", "en", "tell me a joke", "Why did..."),
            ("2026-09-27T10:01:00+05:30", "en", "my otp is 552211", "Okay."),
            ("2026-09-27T10:02:00+05:30", "hi", "नमस्ते", "नमस्ते! कैसे हैं?")]
    examples = build_examples(rows)
    assert len(examples) == 2
    assert [m["role"] for m in examples[0]] == ["system", "user", "assistant"]
    assert "Devanagari" in examples[1][0]["content"]
    assert "What you know about the user" not in examples[0][0]["content"]  # no personal facts


def test_training_time_respects_budget():
    day = Meter("day", 100 * 60, 257 * 60)          # 154 min until the 99 % stop
    week = Meter("week", 28 * 3600, 30 * 3600)       # ~102 min until the stop
    link = Meter("data", 0, 1e9)
    assert training_minutes_allowed([day, week, link], reserve_min=15) == 87


@pytest.mark.parametrize("stats, mode, expected", [
    ({}, "auto", "ab"),                                            # not enough ratings yet
    ({"base": (20, 10), "tuned": (29, 1)}, "auto", "tuned"),       # tuned rated better
    ({"base": (28, 2), "tuned": (15, 15)}, "auto", "base"),        # base rated better
    ({"base": (20, 10), "tuned": (29, 1)}, "base", "base"),        # forced
    ({}, "tuned", "tuned"),
])
def test_model_picker(stats, mode, expected):
    assert ModelPicker("base", "tuned", stats, mode, 30).decision == expected


def test_model_picker_without_tuned_model():
    p = ModelPicker("base", None, {}, "auto", 30)
    assert p.decision == "base" and p.pick() == "base" and p.in_use() == ["base"]


def test_ab_uses_both_models():
    p = ModelPicker("base", "tuned", {}, "auto", 30)
    assert {p.pick() for _ in range(200)} == {"base", "tuned"}


def test_big_file_encryption_round_trip(tmp_path):
    key = crypto.load_key(crypto.generate_key())
    data = os.urandom(10_000)
    (tmp_path / "m.gguf").write_bytes(data)
    crypto.encrypt_file(tmp_path / "m.gguf", tmp_path / "m.enc", key, chunk_size=3000)
    crypto.decrypt_file(tmp_path / "m.enc", tmp_path / "out.gguf", key)
    assert (tmp_path / "out.gguf").read_bytes() == data


def test_big_file_truncation_and_wrong_key_fail(tmp_path):
    key = crypto.load_key(crypto.generate_key())
    (tmp_path / "m.gguf").write_bytes(os.urandom(10_000))
    crypto.encrypt_file(tmp_path / "m.gguf", tmp_path / "m.enc", key, chunk_size=3000)
    blob = (tmp_path / "m.enc").read_bytes()
    one_chunk = 4 + 12 + 4 + 3000 + 16
    (tmp_path / "cut.enc").write_bytes(blob[:one_chunk])  # only the first chunk
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_file(tmp_path / "cut.enc", tmp_path / "x", key)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_file(tmp_path / "m.enc", tmp_path / "x", crypto.load_key(crypto.generate_key()))


def test_rate_endpoint(embedder, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module
    from test_app import FakeSTT, FakeStore, FakeTTS
    from timer import SessionTimer
    monkeypatch.setattr(app_module.llm, "stream_reply", lambda messages, model=None: iter(["Hi."]))
    mem = Memory(embedder)
    assistant = app_module.Assistant(FakeSTT("hello"), FakeTTS(), mem, FakeStore(),
                                     SessionTimer(datetime.now(IST), 600))
    client = TestClient(app_module.create_app(assistant))
    lines = client.post("/api/turn", content=b"a").text.strip().splitlines()
    turn_id = __import__("json").loads(lines[-1])["turn_id"]
    assert client.post("/api/rate", json={"turn_id": turn_id, "value": 1}).json() == {"ok": True}
    assert mem.rating_stats() == {app_module.config.LLM_MODEL: (1, 0)}
    assert client.post("/api/rate", json={"value": 1}).status_code == 400
