"""Load and save the encrypted memory file in a private Kaggle Dataset."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import config
import crypto


class StorageError(Exception):
    pass


class MemoryStore:
    def __init__(self, username: str, key: bytes, kaggle_env: dict[str, str]):
        self.username = username
        self.key = key
        self.dataset = f"{username}/{config.MEMORY_DATASET_SLUG}"
        self.env = {**os.environ, **kaggle_env}
        self.exists = False
        self.blocked = False  # True = never save (protects a memory we failed to load)
        self.last_saved: float | None = None

    def _kaggle(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["kaggle", *args], env=self.env, capture_output=True,
                              text=True, timeout=300)

    def load(self) -> bytes | None:
        """Return the decrypted database, or None on the very first run.

        Raises on any other problem, so a failed download never starts an empty
        memory that would later overwrite the real one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = self._kaggle("datasets", "download", self.dataset, "-p", tmp,
                                  "--unzip", "--force")
            output = (result.stdout + result.stderr).lower()
            if result.returncode != 0:
                # Kaggle answers 403 (not 404) for a private dataset that doesn't exist
                # yet, so ask the list of our own datasets to tell "first run" apart.
                if "404" in output or "not found" in output or not self._memory_dataset_exists():
                    return None
                raise StorageError(f"Could not download memory: {result.stderr.strip()[-300:]}")
            files = list(Path(tmp).rglob(config.MEMORY_FILE_NAME))
            if not files:
                raise StorageError("Memory dataset exists but has no memory file in it.")
            self.exists = True
            return crypto.decrypt(files[0].read_bytes(), self.key)

    def _memory_dataset_exists(self) -> bool:
        """Asked two independent ways; "no" only if both say no. Raises if Kaggle
        refuses the login itself, so a wrong token is never mistaken for "no memory yet"."""
        result = self._kaggle("datasets", "list", "--mine", "--search",
                              config.MEMORY_DATASET_SLUG, "--csv")
        if result.returncode != 0:
            raise StorageError("Kaggle login failed. Check the KAGGLE_USERNAME and "
                               "KAGGLE_API_TOKEN secrets. Details: "
                               f"{(result.stderr or result.stdout).strip()[-300:]}")
        if self.dataset.lower() in result.stdout.lower():
            return True
        return self._kaggle("datasets", "status", self.dataset).returncode == 0

    def save(self, plaintext: bytes, usage: dict | None = None, attempts: int = 3) -> bool:
        """Upload the encrypted memory. `usage` (budget.usage_snapshot) is saved next to
        it as its own small encrypted file, for the Start button's limit check."""
        if self.blocked:
            return False
        blob = crypto.encrypt(plaintext, self.key)
        if crypto.decrypt(blob, self.key) != plaintext:  # never upload a broken file
            raise StorageError("Encryption self-check failed.")
        self._write_backup(blob)
        usage_blob = crypto.encrypt(json.dumps(usage).encode(), self.key) if usage else None

        for attempt in range(1, attempts + 1):
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / config.MEMORY_FILE_NAME).write_bytes(blob)
                if usage_blob:
                    (folder / config.USAGE_FILE_NAME).write_bytes(usage_blob)
                (folder / "dataset-metadata.json").write_text(json.dumps({
                    "title": config.MEMORY_DATASET_SLUG,
                    "id": self.dataset,
                    "licenses": [{"name": "other"}],
                }))
                if self.exists:
                    args = ["datasets", "version", "-p", tmp, "-m", "autosave"]
                    if config.DELETE_OLD_DATASET_VERSIONS:
                        args.append("--delete-old-versions")
                else:
                    args = ["datasets", "create", "-p", tmp]  # private by default
                result = self._kaggle(*args)
            output = (result.stdout + result.stderr).lower()
            if result.returncode == 0 and "error" not in output:
                self.exists = True
                self.last_saved = time.time()
                return True
            print(f"[memory] upload attempt {attempt} failed: {result.stderr.strip()[-200:]}")
            if not self.exists and ("already" in output or "409" in output):
                # We started with an empty memory, but a saved memory exists: it could not
                # be loaded this time. NEVER replace it with this empty one.
                self.blocked = True
                print("[memory] NOT SAVING: your saved memory exists but was not loaded at "
                      "start. It is left untouched; restart Jarvis to load it.")
                return False
            time.sleep(5 * attempt)
        return False

    @staticmethod
    def _write_backup(blob: bytes) -> None:
        """Encrypted copy in the notebook output, in case every upload fails."""
        try:
            config.BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.BACKUP_PATH.with_suffix(".tmp")
            tmp.write_bytes(blob)
            shutil.move(tmp, config.BACKUP_PATH)
        except OSError:
            pass


class LocalMemoryStore:
    """The encrypted memory as a file on your own PC (HOSTING = "home").

    Same promises as the Kaggle store: a file that exists but can't be read stops the
    start (never an empty memory that would overwrite it), and saving is atomic (a
    crash mid-save leaves the old file intact). One previous copy is kept; older ones
    are replaced, so "forget" also leaves the disk after the next save."""

    def __init__(self, folder: Path, key: bytes):
        self.path = Path(folder) / config.MEMORY_FILE_NAME
        self.previous = self.path.with_name(self.path.name + ".prev")
        self.key = key
        self.exists = self.path.exists()
        self.blocked = False
        self.last_saved: float | None = None

    def load(self) -> bytes | None:
        if not self.path.exists():
            if self.previous.exists():  # the last save was interrupted between the renames
                raise StorageError(f"{self.path} is missing but {self.previous.name} exists: "
                                   "rename it back to use it, then start again.")
            return None  # really the first run
        return crypto.decrypt(self.path.read_bytes(), self.key)

    def save(self, plaintext: bytes, usage: dict | None = None) -> bool:
        if self.blocked:
            return False
        blob = crypto.encrypt(plaintext, self.key)
        if crypto.decrypt(blob, self.key) != plaintext:  # never write a broken file
            raise StorageError("Encryption self-check failed.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            if self.path.exists():
                os.replace(self.path, self.previous)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[memory] could not write {self.path.name}: {type(e).__name__}")
            return False
        self.exists = True
        self.last_saved = time.time()
        return True
