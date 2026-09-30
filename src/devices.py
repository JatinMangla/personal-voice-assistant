"""Paired phones (the Jarvis Android app): one random token per device.

Why: ngrok's Google sign-in works in a browser only, so the app can't use it. Instead:
- The page (behind Google sign-in) asks for a pairing: the server makes a random 256-bit
  token, keeps ONLY its SHA-256 (+ name and dates) in the encrypted memory, and shows the
  token once as a QR code. Unused, it expires after PAIR_TTL_SEC.
- The app sends it on every request (`Authorization: Bearer ...` with `X-Jarvis-App: 1`);
  that header is the only thing that lets a request past ngrok without Google sign-in
  (tunnel.traffic_policy), and then this check is required instead.
- Each device can be revoked on the page. Wrong tokens are counted: after MAX_FAILS in
  LOCK_SEC, wrong tokens get "too many attempts" (a valid token always works).
Logs show counts only, never tokens or names.
"""
import hashlib
import hmac
import secrets
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,     -- ISO time, India
    created_at REAL NOT NULL,  -- epoch seconds (for the pairing expiry)
    paired INTEGER DEFAULT 0,  -- 1 after the app used the token once
    last_used TEXT
);
"""

PAIR_TTL_SEC = 5 * 60
MAX_FAILS = 5
LOCK_SEC = 15 * 60
MAX_DEVICES = 10
LAST_USED_EVERY_SEC = 10 * 60  # don't mark the memory as changed on every request


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())[:40]
    return name or "Phone"


class Devices:
    def __init__(self, memory, clock=time.time):
        self.memory = memory
        self.clock = clock
        self.fails: list[float] = []
        self.locked_until = 0.0
        self._touched: dict[int, float] = {}
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
            return cur.rowcount

    def _drop_expired(self) -> None:
        with self.memory.lock:
            cur = self.memory.conn.execute(
                "DELETE FROM app_devices WHERE paired = 0 AND created_at < ?",
                (self.clock() - PAIR_TTL_SEC,))
            if cur.rowcount:
                self.memory.conn.commit()
                self.memory.dirty = True

    # --- the page (Google sign-in) ------------------------------------------------------
    def mint(self, name: str, now: datetime) -> str | None:
        """A new pairing token, shown once. None when MAX_DEVICES are paired already."""
        self._drop_expired()
        if len(self.list()) >= MAX_DEVICES:
            return None
        token = secrets.token_urlsafe(32)  # 256 bits
        self._write("INSERT INTO app_devices (name, token_hash, created, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (clean_name(name), token_hash(token), now.isoformat(timespec="seconds"),
                     self.clock()))
        print("[devices] pairing code made (valid 5 min)")
        return token

    def list(self) -> list[dict]:
        self._drop_expired()
        rows = self._rows("SELECT id, name, created, paired, last_used FROM app_devices "
                          "ORDER BY id")
        return [{**r, "paired": bool(r["paired"])} for r in rows]

    def rotate(self, device_id: int) -> str:
        """A new token for a paired device, replacing its old one. The app does this right
        after scanning, so the token that was on the screen (in the QR code) stops working:
        a photo of the QR code is useless afterwards."""
        token = secrets.token_urlsafe(32)
        self._write("UPDATE app_devices SET token_hash = ?, paired = 1 WHERE id = ?",
                    (token_hash(token), int(device_id)))
        return token

    def revoke(self, device_id: int) -> bool:
        ok = self._write("DELETE FROM app_devices WHERE id = ?", (int(device_id),)) > 0
        if ok:
            self._touched.pop(int(device_id), None)
            print("[devices] a device was revoked")
        return ok

    # --- the app --------------------------------------------------------------------
    def check(self, authorization: str | None, now: datetime) -> tuple[dict | None, bool]:
        """(device, locked). device is None for a missing/wrong/expired token; locked is
        True when wrong tokens are refused for a while (too many attempts)."""
        token = (authorization or "").strip()
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        device = self._find(token) if token else None
        t = self.clock()
        if device is not None:
            self._use(device, now)
            return device, False
        if t < self.locked_until:
            return None, True
        self.fails = [f for f in self.fails if t - f < LOCK_SEC] + [t]
        if len(self.fails) >= MAX_FAILS:
            self.fails = []
            self.locked_until = t + LOCK_SEC
            print(f"[devices] {MAX_FAILS} wrong app tokens: refusing wrong tokens for 15 min")
        return None, False

    def _find(self, token: str) -> dict | None:
        wanted = token_hash(token)
        found = None
        rows = self._rows("SELECT id, name, token_hash, paired, created_at FROM app_devices")
        for row in rows:  # compare with every stored hash (constant time each)
            if hmac.compare_digest(row["token_hash"], wanted):
                found = row
        if found and not found["paired"] and self.clock() - found["created_at"] > PAIR_TTL_SEC:
            return None  # an unused pairing code that expired
        return found

    def _use(self, device: dict, now: datetime) -> None:
        t = self.clock()
        if not device["paired"]:
            self._write("UPDATE app_devices SET paired = 1, last_used = ? WHERE id = ?",
                        (now.isoformat(timespec="seconds"), device["id"]))
            self._touched[device["id"]] = t
            print("[devices] a phone finished pairing")
        elif t - self._touched.get(device["id"], 0) >= LAST_USED_EVERY_SEC:
            self._write("UPDATE app_devices SET last_used = ? WHERE id = ?",
                        (now.isoformat(timespec="seconds"), device["id"]))
            self._touched[device["id"]] = t
