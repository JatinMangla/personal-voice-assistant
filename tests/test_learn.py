import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import learn
import websearch
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 28, 10, 0, tzinfo=IST)


@pytest.mark.parametrize("text, check", [
    ("I am allergic to peanuts", True),
    ("my daughter loves dancing", True),
    ("मुझे मूंगफली से एलर्जी है", True),
    ("mujhe peanuts se allergy hai", True),
    ("you can't talk to me without internet", True),       # a correction
    ("आप बिना इंटरनेट के बात नहीं कर सकते", True),
    ("what is the weather", False),
    ("hello", False),
])
def test_worth_checking(text, check):
    assert learn.worth_checking(text) is check


def test_correction_about_the_assistant_is_kept_and_always_shown(embedder, monkeypatch):
    m = Memory(embedder)
    fake_llm(monkeypatch, [{"fact": "The assistant needs internet; it cannot work offline.",
                            "category": "assistant", "replaces": 0}])
    assert learn.learn(m, "no, you can't talk to me without internet", T0)
    facts = m.facts_for_prompt("tell me a joke", limit=1,
                               always_categories=("health", "diet", "assistant"))
    assert facts == ["The assistant needs internet; it cannot work offline."]


def test_decisions_events_and_goals_are_learned_too(embedder, monkeypatch):
    m = Memory(embedder)
    fake_llm(monkeypatch, [
        {"fact": "User bought a Honda City in September 2026.", "category": "event", "replaces": 0},
        {"fact": "User decided to learn Python.", "category": "decision", "replaces": 0},
        {"fact": "User wants to run a marathon next year.", "category": "goal", "replaces": 0}])
    assert len(learn.learn(m, "I bought a Honda City yesterday and I decided to learn "
                              "Python; my goal is a marathon", T0)) == 3
    cats = {c for (c,) in m.conn.execute("SELECT category FROM facts")}
    assert cats == {"event", "decision", "goal"}


def test_patterns_are_found_over_recent_messages(embedder, monkeypatch):
    m = Memory(embedder)
    for i in range(7):
        m.add_turn(f"help me with my kaggle voice assistant step {i}", "Sure.", "en", T0)
    seen = fake_llm(monkeypatch, [{"fact": "User often works on their Kaggle voice assistant.",
                                   "category": "other", "replaces": 0}])
    assert learn.reflect(m, T0) == ["User often works on their Kaggle voice assistant."]
    assert seen["prompt"] == learn.REFLECT_PROMPT and "step 6" in seen["text"]
    row = m.conn.execute("SELECT category FROM facts").fetchone()
    assert row == ("pattern",)                      # always stored as a pattern
    assert m.unreflected_turns(50) == []            # those messages won't be reviewed again
    assert learn.reflect(m, T0) == []               # nothing new -> no call


def test_too_few_messages_for_patterns(embedder, monkeypatch):
    m = Memory(embedder)
    m.add_turn("hello", "Hi.", "en", T0)
    fake_llm(monkeypatch, [{"fact": "x", "category": "pattern", "replaces": 0}])
    assert learn.reflect(m, T0) == []


def test_assistant_knows_it_needs_internet():
    from llm import system_prompt
    prompt = system_prompt("en", [], [], T0)
    assert "no offline mode" in prompt and "cloud GPU" in prompt
    assert "say so honestly instead of guessing" in prompt


def fake_llm(monkeypatch, facts):
    seen = {}

    def ask(user_text, known, now=None, **kwargs):
        seen["known"] = known
        seen["prompt"] = kwargs.get("prompt", learn.EXTRACT_PROMPT)
        seen["text"] = user_text
        return facts
    monkeypatch.setattr(learn, "_ask_llm", ask)
    return seen


def test_learns_health_fact(embedder, monkeypatch):
    m = Memory(embedder)
    fake_llm(monkeypatch, [{"fact": "User is allergic to peanuts.", "category": "health"}])
    assert learn.learn(m, "I am allergic to peanuts", T0) == ["User is allergic to peanuts."]
    row = m.conn.execute("SELECT text, category, source FROM facts").fetchone()
    assert row == ("User is allergic to peanuts.", "health", "auto")


def test_duplicate_is_skipped(embedder, monkeypatch):
    m = Memory(embedder)
    m.add_fact("User is allergic to peanuts.", T0, "health", "auto")
    fake_llm(monkeypatch, [{"fact": "User is allergic to peanuts.", "category": "health"}])
    assert learn.learn(m, "I am allergic to peanuts, remember", T0) == []
    assert len(m.all_facts(10)) == 1


def test_changed_fact_replaces_old_one(embedder, monkeypatch):
    m = Memory(embedder)
    m.add_fact("User eats chicken.", T0, "diet", "auto")
    seen = fake_llm(monkeypatch, [{"fact": "User is vegetarian now.", "category": "diet",
                                   "replaces": 1}])
    assert learn.learn(m, "I eat chicken no more, I am vegetarian now", T0)
    assert seen["known"] == ["User eats chicken."]
    assert m.all_facts(10) == ["User is vegetarian now."]


def test_private_looking_things_are_never_learned(embedder, monkeypatch):
    m = Memory(embedder)
    fake_llm(monkeypatch, [{"fact": "User's PIN is 4821.", "category": "other"}])
    assert learn.learn(m, "my card PIN is 4821", T0) == []  # skipped before asking
    assert learn.learn(m, "my favourite numbers are fun", T0) == []  # fact itself private
    assert m.all_facts(10) == []


def test_health_facts_always_in_prompt_others_by_relevance(embedder):
    m = Memory(embedder)
    m.add_fact("User is allergic to peanuts.", T0, "health", "auto")
    m.add_fact("my car is a red honda", T0)                       # said "remember": always
    m.add_fact("User likes cricket.", T0, "preference", "auto")
    m.add_fact("User likes jazz music.", T0, "preference", "auto")
    facts = m.facts_for_prompt("give me a recipe for satay chicken", limit=3,
                               always_categories=("health", "diet"))
    assert "User is allergic to peanuts." in facts and "my car is a red honda" in facts
    assert len(facts) == 3  # one relevance slot left for the rest
    facts = m.facts_for_prompt("any jazz music tips", limit=3, always_categories=("health",))
    assert facts[-1] == "User likes jazz music."


# --- in the running app -------------------------------------------------------------
class FakeSTT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, quiet=False):
        return self.text, "en"


class FakeTTS:
    pipelines = {"en": None}

    def __init__(self):
        self.spoken = []

    def synthesize(self, text, lang):
        self.spoken.append(text)
        return b"RIFF"


class FakeStore:
    last_saved = None

    def save(self, data):
        return True


def run_turn(embedder, monkeypatch, text, reply, learned=(), links=()):
    def fake_reply(messages, model=None):
        for link in links:
            websearch._keep_link(*link)
        return iter(reply)
    monkeypatch.setattr(app_module.llm, "stream_reply", fake_reply)
    monkeypatch.setattr(app_module.learn, "learn", lambda m, t, n: list(learned))
    tts = FakeTTS()
    assistant = app_module.Assistant(FakeSTT(text), tts, Memory(embedder), FakeStore(),
                                     SessionTimer(datetime.now(IST), 600))
    body = TestClient(app_module.create_app(assistant)).post("/api/turn", content=b"a").text
    return [json.loads(l) for l in body.splitlines() if l.strip()], tts


def test_links_are_sent_to_the_page_but_never_spoken(embedder, monkeypatch):
    events, tts = run_turn(embedder, monkeypatch, "give me the kesariya youtube link",
                           ["Here it is: https://www.youtube.com/watch?v=BddP6PYo2gs"],
                           links=[("Kesariya", "https://www.youtube.com/watch?v=BddP6PYo2gs",
                                   "YouTube · 2:53"), ("bad", "javascript:alert(1)")])
    links = next(e for e in events if e["type"] == "links")["links"]
    assert len(links) == 1  # the javascript: link is dropped
    assert links[0]["url"] == "https://www.youtube.com/watch?v=BddP6PYo2gs"
    assert links[0]["site"] == "youtube.com" and links[0]["note"] == "YouTube · 2:53"
    assert tts.spoken == ["Here it is:"]


def test_learning_happens_quietly_in_the_background(embedder, monkeypatch):
    seen = []
    monkeypatch.setattr(app_module.llm, "stream_reply", lambda m, model=None: iter(["Noted."]))

    def slow_learn(memory, text, now):
        seen.append(text)
        return ["User is allergic to peanuts."]
    monkeypatch.setattr(app_module.learn, "learn", slow_learn)
    assistant = app_module.Assistant(FakeSTT("I am allergic to peanuts"), FakeTTS(),
                                     Memory(embedder), FakeStore(),
                                     SessionTimer(datetime.now(IST), 600))
    body = TestClient(app_module.create_app(assistant)).post("/api/turn", content=b"a").text
    events = [json.loads(l) for l in body.splitlines() if l.strip()]
    assert not any(e["type"] == "learned" for e in events)  # nothing shown in the chat
    assistant.learn_thread.join(timeout=5)
    assert seen == ["I am allergic to peanuts"]
    assert not assistant.turn_lock.locked()  # the next question is never blocked
