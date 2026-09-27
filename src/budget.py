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
