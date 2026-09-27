"""Long-term memory: conversation turns, remembered facts and daily usage.

The database lives only in RAM (SQLite ":memory:"). It is turned into bytes with
to_bytes(), encrypted, and uploaded by persistence.py. Plain text never touches disk.
"""
import sqlite3
import threading
from datetime import datetime

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply TEXT NOT NULL,
    lang TEXT,
    emb BLOB,
    good INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    emb BLOB
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    seconds REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS net_usage (
    month TEXT PRIMARY KEY,
    requests INTEGER NOT NULL,
    bytes INTEGER NOT NULL
);
"""


class E5Embedder:
    """multilingual-e5 needs "query: " / "passage: " prefixes."""

    def __init__(self, model_path: str, device: str = "cuda"):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_path, device=device)

    def _encode(self, text: str) -> np.ndarray:
        return self.model.encode(text, normalize_embeddings=True).astype(np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode("query: " + text)

    def embed_passage(self, text: str) -> np.ndarray:
        return self._encode("passage: " + text)


def _to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class Memory:
    def __init__(self, embedder, data: bytes | None = None):
        self.embedder = embedder
        self.lock = threading.Lock()
        self.dirty = False
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        if data:
            self.conn.deserialize(data)
        # Overwrite deleted rows with zeros, so "forget" leaves no trace in the file.
        self.conn.execute("PRAGMA secure_delete = ON")
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(turns)")}
        if "model" not in columns:  # added in Phase 5: which LLM gave the reply
            self.conn.execute("ALTER TABLE turns ADD COLUMN model TEXT")

    # --- save / load ---------------------------------------------------------
    def to_bytes(self) -> bytes:
        with self.lock:
            return self.conn.serialize()

    # --- turns ----------------------------------------------------------------
    def set_embedder(self, embedder) -> None:
        """The embedding model loads later than the database (after the limit check)."""
        self.embedder = embedder

    def add_turn(self, user_text: str, reply: str, lang: str, now: datetime,
                 model: str | None = None) -> int:
        """Save one exchange. Returns its id (used for 👍/👎 ratings)."""
        emb = self.embedder.embed_passage(f"{user_text}\n{reply}")
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO turns (ts, user_text, reply, lang, emb, model) VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), user_text, reply, lang, _to_blob(emb), model),
            )
            self.conn.commit()
            self.dirty = True
            return cur.lastrowid

    # --- ratings and fine-tuning data (Phase 5) ----------------------------------
    def rate_turn(self, turn_id: int, value: int) -> bool:
        """value: 1 = 👍 good, -1 = 👎 bad, 0 = clear."""
        with self.lock:
            cur = self.conn.execute("UPDATE turns SET good = ? WHERE id = ?",
                                    (max(-1, min(1, int(value))), turn_id))
            self.conn.commit()
            self.dirty = True
            return cur.rowcount == 1

    def good_turns(self) -> list[tuple[str, str, str, str]]:
        """(ts, lang, user_text, reply) of every 👍 turn, oldest first."""
        with self.lock:
            return self.conn.execute(
                "SELECT ts, lang, user_text, reply FROM turns WHERE good = 1 ORDER BY id"
            ).fetchall()

    def rating_stats(self) -> dict[str, tuple[int, int]]:
        """{model: (👍 count, 👎 count)} for replies that were rated."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT model, SUM(good = 1), SUM(good = -1) FROM turns "
                "WHERE good != 0 AND model IS NOT NULL GROUP BY model"
            ).fetchall()
        return {model: (up, down) for model, up, down in rows}

    def recent_turns(self, n: int) -> list[tuple[str, str]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT user_text, reply FROM turns ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return list(reversed(rows))

    def turn_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]

    # --- facts ----------------------------------------------------------------
    def add_fact(self, text: str, now: datetime) -> None:
        emb = self.embedder.embed_passage(text)
        with self.lock:
            self.conn.execute(
                "INSERT INTO facts (ts, text, emb) VALUES (?, ?, ?)",
                (now.isoformat(timespec="seconds"), text, _to_blob(emb)),
            )
            self.conn.commit()
            self.dirty = True

    def all_facts(self, limit: int) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT text FROM facts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def forget(self, query: str, min_similarity: float) -> tuple[str | None, int]:
        """Delete the fact that best matches `query`, plus past turns that closely
        match it. Returns (deleted fact text or None, number of turns deleted)."""
        q = self.embedder.embed_query(query)
        with self.lock:
            fact_text = None
            facts = self.conn.execute("SELECT id, text, emb FROM facts").fetchall()
            if facts:
                scores = [float(np.dot(q, _from_blob(r[2]))) for r in facts]
                best = int(np.argmax(scores))
                if scores[best] >= min_similarity:
                    fact_text = facts[best][1]
                    self.conn.execute("DELETE FROM facts WHERE id = ?", (facts[best][0],))
            turns = self.conn.execute("SELECT id, emb FROM turns").fetchall()
            turn_ids = [(tid,) for tid, emb in turns
                        if emb and float(np.dot(q, _from_blob(emb))) >= min_similarity]
            self.conn.executemany("DELETE FROM turns WHERE id = ?", turn_ids)
            self.conn.commit()
            if fact_text or turn_ids:
                self.dirty = True
            return fact_text, len(turn_ids)

    # --- search -----------------------------------------------------------------
    def search(self, query: str, k: int, min_similarity: float,
               exclude_recent_turns: int = 0) -> list[str]:
        """Top-k facts and past turns most related to `query`."""
        q = self.embedder.embed_query(query)
        with self.lock:
            facts = self.conn.execute("SELECT text, emb FROM facts").fetchall()
            turns = self.conn.execute(
                "SELECT ts, user_text, reply, emb FROM turns ORDER BY id DESC LIMIT -1 OFFSET ?",
                (exclude_recent_turns,),
            ).fetchall()
        candidates = [(f"Fact: {t}", e) for t, e in facts]
        candidates += [(f"On {ts[:10]} user said: {u} | you replied: {r}", e)
                       for ts, u, r, e in turns]
        scored = [(float(np.dot(q, _from_blob(e))), text) for text, e in candidates if e]
        scored.sort(reverse=True)
        return [text for score, text in scored[:k] if score >= min_similarity]

    # --- daily usage ---------------------------------------------------------
    def get_usage(self, day: str) -> float:
        with self.lock:
            row = self.conn.execute("SELECT seconds FROM usage WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0.0

    def get_net(self, month: str) -> tuple[int, int]:
        """(requests, bytes sent) through ngrok this month."""
        with self.lock:
            row = self.conn.execute(
                "SELECT requests, bytes FROM net_usage WHERE month = ?", (month,)).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def add_net(self, month: str, requests: int, nbytes: int) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO net_usage (month, requests, bytes) VALUES (?, ?, ?) "
                "ON CONFLICT(month) DO UPDATE SET requests = requests + excluded.requests, "
                "bytes = bytes + excluded.bytes",
                (month, requests, nbytes),
            )
            self.conn.commit()
            self.dirty = True

    def get_usage_since(self, first_day: str) -> float:
        """Total seconds used from `first_day` (YYYY-MM-DD) up to today."""
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM usage WHERE day >= ?", (first_day,)
            ).fetchone()
        return row[0]

    def add_usage(self, day: str, seconds: float) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO usage (day, seconds) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET seconds = seconds + excluded.seconds",
                (day, seconds),
            )
            self.conn.commit()
            self.dirty = True
