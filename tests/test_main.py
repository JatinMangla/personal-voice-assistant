from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import main
from memory import Memory
from timer import SessionTimer


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, data):
        self.saved.append(data)
        return True


def test_failed_start_still_records_gpu_time(embedder, monkeypatch):
    store = FakeStore()
    memory = Memory(embedder)
    start = datetime.now(ZoneInfo(config.TIMEZONE)) - timedelta(minutes=3)

    def crashing_run(ctx):
        ctx.update(memory=memory, store=store, timer=SessionTimer(start, 600))
        raise RuntimeError("Ollama broke")

    monkeypatch.setattr(main, "_run", crashing_run)
    with pytest.raises(RuntimeError):
        main.run()
    assert store.saved, "memory with the usage must be uploaded"
    used = Memory(embedder, store.saved[-1]).get_usage(start.date().isoformat())
    assert used >= 170  # ~3 minutes


def test_crash_before_memory_loaded_does_not_try_to_save(monkeypatch):
    def crashing_run(ctx):
        raise RuntimeError("missing secret")

    monkeypatch.setattr(main, "_run", crashing_run)
    with pytest.raises(RuntimeError, match="missing secret"):
        main.run()
