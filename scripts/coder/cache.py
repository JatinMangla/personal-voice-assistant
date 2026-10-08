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
"""


def _key(task: str, question: str) -> str:
    return task + "|" + re.sub(r"\s+", " ", question.strip().lower())


class Cache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    def get(self, task: str, question: str) -> dict | None:
        """{'id', 'answer'} if the same question was answered and none of its files changed."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM answers WHERE key=? ORDER BY id DESC LIMIT 1",
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

    def stats(self) -> dict:
        """Per task: how many answers, how many rated good / bad."""
        with self.lock:
            out: dict = {}
            for r in self.conn.execute("SELECT task, COUNT(*) n FROM answers GROUP BY task"):
                out[r["task"]] = {"answers": r["n"], "good": 0, "bad": 0}
            for r in self.conn.execute("SELECT task, verdict, COUNT(*) n FROM ratings GROUP BY task, verdict"):
                out.setdefault(r["task"], {"answers": 0, "good": 0, "bad": 0})[r["verdict"]] = r["n"]
        return out
