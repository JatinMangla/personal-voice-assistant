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
        """True/False from the list of your own datasets. Raises if Kaggle refuses
        the login itself, so a wrong token is never mistaken for "no memory yet"."""
        result = self._kaggle("datasets", "list", "--mine", "--search",
                              config.MEMORY_DATASET_SLUG, "--csv")
        if result.returncode != 0:
            raise StorageError("Kaggle login failed. Check the KAGGLE_USERNAME and "
                               "KAGGLE_API_TOKEN secrets. Details: "
                               f"{(result.stderr or result.stdout).strip()[-300:]}")
        refs = [line.split(",", 1)[0].strip().lower() for line in result.stdout.splitlines()]
        return self.dataset.lower() in refs

    def save(self, plaintext: bytes, attempts: int = 3) -> bool:
        blob = crypto.encrypt(plaintext, self.key)
        if crypto.decrypt(blob, self.key) != plaintext:  # never upload a broken file
            raise StorageError("Encryption self-check failed.")
        self._write_backup(blob)

        for attempt in range(1, attempts + 1):
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / config.MEMORY_FILE_NAME).write_bytes(blob)
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
            if result.returncode == 0:
                self.exists = True
                self.last_saved = time.time()
                return True
            print(f"[memory] upload attempt {attempt} failed: {result.stderr.strip()[-200:]}")
            if not self.exists and "already" in (result.stdout + result.stderr).lower():
                self.exists = True  # created earlier; next attempt makes a new version
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
