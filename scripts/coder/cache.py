"""Answer cache + ratings. An answer is reused only while every file it was based on still
has the same content hash, so the cache can never return an outdated answer."""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import safety

SCHEMA = """
CREATE TABLE IF NOT EXISTS answers(
    id INTEGER PRIMARY KEY, key TEXT, task TEXT, question TEXT, answer TEXT, files TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS answers_key ON answers(key);
CREATE TABLE IF NOT EXISTS ratings(answer_id INTEGER PRIMARY KEY, task TEXT, verdict TEXT, ts REAL);
-- Experiment E: what a rated-bad answer taught us. Shown as a warning when a similar question
-- comes back, so the same mistake is not made twice (learning without training).
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY, answer_id INTEGER, task TEXT, question TEXT, correction TEXT, ts REAL);
"""
MAX_NOTES = 3                # at most this many warnings go into one prompt
NOTE_WORDS_IGNORED = {"the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "and", "or",
                      "how", "what", "where", "which", "does", "do", "it", "this", "that", "with",
                      "from", "by", "be", "why", "when", "who"}
NOTE_MIN_OVERLAP = 0.34      # share of the new question's words a note must share to be shown


def _key(task: str, question: str) -> str:
    return task + "|" + re.sub(r"\s+", " ", question.strip().lower())


def _words(question: str) -> set:
    """The meaningful words of a question, for matching one question against another."""
    return {w for w in re.findall(r"[a-z0-9_]{2,}", question.lower()) if w not in NOTE_WORDS_IGNORED}


class Cache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    def get(self, task: str, question: str) -> dict | None:
        """{'id', 'answer'} if the same question was answered, none of its files changed, and the
        answer was not rated bad (a bad answer must never come back from the cache)."""
        with self.lock:
            row = self.conn.execute(
                "SELECT a.* FROM answers a LEFT JOIN ratings r ON r.answer_id = a.id "
                "WHERE a.key = ? AND COALESCE(r.verdict, '') != 'bad' ORDER BY a.id DESC LIMIT 1",
                (_key(task, question),)).fetchone()
        if not row:
            return None
        for path, digest in json.loads(row["files"]).items():
            if safety.file_hash(Path(path)) != digest:
                return None
        return {"id": row["id"], "answer": row["answer"]}

    def put(self, task: str, question: str, answer: str, files: list[Path]) -> int:
        hashes = {str(Path(f).resolve()): safety.file_hash(Path(f)) for f in files}
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO answers(key, task, question, answer, files, ts) VALUES (?,?,?,?,?,?)",
                (_key(task, question), task, question, answer, json.dumps(hashes), time.time()))
            self.conn.commit()
            return cur.lastrowid

    def rate(self, answer_id: int, verdict: str) -> bool:
        if verdict not in ("good", "bad"):
            return False
        with self.lock:
            row = self.conn.execute("SELECT task FROM answers WHERE id=?", (answer_id,)).fetchone()
            if not row:
                return False
            self.conn.execute("INSERT OR REPLACE INTO ratings(answer_id, task, verdict, ts) VALUES (?,?,?,?)",
                              (answer_id, row["task"], verdict, time.time()))
            self.conn.commit()
            return True

    # --- notes from answers that were wrong (experiment E) --------------------------------
    def add_note(self, answer_id: int, correction: str = "") -> bool:
        """Remember that this answer was wrong, with an optional correction. Called when an answer
        is rated bad, so a later similar question can be warned."""
        with self.lock:
            row = self.conn.execute("SELECT task, question FROM answers WHERE id=?",
                                    (answer_id,)).fetchone()
            if not row:
                return False
            self.conn.execute(
                "INSERT INTO notes(answer_id, task, question, correction, ts) VALUES (?,?,?,?,?)",
                (answer_id, row["task"], row["question"], correction.strip(), time.time()))
            self.conn.commit()
            return True

    def notes_for(self, task: str, question: str, limit: int = MAX_NOTES) -> list[dict]:
        """Past mistakes on questions close to this one, newest first. Closeness is plain word
        overlap: it needs no model, so it also works while the Coder is offline."""
        wanted = _words(question)
        if not wanted:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT question, correction FROM notes WHERE task=? ORDER BY id DESC",
                (task,)).fetchall()
        out = []
        for r in rows:
            words = _words(r["question"])
            if words and len(wanted & words) / len(wanted) >= NOTE_MIN_OVERLAP:
                out.append({"question": r["question"], "correction": r["correction"]})
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict:
        """Per task: how many answers, how many rated good / bad."""
        with self.lock:
            out: dict = {}
            for r in self.conn.execute("SELECT task, COUNT(*) n FROM answers GROUP BY task"):
                out[r["task"]] = {"answers": r["n"], "good": 0, "bad": 0}
            for r in self.conn.execute("SELECT task, verdict, COUNT(*) n FROM ratings GROUP BY task, verdict"):
                out.setdefault(r["task"], {"answers": 0, "good": 0, "bad": 0})[r["verdict"]] = r["n"]
        return out
