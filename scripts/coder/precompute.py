"""Idle work while the Coder is running: meaning vectors for new code pieces and a one-line
summary per file. Later searches and the repo map get better and answers come from cache.
It does one small step at a time and only when no tool has been called for a while, so it never
delays a real request, and it does nothing at all while the Coder is offline."""
import threading
import time

from . import tasks
from .client import CoderError, Offline

IDLE_AFTER_SEC = 30.0
TICK_SEC = 5.0
VECTOR_BATCH = 16


class Precomputer:
    def __init__(self, coder, idle_after: float = IDLE_AFTER_SEC, tick: float = TICK_SEC,
                 clock=time.time):
        self.coder, self.idle_after, self.tick, self.clock = coder, idle_after, tick, clock
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread or not self.coder.client:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()

    def _loop(self) -> None:
        while not self.stopping.wait(self.tick):
            try:
                self.step()
            except Exception:       # background help: never take the tool server down
                pass

    def quiet(self) -> bool:
        return self.clock() - self.coder.last_activity >= self.idle_after

    def step(self) -> str | None:
        """One unit of work, or None if there is nothing to do / it is not the time.
        Returns 'vectors' or 'summary' (for tests and logs)."""
        coder = self.coder
        if not coder.client or not self.quiet():
            return None
        coder.index.refresh(max_age=60.0)
        try:
            if not coder.client.online():
                return None
            rows = coder.index.chunks_without_vectors(VECTOR_BATCH)
            if rows:
                vectors = coder.client.embed([coder.index.vector_text(r) for r in rows])
                coder.index.add_vectors([r["id"] for r in rows], vectors)
                return "vectors"
            pending = coder.index.files_needing_summary(1)
            if pending:
                return self._summarize(pending[0]["path"])
        except (Offline, CoderError):
            return None
        return None

    def _summarize(self, key: str) -> str | None:
        index = self.coder.index
        digest = index.file_hash(key)
        try:
            text = open(key, encoding="utf-8", errors="replace").read()
        except OSError:
            return None
        line = tasks.file_summary(self.coder.client, index.display(key), text)
        if line and index.set_summary(key, line, digest):
            return "summary"
        if not line:        # an empty answer would be retried forever: mark with a plain text
            index.set_summary(key, "(no summary)", digest)
        return None
