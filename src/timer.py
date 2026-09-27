"""Session clock and idle timeout.

Pure logic with no clock of its own: callers pass `now` (timezone-aware datetime),
which keeps it easy to test. The usage limits live in budget.py.
"""
from datetime import datetime


class SessionTimer:
    def __init__(self, now: datetime, idle_limit_s: float):
        self.idle_limit_s = idle_limit_s
        self.last_tick = now
        self.last_interaction = now

    def tick(self, now: datetime) -> tuple[str, float]:
        """Advance the clock. Returns (day, seconds) to add to that day's usage."""
        delta = max(0.0, (now - self.last_tick).total_seconds())
        if now.date() != self.last_tick.date():  # passed midnight: count only the new day
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            delta = min(delta, (now - midnight).total_seconds())
        self.last_tick = now
        return now.date().isoformat(), delta

    def touch(self, now: datetime) -> None:
        """The user did something (opened the page or finished a turn)."""
        self.last_interaction = now

    def idle_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.last_interaction).total_seconds())

    def idle_reached(self, now: datetime) -> bool:
        return self.idle_seconds(now) >= self.idle_limit_s
