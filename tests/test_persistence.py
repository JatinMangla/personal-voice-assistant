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
        return subprocess.CompletedProcess(args, code, stdout=out if code == 0 else "",
                                           stderr=out if code else "")
    store._kaggle = run


@pytest.fixture(autouse=True)
def no_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BACKUP_PATH", tmp_path / "backup.enc")
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_kaggle_credentials_new_token_and_legacy_key(monkeypatch):
    for name in ("KAGGLE_USERNAME", "KAGGLE_API_TOKEN", "KAGGLE_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAGGLE_USERNAME", "raj")
    with pytest.raises(RuntimeError, match="KAGGLE_API_TOKEN"):
        config.kaggle_credentials()
    monkeypatch.setenv("KAGGLE_API_TOKEN", " KGAT_abc ")
    assert config.kaggle_credentials() == ("raj", {"KAGGLE_USERNAME": "raj",
                                                   "KAGGLE_API_TOKEN": "KGAT_abc"})
    monkeypatch.delenv("KAGGLE_API_TOKEN")
    monkeypatch.setenv("KAGGLE_KEY", "legacy")
    assert config.kaggle_credentials()[1]["KAGGLE_KEY"] == "legacy"


def test_first_run_returns_none():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "404 - Not Found")], calls)
    assert store.load() is None
    assert not store.exists


def test_first_run_kaggle_403_for_missing_private_dataset():
    # Real Kaggle behaviour: 403 for a private dataset that doesn't exist yet.
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "403 Client Error: Forbidden"),
                        (0, "ref,title,size\nme/other-data,Other,1KB"),
                        (1, "404 Not Found")], calls)  # second, independent check
    assert store.load() is None
    assert calls[1][:2] == ("datasets", "list") and calls[2][:2] == ("datasets", "status")


def test_existing_memory_found_by_status_check_is_not_a_first_run():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "403 Client Error: Forbidden"),
                        (0, "no datasets found"),     # list is empty/odd...
                        (0, "ready")], calls)         # ...but status says it exists
    with pytest.raises(StorageError, match="Could not download"):
        store.load()


def test_never_overwrites_a_memory_that_failed_to_load():
    # Started as "first run", but the memory dataset actually exists.
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "409 Conflict: dataset already exists")], calls)
    assert store.save(b"empty new memory") is False
    assert store.blocked
    assert store.save(b"again") is False
    assert len(calls) == 1  # no "version" upload was ever attempted


def test_existing_memory_but_download_fails_refuses_to_start_empty():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "403 Client Error: Forbidden"),
                        (0, "ref,title,size\nme/assistant-memory,assistant-memory,1KB")], calls)
    with pytest.raises(StorageError, match="Could not download"):
        store.load()


def test_bad_token_is_reported_not_treated_as_first_run():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "401 Unauthorized"), (1, "401 Unauthorized")], calls)
    with pytest.raises(StorageError, match="login failed"):
        store.load()


def test_network_error_refuses_to_start_empty():
    store, calls = MemoryStore("me", KEY, {}), []
    fake_kaggle(store, [(1, "Connection timed out"), (1, "Connection timed out")], calls)
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


# --- home PC: the memory as a local encrypted file -------------------------------------
from persistence import LocalMemoryStore  # noqa: E402


def test_local_store_round_trip_and_first_run(tmp_path):
    store = LocalMemoryStore(tmp_path, KEY)
    assert store.load() is None and not store.exists       # first run
    assert store.save(b"memory v1") and store.save(b"memory v2")
    assert b"memory" not in (tmp_path / config.MEMORY_FILE_NAME).read_bytes()  # encrypted
    assert LocalMemoryStore(tmp_path, KEY).load() == b"memory v2"
    assert crypto.decrypt((tmp_path / (config.MEMORY_FILE_NAME + ".prev")).read_bytes(), KEY) \
        == b"memory v1"                                     # one previous copy only
    assert sorted(p.name for p in tmp_path.iterdir()) == [config.MEMORY_FILE_NAME,
                                                          config.MEMORY_FILE_NAME + ".prev"]


def test_local_store_never_starts_empty_over_a_real_memory(tmp_path):
    LocalMemoryStore(tmp_path, KEY).save(b"real memory")
    with pytest.raises(crypto.DecryptionError):             # wrong key: stop, don't start empty
        LocalMemoryStore(tmp_path, crypto.load_key(crypto.generate_key())).load()
    LocalMemoryStore(tmp_path, KEY).save(b"newer")
    (tmp_path / config.MEMORY_FILE_NAME).unlink()           # interrupted save
    with pytest.raises(StorageError, match="rename it back"):
        LocalMemoryStore(tmp_path, KEY).load()
