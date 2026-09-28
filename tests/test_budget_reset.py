"""When each free limit resets, in India time (IST, GMT+5:30)."""
from datetime import datetime, timedelta, timezone

from budget import Meter, describe_reset, reset_time

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 20, 40, tzinfo=IST)  # 8:40 PM IST


def test_day_resets_at_midnight_ist():
    day = Meter("day", 250 * 60, 257 * 60)
    assert reset_time(day, NOW) == datetime(2026, 9, 29, 0, 0, tzinfo=IST)
    assert describe_reset(day, NOW) == \
        "Resets at 12:00 AM (midnight) India time, in 3 h 20 min."
    assert describe_reset(day, NOW, "hi") == \
        "रात 12 बजे (भारतीय समय) रीसेट होगा, 3 घंटे 20 मिनट में।"


def test_week_gives_time_back_every_midnight():
    week = Meter("week", 28 * 3600, 30 * 3600)
    assert "every midnight (12:00 AM India time)" in describe_reset(week, NOW)
    assert describe_reset(week, NOW).endswith("next in 3 h 20 min.")


def test_monthly_link_limits_reset_on_the_first():
    requests = Meter("requests", 18_500, 20_000)
    assert reset_time(requests, NOW) == datetime(2026, 10, 1, 0, 0, tzinfo=IST)
    assert describe_reset(requests, NOW) == \
        "Resets on 1 October at 12:00 AM India time, in 2 days."
    december = datetime(2026, 12, 15, 9, 0, tzinfo=IST)
    assert reset_time(requests, december) == datetime(2027, 1, 1, 0, 0, tzinfo=IST)


def test_usage_summary_tells_jarvis_the_time_left():
    from budget import usage_summary
    meters = [Meter("day", 246 * 60, 257 * 60), Meter("week", 12.3 * 3600, 30 * 3600),
              Meter("requests", 1234, 20_000), Meter("data", 45e6, 1e9)]
    text = usage_summary(meters, NOW, 0.99)
    assert "GPU time today: 246 of 257 minutes used, 8 minutes left" in text
    assert "Resets at 12:00 AM (midnight) India time, in 3 h 20 min." in text
    assert "12.3 of 30 hours used" in text
    assert "1,234 of 20,000 requests" in text and "45 of 1000 MB" in text
    assert "closes after 10 minutes without use" in text


def test_jarvis_gets_the_live_usage_with_every_question(embedder, monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient
    from memory import Memory
    from timer import SessionTimer
    sent = []
    monkeypatch.setattr(app_module.llm, "stream_reply",
                        lambda messages, model=None: sent.append(messages) or iter(["OK."]))
    monkeypatch.setattr(app_module.learn, "learn", lambda m, t, n: [])

    class STT:
        def transcribe(self, audio, quiet=False):
            return "how much GPU time is left today", "en"

    class TTS:
        pipelines = {"en": None}

        def synthesize(self, text, lang):
            return b"RIFF"

    class Store:
        last_saved = None

    memory = Memory(embedder)
    today = app_module.now_ist().date().isoformat()
    memory.add_usage(today, 100 * 60)
    a = app_module.Assistant(STT(), TTS(), memory, Store(), SessionTimer(app_module.now_ist(), 600))
    TestClient(app_module.create_app(a)).post("/api/turn", content=b"x")
    system = sent[0][0]["content"]
    assert "Your free usage limits right now" in system
    assert "GPU time today: 100 of 257 minutes used, 154 minutes left" in system


def test_minutes_only_when_under_an_hour():
    late = datetime(2026, 9, 28, 23, 49, tzinfo=IST)
    assert describe_reset(Meter("day", 1, 2), late).endswith("in 11 min.")
