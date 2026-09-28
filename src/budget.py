"""Free-plan limits: warn once at NOTIFY_AT (90 %), stop at STOP_AT (99 %).

Four meters are watched:
    day       GPU minutes used today       vs 30 h / 7
    week      GPU minutes, last 7 days     vs 30 h  (rolling, so any Kaggle week is safe)
    requests  ngrok requests this month    vs 20,000
    data      ngrok data sent this month   vs 1 GB
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import config


@dataclass
class Meter:
    name: str
    used: float
    limit: float

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 0.0

    @property
    def percent(self) -> int:
        return int(self.fraction * 100)


def current_meters(memory, now: datetime) -> list[Meter]:
    today = now.date()
    week_start = (today - timedelta(days=6)).isoformat()
    requests_used, bytes_used = memory.get_net(now.strftime("%Y-%m"))
    return [
        Meter("day", memory.get_usage(today.isoformat()), config.DAILY_LIMIT_MIN * 60),
        Meter("week", memory.get_usage_since(week_start), config.GPU_WEEKLY_HOURS * 3600),
        Meter("requests", requests_used, config.NGROK_MONTHLY_REQUESTS),
        Meter("data", bytes_used, config.NGROK_MONTHLY_BYTES),
    ]


def reset_time(meter: Meter, now: datetime) -> datetime:
    """When the meter gives time/room back, in India time (now must be IST-aware).
    day: at midnight. week (rolling 7 days): each midnight the oldest day drops out.
    requests/data: on the 1st of next month."""
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if meter.name in ("day", "week"):
        return midnight
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1)


def _in(delta: timedelta, lang: str) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    days, hours, mins = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if lang == "hi":
        if days:
            return f"{days} दिन में"
        return f"{hours} घंटे {mins} मिनट में" if hours else f"{mins} मिनट में"
    if days:
        return f"in {days} day{'s' if days > 1 else ''}"
    return f"in {hours} h {mins} min" if hours else f"in {mins} min"


def describe_reset(meter: Meter, now: datetime, lang: str = "en") -> str:
    """e.g. 'Resets at 12:00 AM (midnight) India time, in 3 h 20 min.'"""
    at = reset_time(meter, now)
    when = _in(at - now, lang)
    if meter.name == "day":
        return (f"रात 12 बजे (भारतीय समय) रीसेट होगा, {when}।" if lang == "hi"
                else f"Resets at 12:00 AM (midnight) India time, {when}.")
    if meter.name == "week":
        return (f"हर रात 12 बजे (भारतीय समय) सबसे पुराने दिन का समय वापस मिलता है, अगली बार {when}।"
                if lang == "hi" else
                f"Some time comes back every midnight (12:00 AM India time) as the oldest "
                f"day drops out; next {when}.")
    date = at.strftime("%d %B").lstrip("0")
    return (f"{date} को रात 12 बजे (भारतीय समय) रीसेट होगा, {when}।" if lang == "hi"
            else f"Resets on {date} at 12:00 AM India time, {when}.")


def usage_summary(meters: list[Meter], now: datetime, stop_at: float) -> str:
    """Live free-limit status for the assistant, so it can answer "how much time is left
    today?" / "when does it reset?" correctly."""
    m = {meter.name: meter for meter in meters}
    parts = []
    if "day" in m:
        day = m["day"]
        parts.append(f"GPU time today: {day.used / 60:.0f} of {day.limit / 60:.0f} minutes used, "
                     f"{minutes_until_stop(day, stop_at)} minutes left before the automatic "
                     f"stop. {describe_reset(day, now)}")
    if "week" in m:
        week = m["week"]
        parts.append(f"GPU time in the last 7 days: {week.used / 3600:.1f} of "
                     f"{week.limit / 3600:.0f} hours used "
                     f"({minutes_until_stop(week, stop_at)} minutes left).")
    if "requests" in m and "data" in m:
        parts.append(f"Free link this month: {m['requests'].used:,.0f} of "
                     f"{m['requests'].limit:,.0f} requests and {m['data'].used / 1e6:.0f} of "
                     f"{m['data'].limit / 1e6:.0f} MB used. {describe_reset(m['requests'], now)}")
    parts.append(f"Each session also closes after {config.IDLE_TIMEOUT_MIN} minutes without use.")
    return " ".join(parts)


def minutes_until_stop(meter: Meter, stop_at: float) -> int | None:
    """For the GPU-time meters: minutes left before the automatic stop."""
    if meter.name not in ("day", "week"):
        return None
    return max(0, int((meter.limit * stop_at - meter.used) // 60))


class Budget:
    def __init__(self, notify_at: float, stop_at: float):
        self.notify_at = notify_at
        self.stop_at = stop_at
        self.notified: set[str] = set()

    def over_stop(self, meters: list[Meter]) -> Meter | None:
        return next((m for m in meters if m.fraction >= self.stop_at), None)

    def too_little_left(self, meters: list[Meter], min_minutes: float) -> Meter | None:
        """A GPU-time meter with less than `min_minutes` before the auto-stop."""
        for m in meters:
            left = minutes_until_stop(m, self.stop_at)
            if left is not None and left < min_minutes:
                return m
        return None

    def warnings(self, meters: list[Meter]) -> list[Meter]:
        return [m for m in meters if m.fraction >= self.notify_at]

    def new_notices(self, meters: list[Meter]) -> list[Meter]:
        """Meters that reached the warning level and were not announced yet."""
        new = [m for m in self.warnings(meters) if m.name not in self.notified]
        self.notified.update(m.name for m in new)
        return new


# --- a numbers-only copy of the usage, for the Start button ---------------------------
# Saved next to the memory as its own small encrypted file, so the GitHub Start button
# can refuse to start (before any GPU time is used) without ever opening the memory
# itself: it holds GPU seconds per day and link counts, nothing else.
SNAPSHOT_DAYS = 8


def usage_snapshot(memory, now: datetime) -> dict:
    first = (now.date() - timedelta(days=SNAPSHOT_DAYS - 1)).isoformat()
    month = now.strftime("%Y-%m")
    return {"version": 1, "saved": now.isoformat(timespec="seconds"),
            "days": memory.usage_days(first), "net": {month: list(memory.get_net(month))}}


class SnapshotUsage:
    """Reads a usage snapshot like a Memory, so current_meters() works on it."""

    def __init__(self, snapshot: dict):
        self.days = {d: float(s) for d, s in snapshot.get("days", {}).items()}
        self.net = {m: tuple(v) for m, v in snapshot.get("net", {}).items()}

    def get_usage(self, day: str) -> float:
        return self.days.get(day, 0.0)

    def get_usage_since(self, first_day: str) -> float:
        return sum(s for d, s in self.days.items() if d >= first_day)

    def get_net(self, month: str) -> tuple[int, int]:
        return self.net.get(month, (0, 0))


def start_blocker(meters: list[Meter], budget: "Budget") -> Meter | None:
    """The limit that forbids starting now, if any: already at the auto-stop, or too
    little left for a session (start-up alone takes a few minutes)."""
    return budget.over_stop(meters) or budget.too_little_left(meters, config.MIN_START_MIN)
