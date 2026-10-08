"""The code index: files -> chunks -> SQLite (keywords via FTS5). Re-reading is cheap: a file
is only read again when its size or modification time changed, and only re-chunked when its
content hash changed."""
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import chunk, safety

MAX_FILE_BYTES = 400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, hash TEXT, size INTEGER, mtime REAL, lines INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY, path TEXT, first INTEGER, last INTEGER, kind TEXT, name TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, words, name, path, tokenize="unicode61 tokenchars '_'");
CREATE TABLE IF NOT EXISTS vectors(chunk_id INTEGER PRIMARY KEY, dim INTEGER, vec BLOB);
"""
VECTOR_TEXT_CHARS = 1500    # how much of a chunk the embedding model reads
MIN_LINES_FOR_SUMMARY = 8


class Index:
    def __init__(self, db_path: Path, roots: list[Path]):
        self.roots = [Path(r).resolve() for r in roots]
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self.last_refresh = 0.0
        self._matrix = None          # (ids, normalised vectors), rebuilt when vectors change
        self._matrix_version = 0
        self._vectors_version = 0

    # --- reading the folders -------------------------------------------------------------
    def _walk(self):
        for root in self.roots:
            for folder, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in safety.SKIP_DIRS]
                for name in names:
                    path = Path(folder, name)
                    if path.suffix.lower() in chunk.TEXT_EXTENSIONS and not safety.is_secret(path):
                        yield path

    def refresh(self, max_age: float = 0.0) -> dict:
        """Bring the index up to date. Returns counts only."""
        with self.lock:
            if max_age and time.time() - self.last_refresh < max_age:
                return {"skipped": True}
            stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
            known = {r["path"]: r for r in self.conn.execute("SELECT path, hash, size, mtime FROM files")}
            seen = set()
            for path in self._walk():
                key = path.resolve().as_posix()
                seen.add(key)
                try:
                    st = path.stat()
                except OSError:
                    continue
                old = known.get(key)
                if old and old["size"] == st.st_size and old["mtime"] == st.st_mtime:
                    stats["unchanged"] += 1
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:4096]:
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if old and old["hash"] == digest:  # touched, not changed
                    self.conn.execute("UPDATE files SET size=?, mtime=? WHERE path=?",
                                      (st.st_size, st.st_mtime, key))
                    stats["unchanged"] += 1
                    continue
                self._store(key, data.decode("utf-8", errors="replace"), digest, st)
                stats["updated" if old else "added"] += 1
            for gone in set(known) - seen:
                self._delete(gone)
                stats["removed"] += 1
            self.conn.commit()
            self.last_refresh = time.time()
            return stats

    def _delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE path=?)", (key,))
        self._vectors_version += 1
        self.conn.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (key,))
        self.conn.execute("DELETE FROM chunks WHERE path=?", (key,))
        self.conn.execute("DELETE FROM files WHERE path=?", (key,))

    def _store(self, key: str, text: str, digest: str, st) -> None:
        self._delete(key)
        pieces = chunk.chunk_file(key, text)
        self.conn.execute("INSERT INTO files(path, hash, size, mtime, lines) VALUES (?,?,?,?,?)",
                          (key, digest, st.st_size, st.st_mtime, len(text.splitlines())))
        rel = self.display(key)
        for c in pieces:
            cur = self.conn.execute(
                "INSERT INTO chunks(path, first, last, kind, name, text) VALUES (?,?,?,?,?,?)",
                (key, c.start, c.end, c.kind, c.name, c.text))
            self.conn.execute(
                "INSERT INTO chunks_fts(rowid, text, words, name, path) VALUES (?,?,?,?,?)",
                (cur.lastrowid, c.text, chunk.split_identifiers(c.text),
                 f"{c.name} {chunk.split_identifiers(c.name)}".strip(),
                 f"{rel} {chunk.split_identifiers(rel)}"))

    # --- reading the index ---------------------------------------------------------------
    def display(self, key: str) -> str:
        """A short path for output: relative to its root (prefixed by the root's name when
        more than one folder is indexed)."""
        path = Path(key)
        for root in self.roots:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            return rel if len(self.roots) == 1 else f"{root.name}/{rel}"
        return path.as_posix()

    def stats(self) -> dict:
        with self.lock:
            files = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            return {"files": files, "chunks": chunks}

    def files(self, prefix: str = "") -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute("SELECT path, lines, summary FROM files ORDER BY path").fetchall()
        return [r for r in rows if self.display(r["path"]).startswith(prefix)]

    def symbols(self, key: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT name FROM chunks WHERE path=? AND name != '' AND kind != 'module' ORDER BY first",
                (key,)).fetchall()
        return [r["name"] for r in rows]

    # --- file summaries (written while the Coder is idle; lost when the file changes) ------
    def files_needing_summary(self, limit: int = 1) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT path FROM files WHERE summary IS NULL AND lines >= ? ORDER BY path LIMIT ?",
                (MIN_LINES_FOR_SUMMARY, limit)).fetchall()

    def set_summary(self, key: str, summary: str, expected_hash: str | None = None) -> bool:
        """Store a summary unless the file changed meanwhile (then it would describe old code)."""
        with self.lock:
            row = self.conn.execute("SELECT hash FROM files WHERE path=?", (key,)).fetchone()
            if not row or (expected_hash and row["hash"] != expected_hash):
                return False
            self.conn.execute("UPDATE files SET summary=? WHERE path=?", (summary, key))
            self.conn.commit()
            return True

    def file_hash(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT hash FROM files WHERE path=?", (key,)).fetchone()
        return row["hash"] if row else None

    # --- meaning vectors (made by the Coder's embedding model) -----------------------------
    def chunks_without_vectors(self, limit: int = 16) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, path, kind, name, text FROM chunks WHERE id NOT IN "
                "(SELECT chunk_id FROM vectors) ORDER BY id LIMIT ?", (limit,)).fetchall()

    def vector_text(self, row) -> str:
        return "\n".join((self.display(row["path"]), f"{row['kind']} {row['name']}",
                          row["text"][:VECTOR_TEXT_CHARS]))

    def add_vectors(self, ids: list[int], vectors: list[list[float]]) -> None:
        import numpy as np
        with self.lock:
            for cid, vec in zip(ids, vectors):
                arr = np.asarray(vec, dtype=np.float32)
                self.conn.execute("INSERT OR REPLACE INTO vectors(chunk_id, dim, vec) VALUES (?,?,?)",
                                  (cid, len(arr), arr.tobytes()))
            self.conn.commit()
            self._vectors_version += 1

    def vector_coverage(self) -> float:
        """Share of chunks that have a vector (0..1)."""
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            have = self.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        return have / total if total else 0.0

    def vector_matrix(self):
        """(ids, matrix of unit vectors) or None. Built once, rebuilt only after changes."""
        import numpy as np
        with self.lock:
            if self._matrix is not None and self._matrix_version == self._vectors_version:
                return self._matrix
            rows = self.conn.execute("SELECT chunk_id, dim, vec FROM vectors").fetchall()
            dims = {r["dim"] for r in rows}
            if not rows or len(dims) != 1:
                self._matrix = None
            else:
                matrix = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                self._matrix = (np.array([r["chunk_id"] for r in rows]), matrix / np.where(norms == 0, 1, norms))
            self._matrix_version = self._vectors_version
            return self._matrix
