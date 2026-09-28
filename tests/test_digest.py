"""Day -> week -> month summaries of old conversations, used for questions about a period."""
from datetime import date, datetime, timedelta, timezone

import pytest

import digest
import query_hints
from memory import Memory

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 20, 0, tzinfo=IST)  # Monday


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def ask(period, material):
        calls.append((period, material))
        return f"Summary for {period}. Call 98765 43210 later."
    monkeypatch.setattr(digest, "_ask_llm", ask)
    return calls


def add(m, when, text):
    m.add_turn(text, "Okay.", "en", when)


def run_all(m, now=NOW):
    n = 0
    while digest.digest_one(m, now):
        n += 1
    return n


def test_days_then_weeks_then_months_only_for_finished_periods(embedder, fake_llm):
    m = Memory(embedder)
    add(m, datetime(2026, 8, 10, 9, tzinfo=IST), "planning the Goa trip")
    add(m, datetime(2026, 8, 11, 9, tzinfo=IST), "booked flights to Goa")
    add(m, datetime(2026, 9, 21, 9, tzinfo=IST), "working on the Kaggle notebook")
    add(m, NOW, "hello")  # today: not summarised yet
    assert run_all(m) == 3 + 2 + 1  # 3 days, 2 weeks, August (September isn't over)
    kinds = [c[0] for c in fake_llm]
    assert kinds[:3] == ["Monday 10 August 2026", "Tuesday 11 August 2026",
                         "Monday 21 September 2026"]
    assert "the week of 10 August 2026" in kinds and "August 2026" in kinds
    assert "planning the Goa trip" in fake_llm[0][1]
    assert run_all(m) == 0  # nothing left


def test_numbers_are_masked(embedder, fake_llm):
    m = Memory(embedder)
    add(m, datetime(2026, 9, 20, 9, tzinfo=IST), "my number is 98765 43210")
    run_all(m)
    assert "98765" not in fake_llm[0][1]
    stored = m.conn.execute("SELECT text FROM digests").fetchone()[0]
    assert "98765" not in stored and "…" in stored


def test_period_question_gets_the_summaries_first(embedder, fake_llm):
    m = Memory(embedder)
    for d in (3, 12, 25):
        add(m, datetime(2026, 8, d, 9, tzinfo=IST), f"worked on project {d}")
    run_all(m)
    q = "what did I work on last month"
    found = m.search(q, k=5, min_similarity=0.8, hints=query_hints.parse(q, NOW))
    assert found[0] == "Summary of month 2026-08: Summary for August 2026. Call … later."
    assert sum(f.startswith("On 2026-08") for f in found) == 3
    q = "what did we talk about last week"  # no digests for that week: turns only
    assert not any(f.startswith("Summary") for f in
                   m.search(q, k=5, min_similarity=0.8, hints=query_hints.parse(q, NOW)))


def test_week_question_uses_week_or_day_summaries(embedder, fake_llm):
    m = Memory(embedder)
    add(m, datetime(2026, 9, 22, 9, tzinfo=IST), "fixed the speaker bug")
    run_all(m, NOW + timedelta(days=7))
    lines = digest.for_range(m, date(2026, 9, 21), date(2026, 9, 28))
    assert lines == ["Summary of week of 2026-09-21: Summary for the week of 21 September 2026. "
                     "Call … later."]
    assert digest.for_range(m, date(2026, 9, 22), date(2026, 9, 23))[0].startswith(
        "Summary of 2026-09-22")


def test_forget_drops_the_summaries_of_those_days(embedder, fake_llm):
    m = Memory(embedder)
    add(m, datetime(2026, 8, 10, 9, tzinfo=IST), "my secret plan is zebra surprise party")
    add(m, datetime(2026, 8, 20, 9, tzinfo=IST), "bought a new phone")
    run_all(m)
    assert m.conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 5  # 2 d, 2 w, 1 m
    m.forget("my secret plan is zebra surprise party", 0.8)
    left = m.conn.execute("SELECT kind, start FROM digests ORDER BY kind, start").fetchall()
    assert left == [("day", "2026-08-20"), ("week", "2026-08-17")]  # Aug 10's day/week/month gone
    fake_llm.clear()
    assert run_all(m) == 1 and fake_llm[0][0] == "August 2026"  # written again without it
    assert "zebra" not in fake_llm[0][1]


def test_assistant_writes_digests_while_idle_and_stops_on_errors(embedder, monkeypatch):
    import app as app_module
    from test_app import FakeStore, FakeTTS
    from timer import SessionTimer
    m = Memory(embedder)
    add(m, datetime(2026, 8, 10, 9, tzinfo=IST), "planning the Goa trip")
    a = app_module.Assistant(None, FakeTTS(), m, FakeStore(), SessionTimer(NOW, 600))

    def busy(period, material):
        raise ConnectionError("ollama busy")
    monkeypatch.setattr(digest, "_ask_llm", busy)
    assert a.digest_one() is False and a.digest_stopped
    assert a.digest_one() is False  # not retried in this session
