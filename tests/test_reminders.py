"""Reminders, to-dos and bills: stored in memory, told when Jarvis is opened, optional push."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import llm
import reminders as rem
import tools
from memory import Memory
from test_app import FakeStore, FakeTTS, events
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=IST)


@pytest.fixture
def r(embedder):
    return rem.Reminders(Memory(embedder))


@pytest.mark.parametrize("value,expected", [
    ("2026-09-28 18:00", datetime(2026, 9, 28, 18, 0)),
    ("2026-09-28T18:00:00", datetime(2026, 9, 28, 18, 0)),
    ("2026-10-05", datetime(2026, 10, 5, 9, 0)),
    ("18:00", datetime(2026, 9, 28, 18, 0)),
    ("09:30", datetime(2026, 9, 29, 9, 30)),   # already past today: tomorrow
    ("", None),
])
def test_parse_due(value, expected):
    assert rem.parse_due(value, NOW) == expected


def test_parse_due_rejects_nonsense():
    with pytest.raises(ValueError):
        rem.parse_due("sometime soon", NOW)


@pytest.mark.parametrize("repeat,due,expected", [
    ("daily", datetime(2026, 9, 28, 9), datetime(2026, 9, 29, 9)),
    ("weekly", datetime(2026, 9, 20, 9), datetime(2026, 10, 4, 9)),   # skips past dates
    ("monthly", datetime(2026, 8, 31, 9), datetime(2026, 9, 30, 9)),  # short month
    ("monthly", datetime(2026, 12, 5, 9), datetime(2027, 1, 5, 9)),
    ("yearly", datetime(2024, 2, 29, 9), datetime(2027, 2, 28, 9)),
])
def test_next_due(repeat, due, expected):
    assert rem.next_due(due, repeat, NOW) == expected


def test_add_list_complete_todo(r):
    assert "Saved: #1 buy milk" in r.add("buy milk", NOW)
    assert "you can't ring while switched off" in r.add("call mom", NOW, "2026-09-28 18:00")
    assert r.overview(NOW).splitlines()[0].startswith("#2 call mom — due Mon 28 Sep, 6:00 PM")
    assert r.complete("milk", NOW) == "Marked done: buy milk."
    assert [x["text"] for x in r.pending()] == ["call mom"]


def test_bill_repeats_and_logs_payments(r):
    r.add("pay electricity bill", NOW, "2026-10-05", "monthly", 1200)
    assert "Next time: #1 pay electricity bill (₹1,200, monthly) — due Thu 5 Nov" in \
        r.complete("electricity", NOW.replace(day=30))
    r.complete("#1", NOW.replace(day=30), amount=1350)
    text = r.overview(NOW.replace(day=30))
    assert "₹1,200" in text and "₹1,350" in text and "Total: ₹2,550" in text


def test_ambiguous_and_unknown(r):
    r.add("call mom", NOW)
    r.add("call dad", NOW)
    assert r.complete("call", NOW).startswith("Several match")
    assert r.complete("pay rent", NOW).startswith("No matching")
    assert r.delete("dad", NOW) == "Deleted: call dad."


def test_bad_input_is_explained(r):
    assert r.add("", NOW).startswith("Not saved")
    assert r.add("x", NOW, "whenever").startswith("Not saved")
    assert r.add("x", NOW, None, "daily").startswith("Not saved")


def test_due_reminders_are_announced_once(r):
    r.add("call mom", NOW, "2026-09-28 17:30")          # within the hour
    r.add("dentist", NOW, "2026-09-27 10:00")           # overdue
    r.add("book trip", NOW, "2026-10-10 10:00")         # later
    assert [x["text"] for x in r.to_announce(NOW)] == ["dentist", "call mom"]
    assert r.to_announce(NOW) == []
    assert "(OVERDUE)" in r.prompt_summary(NOW)


def test_reminders_survive_save_and_load(embedder):
    m = Memory(embedder)
    rem.Reminders(m).add("call mom", NOW, "2026-09-28 18:00")
    again = rem.Reminders(Memory(embedder, m.to_bytes()))
    assert [x["text"] for x in again.pending()] == ["call mom"]


class FakePush:
    def __init__(self):
        self.sent = []

    def schedule(self, due, text, now):
        self.sent.append((due, text))
        return True


def test_push_scheduled_once_within_three_days(embedder):
    push = FakePush()
    r = rem.Reminders(Memory(embedder), push)
    assert "phone notification is scheduled" in r.add("call mom", NOW, "2026-09-28 18:00")
    r.add("far away", NOW, "2026-11-01 10:00")          # > 3 days: scheduled at a later start
    assert r.schedule_pushes(NOW) == 0                  # already scheduled: not again
    assert push.sent == [(datetime(2026, 9, 28, 18, 0), "call mom")]
    r.complete("mom", NOW)
    assert "phone notification at the due time" in r.prompt_summary(NOW)


def test_ntfy_message_is_generic_by_default(monkeypatch):
    sent = {}

    def fake_post(url, data=None, timeout=None, headers=None):
        sent.update(url=url, data=data, headers=headers)
        return type("R", (), {"ok": True})()
    monkeypatch.setattr(rem.requests, "post", fake_post)
    assert rem.Push("my-secret-topic").schedule(datetime(2026, 9, 28, 18, 0), "see doctor", NOW)
    assert sent["url"] == "https://ntfy.sh/my-secret-topic"
    assert b"doctor" not in sent["data"]                 # the reminder text stays private
    assert int(sent["headers"]["At"]) == int(NOW.timestamp()) + 3600


def test_tools_use_the_clock(r, monkeypatch):
    monkeypatch.setattr(tools, "REGISTRY", {})
    rem.register(r, lambda: NOW)
    assert "Saved" in tools.run("add_reminder", {"text": "water plants", "due": "19:00"})
    assert "water plants" in tools.run("list_reminders", {})
    assert tools.run("complete_reminder", {"which": "plants"}).startswith("Marked done")
    prompt = llm.system_prompt("en", [], [], NOW)
    assert "add_reminder" in prompt


def test_turn_mentions_due_reminder_and_page_lists_it(embedder, monkeypatch):
    monkeypatch.setattr(app_module.llm, "stream_reply", lambda messages, model=None: iter(["Hi."]))
    monkeypatch.setattr(app_module.learn, "learn", lambda *a: [])
    memory = Memory(embedder)
    reminders = rem.Reminders(memory)
    now = datetime.now(IST)
    reminders.add("take medicine", now, (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"))

    class STT:
        def transcribe(self, audio, quiet=False):
            return "hello there", "en"
    a = app_module.Assistant(STT(), FakeTTS(), memory, FakeStore(), SessionTimer(now, 600),
                             reminders=reminders)
    client = TestClient(app_module.create_app(a))
    assert client.get("/api/reminders").json()["due"][0]["text"] == "take medicine"
    said = [e["text"] for e in events(client.post("/api/turn", content=b"a")) if e["type"] == "sentence"]
    assert said == ["Hi.", "Reminder: take medicine."]
    said = [e["text"] for e in events(client.post("/api/turn", content=b"a")) if e["type"] == "sentence"]
    assert said == ["Hi."]  # only once per session
