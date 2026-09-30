"""Reminders, to-dos and bills ("remind me to call mom at 6", "pay the electricity bill of
₹1,200 on the 5th every month", "add milk to my to-do list").

They live in the encrypted memory. Jarvis runs only when you start it, so it can't ring
at the due time. Instead:
- due and overdue reminders are spoken after your first question and shown as notes
  when the page opens, and the ones due soon are in every prompt;
- optional free phone notification: set the NTFY_TOPIC secret and subscribe to that
  topic in the ntfy web app or app. ntfy.sh then delivers a scheduled message at the
  due time, even while Jarvis is off (up to 3 days ahead; later ones are scheduled at a
  later start). The message is generic ("You have a reminder") unless
  config.NTFY_INCLUDE_TEXT is True: your reminder text never leaves the memory.

Bills: a reminder with an amount. Marking it paid logs the payment (for "how much did I
pay for electricity?") and moves a repeating one to its next date.
"""
import calendar
import hashlib
import json
import re
from datetime import datetime, timedelta

import requests

import config
import tools

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    created TEXT NOT NULL,
    text TEXT NOT NULL,
    due TEXT,              -- 'YYYY-MM-DDTHH:MM' India time; NULL = a to-do without a time
    repeat TEXT,           -- NULL, daily, weekly, monthly, yearly
    amount REAL,           -- bills: amount in rupees
    done INTEGER DEFAULT 0,
    pushed_for TEXT        -- the due time a phone notification was scheduled for
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    reminder_id INTEGER,
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    amount REAL
);
"""
REPEATS = ("daily", "weekly", "monthly", "yearly")
PUSH_MAX_AHEAD = timedelta(days=3)  # ntfy.sh keeps scheduled messages up to 3 days
_WORD = re.compile(r"[0-9A-Za-zऀ-ॿ]+")


def parse_due(value: str | None, now: datetime) -> datetime | None:
    """'2026-09-28 18:00', '2026-09-28T18:00', '2026-09-28' (9 AM) or '18:00' (the next
    18:00). Returns a naive India-time datetime; raises ValueError if unreadable."""
    value = str(value or "").strip()
    if not value:
        return None
    now = now.replace(tzinfo=None, second=0, microsecond=0)
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        hour, minute = map(int, value.split(":"))
        due = now.replace(hour=hour, minute=minute)
        return due if due > now else due + timedelta(days=1)
    value = value.replace(" ", "T", 1)
    due = datetime.fromisoformat(value[:16] if "T" in value else value)
    if "T" not in value:
        due = due.replace(hour=9)
    return due.replace(tzinfo=None, second=0, microsecond=0)


def next_due(due: datetime, repeat: str | None, now: datetime) -> datetime | None:
    """The next occurrence after `now` of a repeating reminder, else None."""
    if repeat not in REPEATS:
        return None
    now = now.replace(tzinfo=None)
    while True:
        if repeat == "daily":
            due += timedelta(days=1)
        elif repeat == "weekly":
            due += timedelta(days=7)
        elif repeat == "monthly":
            year, month = due.year + due.month // 12, due.month % 12 + 1
            due = due.replace(year=year, month=month,
                              day=min(due.day, calendar.monthrange(year, month)[1]))
        else:
            due = due.replace(year=due.year + 1,
                              day=min(due.day, calendar.monthrange(due.year + 1, due.month)[1]))
        if due > now:
            return due


def describe(row: dict, now: datetime) -> str:
    """e.g. '#3 pay electricity bill (₹1,200, monthly) — due Mon 5 Oct, 9:00 AM'."""
    extras = [f"₹{row['amount']:,.0f}" if row.get("amount") else "", row.get("repeat") or ""]
    text = f"#{row['id']} {row['text']}"
    if any(extras):
        text += f" ({', '.join(e for e in extras if e)})"
    if row.get("due"):
        due = datetime.fromisoformat(row["due"])
        text += f" — due {due:%a %d %b, %I:%M %p}".replace(" 0", " ")
        if due <= now.replace(tzinfo=None):
            text += " (OVERDUE)" if due < now.replace(tzinfo=None) - timedelta(minutes=30) else " (NOW)"
    return text


class Reminders:
    def __init__(self, memory, push: "Push | None" = None):
        self.memory = memory
        self.push = push
        # True when the Jarvis Android app is paired: it rings at the due time by itself
        # (it syncs the list below whenever it's open while Jarvis runs).
        self.phone_app = lambda: False
        self.announced: set[int] = set()  # told this session already
        with memory.lock:
            memory.conn.executescript(SCHEMA)  # saved with the memory from now on

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.memory.lock:
            cur = self.memory.conn.execute(sql, params)
            names = [c[0] for c in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self.memory.lock:
            cur = self.memory.conn.execute(sql, params)
            self.memory.conn.commit()
            self.memory.dirty = True
            return cur.lastrowid

    # --- what the tools do ---------------------------------------------------------
    def add(self, text: str, now: datetime, due: str | None = None, repeat: str | None = None,
            amount: float | None = None) -> str:
        text = " ".join(str(text or "").split())[:200]
        if not text:
            return "Not saved: the reminder has no text."
        try:
            due_at = parse_due(due, now)
        except ValueError:
            return "Not saved: give 'due' as 'YYYY-MM-DD HH:MM' (India time), or leave it empty."
        repeat = repeat if repeat in REPEATS else None
        if repeat and not due_at:
            return "Not saved: a repeating reminder needs a first due date and time."
        try:
            amount = float(amount) if amount not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        rid = self._write(
            "INSERT INTO reminders (created, text, due, repeat, amount) VALUES (?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), text,
             due_at.isoformat(timespec="minutes") if due_at else None, repeat, amount))
        row = self.get(rid)
        pushed = self.schedule_pushes(now) and row["due"]
        return (f"Saved: {describe(row, now)}. "
                + self._honesty(bool(pushed) or bool(row["due"] and self.phone_app())))

    def _honesty(self, pushed: bool) -> str:
        if pushed:
            return ("A phone notification is scheduled for the due time. Tell the user "
                    "briefly that it's saved.")
        return ("Tell the user briefly that it's saved and that you'll remind them when they "
                "next open you (you can't ring while switched off).")

    def get(self, rid: int) -> dict | None:
        rows = self._rows("SELECT * FROM reminders WHERE id = ?", (rid,))
        return rows[0] if rows else None

    def pending(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM reminders WHERE done = 0 "
                          "ORDER BY due IS NULL, due, id LIMIT ?", (limit,))

    def due_soon(self, now: datetime, within: timedelta = timedelta(hours=1)) -> list[dict]:
        limit = (now.replace(tzinfo=None) + within).isoformat(timespec="minutes")
        return self._rows("SELECT * FROM reminders WHERE done = 0 AND due IS NOT NULL "
                          "AND due <= ? ORDER BY due", (limit,))

    def find(self, which: str) -> list[dict]:
        """Reminders matching a number ('3', '#3') or words ('electricity bill')."""
        which = str(which or "").strip()
        number = re.fullmatch(r"#?(\d+)", which)
        pending = self.pending(200)
        if number:
            return [r for r in pending if r["id"] == int(number.group(1))]
        words = {w for w in _WORD.findall(which.lower()) if len(w) > 2}
        scored = [(len(words & set(_WORD.findall(r["text"].lower()))), r) for r in pending]
        best = max((s for s, _ in scored), default=0)
        return [r for s, r in scored if s == best] if best else []

    def complete(self, which: str, now: datetime, amount: float | None = None) -> str:
        """Done / paid. A repeating one moves to its next date; bills log the payment."""
        found = self.find(which)
        if not found:
            return "No matching reminder. Say it differently or ask the user which one."
        if len(found) > 1:
            return "Several match; ask the user which one: " + "; ".join(
                describe(r, now) for r in found[:5])
        row = found[0]
        paid = amount if amount not in (None, "") else row["amount"]
        if paid:
            self._write("INSERT INTO payments (reminder_id, ts, text, amount) VALUES (?, ?, ?, ?)",
                        (row["id"], now.isoformat(timespec="seconds"), row["text"], float(paid)))
        following = next_due(datetime.fromisoformat(row["due"]), row["repeat"], now) \
            if row["due"] else None
        if following:
            self._write("UPDATE reminders SET due = ?, pushed_for = NULL WHERE id = ?",
                        (following.isoformat(timespec="minutes"), row["id"]))
            self.schedule_pushes(now)
            return f"Marked done. Next time: {describe(self.get(row['id']), now)}."
        self._write("UPDATE reminders SET done = 1 WHERE id = ?", (row["id"],))
        return f"Marked done: {row['text']}."

    def delete(self, which: str, now: datetime) -> str:
        found = self.find(which)
        if len(found) != 1:
            return ("No matching reminder." if not found else "Several match; ask which one: "
                    + "; ".join(describe(r, now) for r in found[:5]))
        self._write("DELETE FROM reminders WHERE id = ?", (found[0]["id"],))
        return f"Deleted: {found[0]['text']}."

    def overview(self, now: datetime) -> str:
        """Everything pending, plus bill payments of the last 3 months (with totals)."""
        rows = self.pending()
        lines = [describe(r, now) for r in rows] or ["No reminders or to-dos."]
        since = (now - timedelta(days=92)).isoformat(timespec="seconds")
        payments = self._rows("SELECT ts, text, amount FROM payments WHERE ts >= ? "
                              "ORDER BY ts", (since,))
        if payments:
            lines.append("Payments in the last 3 months:")
            lines += [f"- {p['ts'][:10]} {p['text']}: ₹{p['amount']:,.0f}" for p in payments]
            lines.append(f"Total: ₹{sum(p['amount'] for p in payments):,.0f}")
        return "\n".join(lines)

    # --- for the prompt and the page ------------------------------------------------
    def prompt_summary(self, now: datetime, limit: int = 8) -> str | None:
        rows = self.pending(limit)
        if not rows:
            return None
        how = ("when the user opens you, and a phone notification at the due time"
               if self.push or self.phone_app()
               else "only when the user opens you (you can't ring while off)")
        return (f"The user's reminders and to-dos (you tell them {how}):\n"
                + "\n".join(f"- {describe(r, now)}" for r in rows))

    def for_phone(self, now: datetime, limit: int = 100) -> list[dict]:
        """Reminders with a due time that isn't long past, for the phone app's alarms
        (it computes later repeats itself). Due times are India time 'YYYY-MM-DDTHH:MM'."""
        since = (now.replace(tzinfo=None) - timedelta(days=1)).isoformat(timespec="minutes")
        rows = self._rows("SELECT id, text, due, repeat FROM reminders WHERE done = 0 AND "
                          "due IS NOT NULL AND (due >= ? OR repeat IS NOT NULL) "
                          "ORDER BY due LIMIT ?", (since, limit))
        return rows

    def fingerprint(self) -> str:
        """Changes whenever a pending reminder is added, changed or done: the app fetches
        the list only then (each fetch is a request on ngrok's free plan)."""
        rows = self._rows("SELECT id, text, due, repeat FROM reminders WHERE done = 0 "
                          "AND due IS NOT NULL ORDER BY id")
        blob = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def to_announce(self, now: datetime) -> list[dict]:
        """Due or overdue reminders not yet told this session (then marked as told)."""
        rows = [r for r in self.due_soon(now) if r["id"] not in self.announced]
        self.announced.update(r["id"] for r in rows)
        return rows[:3]

    def schedule_pushes(self, now: datetime) -> int:
        """Schedule phone notifications for reminders due within ntfy's 3-day window."""
        if not self.push:
            return 0
        naive = now.replace(tzinfo=None)
        count = 0
        for row in self.pending(200):
            if not row["due"] or row["pushed_for"] == row["due"]:
                continue
            due = datetime.fromisoformat(row["due"])
            if not (naive + timedelta(seconds=15) < due <= naive + PUSH_MAX_AHEAD):
                continue
            if self.push.schedule(due, row["text"], now):
                self._write("UPDATE reminders SET pushed_for = ? WHERE id = ?",
                            (row["due"], row["id"]))
                count += 1
        return count


class Push:
    """Scheduled phone notifications through ntfy (free, no account needed)."""

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.url = f"{server.rstrip('/')}/{topic.strip()}"

    def schedule(self, due: datetime, text: str, now: datetime) -> bool:
        at = int((due - now.replace(tzinfo=None)).total_seconds() + now.timestamp())
        body = f"⏰ {text}" if config.NTFY_INCLUDE_TEXT else \
            "⏰ You have a reminder now. Open Jarvis to hear it."
        try:
            resp = requests.post(self.url, data=body.encode(), timeout=10, headers={
                "At": str(at), "Title": config.ASSISTANT_NAME, "Tags": "alarm_clock",
                "Priority": "high"})
            return resp.ok
        except requests.RequestException:
            return False


def register(reminders: Reminders, clock) -> None:
    """The reminder tools. They only write to Jarvis's own memory, so no spoken "yes"
    is needed. clock() gives the current India time."""
    tools.register(tools.Tool(
        name="add_reminder",
        description=("Save a reminder, to-do or bill. Use for 'remind me to...', 'add ... to "
                     "my to-do list', 'I have to pay ... on ...'."),
        parameters={"type": "object", "properties": {
            "text": {"type": "string", "description": "what to do, e.g. 'call mom'"},
            "due": {"type": "string", "description": (
                "India time as 'YYYY-MM-DD HH:MM'; work out 'at 6', 'tomorrow' or 'on the "
                "5th' from the current date and time. Empty for a to-do without a time.")},
            "repeat": {"type": "string", "enum": ["", *REPEATS]},
            "amount": {"type": "number", "description": "for bills: amount in rupees"}},
            "required": ["text"]},
        func=lambda text, due=None, repeat=None, amount=None:
            reminders.add(text, clock(), due, repeat, amount),
        requires_confirmation=False))
    tools.register(tools.Tool(
        name="list_reminders",
        description="List the user's reminders, to-dos and bills, and recent bill payments.",
        parameters={"type": "object", "properties": {}},
        func=lambda: reminders.overview(clock()),
        requires_confirmation=False))
    tools.register(tools.Tool(
        name="complete_reminder",
        description=("Mark a reminder, to-do or bill as done or paid (a repeating one moves "
                     "to its next date)."),
        parameters={"type": "object", "properties": {
            "which": {"type": "string", "description": "its number or a few words of it"},
            "amount": {"type": "number", "description": "amount paid, if different"}},
            "required": ["which"]},
        func=lambda which, amount=None: reminders.complete(which, clock(), amount),
        requires_confirmation=False))
    tools.register(tools.Tool(
        name="delete_reminder",
        description="Delete a reminder or to-do the user no longer wants.",
        parameters={"type": "object", "properties": {
            "which": {"type": "string", "description": "its number or a few words of it"}},
            "required": ["which"]},
        func=lambda which: reminders.delete(which, clock()),
        requires_confirmation=False))
