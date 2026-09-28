"""Time summaries of old conversations: day -> week -> month (the useful idea from
"vectorless RAG", see docs/MEMORY_RESEARCH.md).

While you're quiet, Jarvis writes a 1-3 sentence digest of each finished day, then of
each finished week and month from those. Questions about a period ("what did I work on
last month?", "pichhle hafte kya kiya?") get the digests for it, so a whole month fits
in a few lines instead of 5 random conversations.

Privacy: digests are written from your own messages and Jarvis's replies, in RAM and in
the encrypted memory only; long numbers are masked; "forget" deletes the digests of
the days it removed conversations from (they are written again without them).
"""
import re
from datetime import date, datetime, timedelta

import requests

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,    -- day, week, month
    start TEXT NOT NULL,   -- first day, YYYY-MM-DD
    end TEXT NOT NULL,     -- day after the last day (exclusive)
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(kind, start)
);
"""

PROMPT = """You keep the diary of a personal voice assistant. Summarise what the user talked
about with the assistant in the period below, in 1-3 short English sentences, third person
("User asked about..., planned..., decided..."). Mention topics, plans, decisions and
events; skip greetings and small talk. Never include passwords, PINs, OTPs, card, bank or
phone numbers, or exact addresses. Write only the summary."""


def mask(text: str) -> str:
    """Long digit runs (phone/card/account numbers) never go into a digest."""
    return re.sub(r"\d[\d\s-]{4,}\d", "…", text)


def _ask_llm(period: str, material: str) -> str:
    resp = requests.post(f"{config.OLLAMA_URL}/api/chat", timeout=(5, 120), json={
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": f"Period: {period}\n\n{material}"}],
        "stream": False, "think": False, "keep_alive": "60m",
        # same num_ctx as every other request: a different one makes Ollama reload the model
        "options": {"temperature": 0.2, "num_predict": 160, "num_ctx": config.LLM_CONTEXT_TOKENS},
    })
    resp.raise_for_status()
    return " ".join(resp.json()["message"]["content"].split())[:500]


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_job(memory, today: date) -> tuple[str, date, date, str] | None:
    """The oldest missing digest: (kind, start, end, material to summarise), or None.
    Only finished periods: days before today, weeks before this week, months before
    this month."""
    with memory.lock:
        conn = memory.conn
        day = conn.execute(
            "SELECT substr(ts, 1, 10) AS d FROM turns WHERE d < ? AND d NOT IN "
            "(SELECT start FROM digests WHERE kind = 'day') GROUP BY d ORDER BY d LIMIT 1",
            (today.isoformat(),)).fetchone()
        if day:
            rows = conn.execute("SELECT user_text, reply FROM turns WHERE substr(ts, 1, 10) = ? "
                                "ORDER BY id LIMIT 60", (day[0],)).fetchall()
            start = date.fromisoformat(day[0])
            material = "\n".join(f"- User: {u[:300]} | Assistant: {r[:200]}" for u, r in rows)
            return "day", start, start + timedelta(days=1), mask(material)
        days = conn.execute("SELECT start, text FROM digests WHERE kind = 'day' ORDER BY start"
                            ).fetchall()
        have = {(k, s) for k, s in conn.execute("SELECT kind, start FROM digests")}
    for kind, first_of, limit in (
            ("week", lambda d: _week_start(d), _week_start(today)),
            ("month", lambda d: d.replace(day=1), today.replace(day=1))):
        groups: dict[date, list[str]] = {}
        for start, text in days:
            key = first_of(date.fromisoformat(start))
            groups.setdefault(key, []).append(f"- {start}: {text}")
        for key in sorted(groups):
            end = key + timedelta(days=7) if kind == "week" else \
                (key + timedelta(days=32)).replace(day=1)
            if end <= limit and (kind, key.isoformat()) not in have:
                return kind, key, end, "\n".join(groups[key])
    return None


def digest_one(memory, now: datetime) -> bool:
    """Write ONE missing digest. False when there is nothing left to do."""
    job = next_job(memory, now.date())
    if not job:
        return False
    kind, start, end, material = job
    period = {"day": f"{start:%A %d %B %Y}",
              "week": f"the week of {start:%d %B %Y}",
              "month": f"{start:%B %Y}"}[kind]
    text = mask(_ask_llm(period, material)) or "(nothing notable)"
    with memory.lock:
        memory.conn.execute(
            "INSERT OR REPLACE INTO digests (kind, start, end, ts, text) VALUES (?, ?, ?, ?, ?)",
            (kind, start.isoformat(), end.isoformat(), now.isoformat(timespec="seconds"), text))
        memory.conn.commit()
        memory.dirty = True
    return True


def for_range(memory, start: date, end: date, limit: int = 6) -> list[str]:
    """Digests for a question about [start, end): months for long ranges, else weeks,
    else days. Lines ready for the prompt."""
    span = (end - start).days
    kinds = ["month", "week", "day"] if span >= 28 else ["week", "day"] if span >= 7 else ["day"]
    with memory.lock:
        for kind in kinds:
            rows = memory.conn.execute(
                "SELECT start, text FROM digests WHERE kind = ? AND start >= ? AND start < ? "
                "ORDER BY start", (kind, start.isoformat(), end.isoformat())).fetchall()
            if rows:
                label = {"day": "", "week": "week of ", "month": "month "}[kind]
                return [f"Summary of {label}{s if kind != 'month' else s[:7]}: {t}"
                        for s, t in rows[-limit:]]
    return []


def drop_for_days(memory, days: set[str]) -> None:
    """After "forget": remove digests that cover these days (call with memory.lock)."""
    for day in days:
        memory.conn.execute("DELETE FROM digests WHERE start <= ? AND end > ?", (day, day))
