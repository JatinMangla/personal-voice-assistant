from datetime import datetime, timedelta, timezone

import pytest

import crypto
from intents import parse_command
from llm import SentenceChunker, build_messages
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=IST)


# --- crypto -------------------------------------------------------------------
def test_encrypt_round_trip():
    key = crypto.load_key(crypto.generate_key())
    blob = crypto.encrypt(b"secret memory", key)
    assert b"secret memory" not in blob
    assert crypto.decrypt(blob, key) == b"secret memory"


def test_wrong_key_fails():
    blob = crypto.encrypt(b"x", crypto.load_key(crypto.generate_key()))
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob, crypto.load_key(crypto.generate_key()))


def test_tampered_blob_fails():
    key = crypto.load_key(crypto.generate_key())
    blob = bytearray(crypto.encrypt(b"hello", key))
    blob[-1] ^= 1
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(bytes(blob), key)


def test_bad_key_rejected():
    with pytest.raises(ValueError):
        crypto.load_key("dG9vc2hvcnQ=")


# --- memory -------------------------------------------------------------------
def test_facts_survive_save_and_load(embedder):
    m = Memory(embedder)
    m.add_fact("my wife's birthday is 5 May", T0)
    m.add_turn("what is the capital of france", "Paris.", "en", T0)
    m.add_usage("2026-09-27", 120)

    m2 = Memory(embedder, m.to_bytes())
    assert m2.all_facts(10) == ["my wife's birthday is 5 May"]
    assert m2.recent_turns(5) == [("what is the capital of france", "Paris.")]
    assert m2.get_usage("2026-09-27") == 120


def test_search_finds_related(embedder):
    m = Memory(embedder)
    m.add_fact("my car is a red honda city", T0)
    m.add_fact("i like mango ice cream", T0)
    results = m.search("which car do i have honda", k=1, min_similarity=0.1)
    assert results == ["Fact: my car is a red honda city"]


def test_forget_removes_fact_and_matching_turns(embedder):
    m = Memory(embedder)
    m.add_fact("my bank pin is 1234", T0)
    m.add_fact("i like tea", T0)
    m.add_turn("my bank pin is 1234", "Okay.", "en", T0)
    fact, turns = m.forget("my bank pin is 1234", min_similarity=0.6)
    assert fact == "my bank pin is 1234"
    assert turns == 1
    assert m.all_facts(10) == ["i like tea"]
    assert b"1234" not in m.to_bytes()  # secure_delete: no leftovers in the file


def test_forget_nothing_matches(embedder):
    m = Memory(embedder)
    m.add_fact("i like tea", T0)
    assert m.forget("quantum physics lecture", min_similarity=0.6) == (None, 0)


def test_usage_accumulates(embedder):
    m = Memory(embedder)
    m.add_usage("d", 10)
    m.add_usage("d", 5)
    assert m.get_usage("d") == 15


# --- timer --------------------------------------------------------------------
def test_idle_after_10_minutes():
    t = SessionTimer(T0, idle_limit_s=600)
    assert not t.idle_reached(T0 + timedelta(minutes=9))
    assert t.idle_reached(T0 + timedelta(minutes=10))
    t.touch(T0 + timedelta(minutes=9))
    assert not t.idle_reached(T0 + timedelta(minutes=15))


def test_tick_returns_elapsed_time():
    t = SessionTimer(T0, idle_limit_s=600)
    assert t.tick(T0 + timedelta(seconds=5)) == ("2026-09-27", 5)
    assert t.tick(T0 + timedelta(seconds=12)) == ("2026-09-27", 7)


def test_new_day_counts_only_time_after_midnight():
    t = SessionTimer(T0, idle_limit_s=600)
    day, delta = t.tick(T0 + timedelta(hours=14, minutes=5))  # 00:05 next day
    assert (day, delta) == ("2026-09-28", 5 * 60)


# --- free-plan budget (90 % warn, 99 % stop) ------------------------------------
def test_daily_share_of_weekly_gpu():
    import config
    assert round(config.DAILY_LIMIT_MIN) == 257  # 30 h / 7 days


def test_meters_read_day_week_and_link_usage(embedder):
    from budget import current_meters
    m = Memory(embedder)
    m.add_usage("2026-09-27", 60 * 60)   # today: 1 h
    m.add_usage("2026-09-21", 2 * 3600)  # 6 days ago: inside the 7-day window
    m.add_usage("2026-09-20", 9 * 3600)  # 7 days ago: outside
    m.add_net("2026-09", 500, 2_000_000)
    meters = {x.name: x for x in current_meters(m, T0)}
    assert meters["day"].used == 3600
    assert meters["week"].used == 3 * 3600
    assert meters["requests"].used == 500 and meters["data"].used == 2_000_000


def test_warn_once_at_90_and_stop_at_99():
    from budget import Budget, Meter
    b = Budget(0.90, 0.99)
    at = lambda pct: [Meter("day", pct, 100)]
    assert b.new_notices(at(89)) == [] and b.over_stop(at(89)) is None
    assert [m.name for m in b.new_notices(at(90))] == ["day"]
    assert b.new_notices(at(95)) == []  # only once
    assert b.over_stop(at(98.9)) is None
    assert b.over_stop(at(99)).name == "day"


def test_minutes_until_stop_and_start_guard():
    from budget import Budget, Meter, minutes_until_stop
    day = Meter("day", 250 * 60, 257 * 60)
    assert minutes_until_stop(day, 0.99) == 4          # stops at 254.4 min
    assert minutes_until_stop(Meter("data", 1, 10), 0.99) is None
    assert Budget(0.9, 0.99).too_little_left([day], 5).name == "day"
    empty = Meter("day", 257 * 60, 257 * 60)           # 0 minutes left must also block
    assert Budget(0.9, 0.99).too_little_left([empty], 5) is not None


# --- intents ------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Remember that my wife's birthday is 5 May.", ("remember", "my wife's birthday is 5 May")),
    ("Jarvis, remember my car number is DL 3C 1234", ("remember", "my car number is DL 3C 1234")),
    ("याद रखना कि मेरी मीटिंग सोमवार को है।", ("remember", "मेरी मीटिंग सोमवार को है")),
    ("meri meeting monday ko hai, yaad rakhna", ("remember", "meri meeting monday ko hai")),
    ("Forget that my bank pin is 1234", ("forget", "my bank pin is 1234")),
    ("मेरा पिन भूल जाओ", ("forget", "मेरा पिन")),
    ("Remember when we talked about Goa?", None),
    ("What's the weather like?", None),
    ("Do you remember my car?", None),
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


# --- audio size -----------------------------------------------------------------
def test_speech_audio_is_mp3_and_small():
    import numpy as np
    from tts import encode_audio
    rng = np.random.default_rng(0)
    samples = (rng.standard_normal(24000 * 3) * 0.1).astype(np.float32)  # 3 s, hard to compress
    data = encode_audio(samples)
    assert data[:3] == b"ID3" or data[0] == 0xFF  # MP3, not WAV
    assert len(data) < 24000 * 3 * 2 / 4           # at least 4x smaller than WAV


# --- LLM helpers ------------------------------------------------------------------
def test_sentence_chunker_streams_sentences():
    c = SentenceChunker()
    out = []
    for piece in ["Hello there", ", how are you? I am", " fine. Thanks", " a lot"]:
        out += c.feed(piece)
    out += c.flush()
    assert out == ["Hello there, how are you?", "I am fine.", "Thanks a lot"]


def test_sentence_chunker_merges_very_short_sentences():
    c = SentenceChunker()
    assert c.feed("Okay. Sure thing, I can do that. Bye") == ["Okay. Sure thing, I can do that."]
    assert c.flush() == ["Bye"]


def test_sentence_chunker_hindi_danda():
    c = SentenceChunker()
    assert c.feed("आज मौसम अच्छा है। कल बारिश ") == ["आज मौसम अच्छा है।"]
    assert c.flush() == ["कल बारिश"]


def test_build_messages_includes_memory_and_history():
    msgs = build_messages("hi", "hi", ["likes tea"], ["On 2026-09-01 user said: x"],
                          [("q1", "a1")], T0)
    assert msgs[0]["role"] == "system"
    assert "likes tea" in msgs[0]["content"] and "Devanagari" in msgs[0]["content"]
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user"]
