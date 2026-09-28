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


def test_minutes_only_when_under_an_hour():
    late = datetime(2026, 9, 28, 23, 49, tzinfo=IST)
    assert describe_reset(Meter("day", 1, 2), late).endswith("in 11 min.")
