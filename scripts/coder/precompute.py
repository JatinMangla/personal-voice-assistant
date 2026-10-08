"""Idle work while the Coder is running: meaning vectors for new code pieces and a one-line
summary per file. Later searches and the repo map get better and answers come from cache.

Rules that keep it harmless (a bug here once kept the GPU running for hours):
- it looks at what is left to do on the laptop FIRST; with nothing to do it sends no request at all
  and only looks again every IDLE_RECHECK_SEC;
- while the Coder is offline it tries again only every OFFLINE_RECHECK_SEC;
- every request it makes is marked as background, so the Coder does not count it as use and its
  20-minute idle stop is never postponed by it;
- it does one small step at a time and only when no tool has been called for a while.
"""
import threading
import time

from . import tasks
from .client import CoderError, Offline

IDLE_AFTER_SEC = 30.0
TICK_SEC = 5.0
IDLE_RECHECK_SEC = 600.0       # nothing left to do: look again in 10 minutes (no network)
OFFLINE_RECHECK_SEC = 300.0    # the Coder is off: try again in 5 minutes
VECTOR_BATCH = 16


class Precomputer:
    def __init__(self, coder, idle_after: float = IDLE_AFTER_SEC, tick: float = TICK_SEC,
                 clock=time.time):
        self.coder, self.idle_after, self.tick, self.clock = coder, idle_after, tick, clock
        self.wait = tick                 # seconds until the next look
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
        while not self.stopping.wait(self.wait):
            self.wait = self.tick
            try:
                self.step()
            except Exception:       # background help: never take the tool server down
                pass

    def quiet(self) -> bool:
        return self.clock() - self.coder.last_activity >= self.idle_after

    def step(self) -> str | None:
        """One unit of work, or None if there is nothing to do / it is not the time.
        Returns 'vectors' or 'summary' (for tests and logs). Sets self.wait for the next look."""
        coder = self.coder
        if not coder.client or not self.quiet():
            return None
        coder.index.refresh(max_age=60.0)
        rows = coder.index.chunks_without_vectors(VECTOR_BATCH)
        pending = [] if rows else coder.index.files_needing_summary(1)
        if not rows and not pending:
            self.wait = IDLE_RECHECK_SEC      # nothing to do: stay silent, no request at all
            return None
        try:
            if not coder.client.online(background=True):
                self.wait = OFFLINE_RECHECK_SEC
                return None
            if rows:
                vectors = coder.client.embed([coder.index.vector_text(r) for r in rows], background=True)
                coder.index.add_vectors([r["id"] for r in rows], vectors)
                return "vectors"
            return self._summarize(pending[0]["path"])
        except (Offline, CoderError):
            self.wait = OFFLINE_RECHECK_SEC
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
