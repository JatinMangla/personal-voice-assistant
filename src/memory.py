"""Long-term memory: conversation turns, remembered facts and daily usage.

The database lives only in RAM (SQLite ":memory:"). It is turned into bytes with
to_bytes(), encrypted, and uploaded by persistence.py. Plain text never touches disk.

Finding related memories is a hybrid search (see docs/MEMORY_RESEARCH.md):
- meaning: multilingual vectors ("allergy" also finds "एलर्जी");
- keywords: an FTS5 (BM25) index, for names and numbers that vectors blur;
- both rankings merged by Reciprocal Rank Fusion, plus a small boost for recent turns;
- date and category hints ("last week", "my decisions") filter before ranking.
The keyword index lives in TEMP tables: rebuilt at every start (fast), never saved, so
it adds nothing to the uploaded file and leaves no trace of forgotten text in it.
"""
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

import config
import query_hints

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

# Hindi vowel signs are "marks" (M*), not letters: without M* the index would cut
# Devanagari words into pieces.
FTS_TOKENIZER = "unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
KEYWORD_INDEX = f"""
CREATE VIRTUAL TABLE temp.facts_fts USING fts5(text, tokenize="{FTS_TOKENIZER}");
CREATE VIRTUAL TABLE temp.turns_fts USING fts5(text, tokenize="{FTS_TOKENIZER}");
INSERT INTO temp.facts_fts(rowid, text) SELECT id, text FROM main.facts;
INSERT INTO temp.turns_fts(rowid, text) SELECT id, user_text || ' ' || reply FROM main.turns;
CREATE TEMP TRIGGER facts_fts_add AFTER INSERT ON main.facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TEMP TRIGGER facts_fts_del AFTER DELETE ON main.facts BEGIN
    DELETE FROM facts_fts WHERE rowid = old.id; END;
CREATE TEMP TRIGGER turns_fts_add AFTER INSERT ON main.turns BEGIN
    INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.user_text || ' ' || new.reply); END;
CREATE TEMP TRIGGER turns_fts_del AFTER DELETE ON main.turns BEGIN
    DELETE FROM turns_fts WHERE rowid = old.id; END;
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


def rrf(*rankings: list, k: int = 60) -> dict:
    """Reciprocal Rank Fusion: {item: score}. Items ranked high in any list win; items
    found by both searches win most. Scores don't need to be comparable."""
    scores: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


@dataclass
class _TurnVectors:
    """All turn embeddings as one matrix (built once, then appended to), so searching
    thousands of turns is one matrix product instead of a Python loop."""
    ids: np.ndarray
    days: np.ndarray  # "YYYY-MM-DD" per row, for date filters
    matrix: np.ndarray


SEARCH_CANDIDATES = 30   # from each search, before fusing
RECENCY_BOOST = 0.004     # about a quarter of a first place in RRF, for a turn from today
RECENCY_HALF_LIFE_DAYS = 30


def _plain_hints(query: str) -> query_hints.Hints:
    """Keywords only (no date or category rules), for callers without a clock."""
    return query_hints.Hints(terms=query_hints.keyword_terms(query))


def _in_range(ts: str | None, date_range: tuple[date, date] | None) -> bool:
    if not date_range or not ts:
        return False
    return date_range[0].isoformat() <= ts[:10] < date_range[1].isoformat()


def _fts(term: str) -> str:
    """A safe FTS5 term: quoted (so words like OR/NEAR stay plain words), * = prefix."""
    word = term.rstrip("*").replace('"', "")
    return f'"{word}"*' if term.endswith("*") else f'"{word}"'


def _short(text: str, limit: int = 300) -> str:
    """Old conversations in the prompt stay short: a long answer is cut."""
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


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
        self._add_column("turns", "model", "TEXT")      # Phase 5: which LLM replied
        self._add_column("turns", "learned", "INTEGER DEFAULT 0")  # facts extracted yet?
        self._add_column("turns", "reflected", "INTEGER DEFAULT 0")  # patterns looked for?
        self._add_column("facts", "category", "TEXT")   # health, diet, person, ...
        self._add_column("facts", "source", "TEXT")     # "user" (said "remember") or "auto"
        self.keyword_index = self._build_keyword_index()
        self._vectors: _TurnVectors | None = None  # built on first search
        self._last_query: tuple[str, np.ndarray] | None = None

    def _build_keyword_index(self) -> bool:
        try:
            self.conn.executescript(KEYWORD_INDEX)
            return True
        except sqlite3.OperationalError as e:  # SQLite without FTS5: vectors only
            print(f"[memory] keyword search unavailable ({e}); using meaning search only")
            return False

    def _add_column(self, table: str, column: str, kind: str) -> None:
        """Upgrade databases saved by older versions."""
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

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
            if self._vectors is not None:
                v = self._vectors
                self._vectors = _TurnVectors(
                    np.append(v.ids, cur.lastrowid), np.append(v.days, now.date().isoformat()),
                    np.vstack([v.matrix, emb[None, :]]) if len(v.ids) else emb[None, :])
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
    def add_fact(self, text: str, now: datetime, category: str | None = None,
                 source: str = "user") -> None:
        emb = self.embedder.embed_passage(text)
        with self.lock:
            self.conn.execute(
                "INSERT INTO facts (ts, text, emb, category, source) VALUES (?, ?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), text, _to_blob(emb), category, source),
            )
            self.conn.commit()
            self.dirty = True

    def all_facts(self, limit: int) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT text FROM facts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def facts_for_prompt(self, query: str, limit: int, always_categories: tuple[str, ...],
                         hints: query_hints.Hints | None = None,
                         preferred_categories: tuple[str, ...] = ()) -> list[str]:
        """Facts to show the LLM for this question. Always: facts you asked it to
        remember and health/diet facts (e.g. allergies), so it can warn you even when
        you don't mention them. Then, up to `limit` in total: facts of an asked-about
        category ("my decisions"), then preferred categories (preferences, patterns),
        then the rest, each group by hybrid rank."""
        hints = hints or _plain_hints(query)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, text, category, source, ts, emb FROM facts ORDER BY id").fetchall()
        shown_always = lambda cat, src: src != "auto" or cat in always_categories  # noqa: E731
        always = [t for _, t, cat, src, _, _ in rows if shown_always(cat, src)]
        others = [(fid, t, cat, ts, e) for fid, t, cat, src, ts, e in rows
                  if not shown_always(cat, src)]
        room = max(0, limit - len(always))
        if not others or not room:
            return always
        q = self._embed_query(query)
        by_meaning = [r[0] for r in sorted(
            others, key=lambda r: -float(np.dot(q, _from_blob(r[4]))) if r[4] else 1.0)]
        allowed = {r[0] for r in others}
        by_words = [fid for fid in self._keyword_ids("facts_fts", hints.terms, 50) if fid in allowed]
        in_range = [r[0] for r in reversed(others) if _in_range(r[3], hints.date_range)]
        score = rrf(by_meaning, by_words, in_range)
        others.sort(key=lambda r: (r[2] in hints.categories, r[2] in preferred_categories,
                                   score.get(r[0], 0.0)), reverse=True)
        return always + [r[1] for r in others[:room]]

    def similar_facts(self, text: str, k: int) -> list[tuple[int, str, float]]:
        """(id, text, similarity) of the k facts most like `text`."""
        q = self._embed_query(text)
        with self.lock:
            rows = self.conn.execute("SELECT id, text, emb FROM facts").fetchall()
        scored = [(fid, t, float(np.dot(q, _from_blob(e)))) for fid, t, e in rows]
        return sorted(scored, key=lambda r: r[2], reverse=True)[:k]

    def delete_fact(self, fact_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self.conn.commit()
            self.dirty = True
            return cur.rowcount == 1

    def list_facts(self) -> list[dict]:
        """Everything Jarvis knows about the user, for the 🧠 Memory panel."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, text, category, source, ts FROM facts ORDER BY id").fetchall()
        return [{"id": i, "text": t, "category": c or ("remembered" if s != "auto" else "other"),
                 "date": (ts or "")[:10]} for i, t, c, s, ts in rows]

    # --- learning from old conversations (back-learning) ----------------------------
    def unlearned_turns(self, limit: int) -> list[tuple[int, str, str]]:
        """(id, ts, user_text) of past turns not yet read for facts, oldest first."""
        with self.lock:
            return self.conn.execute(
                "SELECT id, ts, user_text FROM turns WHERE COALESCE(learned, 0) = 0 "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()

    def unreflected_turns(self, limit: int) -> list[tuple[int, str]]:
        """(id, user_text) of the most recent turns not yet reviewed for patterns."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, user_text FROM turns WHERE COALESCE(reflected, 0) = 0 "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return list(reversed(rows))

    def mark_reflected(self, turn_ids: list[int]) -> None:
        with self.lock:
            self.conn.executemany("UPDATE turns SET reflected = 1 WHERE id = ?",
                                  [(i,) for i in turn_ids])
            self.conn.commit()
            self.dirty = True

    def mark_learned(self, turn_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE turns SET learned = 1 WHERE id = ?", (turn_id,))
            self.conn.commit()
            self.dirty = True

    def forget(self, query: str, min_similarity: float) -> tuple[str | None, int]:
        """Delete the fact that best matches `query` (and its near-copies, e.g. the same
        fact said once and also learned automatically), plus past turns that closely
        match it. Returns (deleted fact text or None, number of turns deleted)."""
        q = self._embed_query(query)
        with self.lock:
            fact_text = None
            facts = [(fid, t, _from_blob(e)) for fid, t, e in
                     self.conn.execute("SELECT id, text, emb FROM facts WHERE emb IS NOT NULL")]
            if facts:
                scores = [float(np.dot(q, e)) for _, _, e in facts]
                best = int(np.argmax(scores))
                if scores[best] >= min_similarity:
                    fact_text, best_emb = facts[best][1], facts[best][2]
                    doomed = [(fid,) for fid, _, e in facts
                              if float(np.dot(best_emb, e)) >= config.LEARN_DUPLICATE_SIMILARITY]
                    self.conn.executemany("DELETE FROM facts WHERE id = ?", doomed)
            vec = self._turn_vectors()
            hits = vec.ids[vec.matrix @ q >= min_similarity] if len(vec.ids) else []
            turn_ids = [(int(tid),) for tid in hits]
            self.conn.executemany("DELETE FROM turns WHERE id = ?", turn_ids)
            self.conn.commit()
            if fact_text or turn_ids:
                self.dirty = True
                self._vectors = None
                self._compact_keyword_index()
            return fact_text, len(turn_ids)

    def _compact_keyword_index(self) -> None:
        """FTS5 marks deleted rows instead of erasing them; merging drops the words of
        forgotten text from RAM right away (the index is never saved anyway)."""
        if self.keyword_index:
            for table in ("facts_fts", "turns_fts"):
                self.conn.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")

    # --- search -----------------------------------------------------------------
    def _embed_query(self, text: str) -> np.ndarray:
        """One embedding per question, although several searches use it."""
        cached = self._last_query
        if cached and cached[0] == text:
            return cached[1]
        vec = self.embedder.embed_query(text)
        self._last_query = (text, vec)
        return vec

    def _turn_vectors(self) -> _TurnVectors:
        """Call with self.lock held."""
        if self._vectors is None:
            rows = self.conn.execute(
                "SELECT id, ts, emb FROM turns WHERE emb IS NOT NULL ORDER BY id").fetchall()
            self._vectors = _TurnVectors(
                np.array([r[0] for r in rows], dtype=np.int64),
                np.array([(r[1] or "")[:10] for r in rows], dtype="<U10"),
                np.vstack([_from_blob(r[2]) for r in rows]) if rows
                else np.zeros((0, 0), dtype=np.float32))
        return self._vectors

    def _keyword_ids(self, table: str, terms: list[str], limit: int,
                     where: str = "", params: tuple = ()) -> list[int]:
        """Row ids ranked by BM25 for the distinctive words among `terms`."""
        if not self.keyword_index or not terms:
            return []
        total = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        useful = []
        for term in terms:  # a word found in most rows can't tell rows apart
            n = self.conn.execute(f"SELECT count(*) FROM {table} WHERE {table} MATCH ?",
                                  (_fts(term),)).fetchone()[0]
            if n and n <= max(20, total // 10):
                useful.append(term)
        if not useful:
            return []
        match = " OR ".join(_fts(t) for t in useful)
        if table == "turns_fts":
            sql = ("SELECT f.rowid FROM turns_fts f JOIN main.turns t ON t.id = f.rowid "
                   f"WHERE turns_fts MATCH ? {where} ORDER BY f.rank LIMIT ?")
        else:
            sql = f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?"
        return [r[0] for r in self.conn.execute(sql, (match, *params, limit))]

    def search(self, query: str, k: int, min_similarity: float,
               exclude_recent_turns: int = 0, hints: query_hints.Hints | None = None,
               include_facts: bool = True) -> list[str]:
        """Top-k past turns (and facts) related to `query`: meaning and keyword searches
        merged by RRF, plus a small boost for recent turns.
        exclude_recent_turns: skip the newest turns (they are the chat history already).
        With a date hint, only turns from that time count; when the user asks about that
        time ("what did we talk about yesterday?"), the date alone is enough."""
        hints = hints or _plain_hints(query)
        q = self._embed_query(query)
        date_only = bool(hints.date_range and hints.recall)
        with self.lock:
            newest = self.conn.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1 OFFSET ?",
                                       (exclude_recent_turns,)).fetchone()
            max_id = newest[0] if newest else 0
            vec = self._turn_vectors()
            mask = vec.ids <= max_id
            where, params = "AND t.id <= ?", (max_id,)
            if hints.date_range:
                start, end = (d.isoformat() for d in hints.date_range)
                mask &= (vec.days >= start) & (vec.days < end)
                where += " AND substr(t.ts, 1, 10) >= ? AND substr(t.ts, 1, 10) < ?"
                params += (start, end)
            ids, days = vec.ids[mask], vec.days[mask]
            scores = vec.matrix[mask] @ q if len(ids) else np.zeros(0)
            order = np.argsort(-scores, kind="stable")[:SEARCH_CANDIDATES]
            by_meaning = [("t", int(ids[i])) for i in order
                          if date_only or scores[i] >= min_similarity]
            by_words = [("t", i) for i in self._keyword_ids(
                "turns_fts", hints.terms, SEARCH_CANDIDATES, where, params)]
            rankings = [by_meaning, by_words]
            if include_facts:
                facts = self.conn.execute(
                    "SELECT id, emb FROM facts WHERE emb IS NOT NULL").fetchall()
                fact_scores = sorted(((float(np.dot(q, _from_blob(e))), fid) for fid, e in facts),
                                     reverse=True)
                rankings.append([("f", fid) for score, fid in fact_scores[:SEARCH_CANDIDATES]
                                 if score >= min_similarity])
                rankings.append([("f", fid) for fid in
                                 self._keyword_ids("facts_fts", hints.terms, SEARCH_CANDIDATES)])
            fused = rrf(*rankings)
            day_of = dict(zip(ids.tolist(), days.tolist()))
            today = date.today()
            for item in fused:  # a small boost for recent conversations
                if item[0] == "t" and (day := day_of.get(item[1])):
                    age = max(0, (today - date.fromisoformat(day)).days)
                    fused[item] += RECENCY_BOOST * 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
            chosen = sorted(fused, key=fused.get, reverse=True)[:k]
            if date_only:
                chosen.sort()  # that time's conversations in the order they happened
            out = []
            for kind, row_id in chosen:
                if kind == "f":
                    row = self.conn.execute("SELECT text FROM facts WHERE id = ?",
                                            (row_id,)).fetchone()
                    out.append(f"Fact: {row[0]}")
                else:
                    ts, user, reply = self.conn.execute(
                        "SELECT ts, user_text, reply FROM turns WHERE id = ?", (row_id,)).fetchone()
                    out.append(f"On {ts[:10]} user said: {_short(user)} | "
                               f"you replied: {_short(reply)}")
        return out

    def recall(self, query: str, now: datetime,
               exclude_recent_turns: int = 0) -> tuple[list[str], list[str]]:
        """What the LLM gets for this question: (facts, related past conversations).
        Facts appear once (in the facts list), never again among the related."""
        hints = query_hints.parse(query, now)
        facts = self.facts_for_prompt(query, config.ALL_FACTS_LIMIT,
                                      config.ALWAYS_SHOWN_CATEGORIES, hints,
                                      config.PREFERRED_CATEGORIES)
        related = self.search(query, config.MEMORY_TOP_K, config.MEMORY_MIN_SIMILARITY,
                              exclude_recent_turns, hints, include_facts=False)
        return facts, related

    def stats(self) -> dict:
        """Row counts for the log (never content): shows when to split storage."""
        with self.lock:
            return {"turns": self.conn.execute("SELECT count(*) FROM turns").fetchone()[0],
                    "facts": self.conn.execute("SELECT count(*) FROM facts").fetchone()[0]}

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

    def usage_days(self, first_day: str) -> dict[str, float]:
        """{day: seconds} from `first_day` (YYYY-MM-DD) on."""
        with self.lock:
            return dict(self.conn.execute(
                "SELECT day, seconds FROM usage WHERE day >= ?", (first_day,)).fetchall())

    def add_usage(self, day: str, seconds: float) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO usage (day, seconds) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET seconds = seconds + excluded.seconds",
                (day, seconds),
            )
            self.conn.commit()
            self.dirty = True
