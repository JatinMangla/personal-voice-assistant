import subprocess
from pathlib import Path

import pytest

import config
import crypto
from persistence import MemoryStore, StorageError

KEY = crypto.load_key(crypto.generate_key())


def fake_kaggle(store, responses, calls, remote_blob=None):
    """Replace the kaggle CLI: returns queued (returncode, output) per call."""
    def run(*args):
        calls.append(args)
        if args[1] == "download" and remote_blob is not None:
            (Path(args[args.index("-p") + 1]) / config.MEMORY_FILE_NAME).write_bytes(remote_blob)
        code, out = responses.pop(0)
        return subprocess.CompletedProcess(args, code, stdout="", stderr=out)
    store._kaggle = run


@pytest.fixture(autouse=True)
def no_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BACKUP_PATH", tmp_path / "backup.enc")
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_first_run_returns_none():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "404 - Not Found")], calls)
    assert store.load() is None
    assert not store.exists


def test_network_error_refuses_to_start_empty():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "Connection timed out")], calls)
    with pytest.raises(StorageError):
        store.load()


def test_load_decrypts_remote_file():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(0, "")], calls, remote_blob=crypto.encrypt(b"db", KEY))
    assert store.load() == b"db"
    assert store.exists


def test_save_creates_then_versions_and_deletes_old():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(0, ""), (0, "")], calls)
    assert store.save(b"one")
    assert store.save(b"two")
    assert calls[0][:2] == ("datasets", "create")
    assert calls[1][:2] == ("datasets", "version") and "--delete-old-versions" in calls[1]
    assert crypto.decrypt(config.BACKUP_PATH.read_bytes(), KEY) == b"two"


def test_save_retries_then_reports_failure():
    store, calls = MemoryStore("me", KEY, {}), []
    store.exists = True
    fake_kaggle(store, [(1, "500"), (1, "500"), (1, "500")], calls)
    assert store.save(b"x") is False
    assert len(calls) == 3
