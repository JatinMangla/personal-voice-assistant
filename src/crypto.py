"""AES-256-GCM encryption for the memory database.

Blob format: MAGIC (4 bytes) + nonce (12 bytes) + ciphertext-with-tag.
The key is 32 random bytes, stored as url-safe base64 in the MEMORY_KEY secret.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"PVA1"
NONCE_SIZE = 12


class DecryptionError(Exception):
    """Wrong key or corrupted file."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def load_key(key_b64: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(key_b64.strip().encode())
    except Exception as e:
        raise ValueError("MEMORY_KEY is not valid base64. Run scripts/make_key.py.") from e
    if len(key) != 32:
        raise ValueError("MEMORY_KEY must decode to 32 bytes. Run scripts/make_key.py.")
    return key


def encrypt(data: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise DecryptionError("Not a memory file (bad header).")
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE_SIZE]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + NONCE_SIZE:], MAGIC)
    except InvalidTag as e:
        raise DecryptionError("Wrong MEMORY_KEY or corrupted memory file.") from e


# --- big files (the fine-tuned model, ~5 GB) in chunks, so RAM use stays small -----
FILE_MAGIC = b"PVC1"
CHUNK_SIZE = 64 * 1024 * 1024


def _chunk_aad(index: int, last: bool) -> bytes:
    # Binding the position and the "last" flag stops chunks being reordered or cut off.
    return FILE_MAGIC + index.to_bytes(8, "big") + (b"\x01" if last else b"\x00")


def encrypt_file(src, dst, key: bytes, chunk_size: int = CHUNK_SIZE) -> None:
    aes = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(FILE_MAGIC)
        index, chunk = 0, fin.read(chunk_size)
        while True:
            following = fin.read(chunk_size)
            last = not following
            nonce = os.urandom(NONCE_SIZE)
            sealed = aes.encrypt(nonce, chunk, _chunk_aad(index, last))
            fout.write(nonce + len(sealed).to_bytes(4, "big") + sealed)
            if last:
                return
            index, chunk = index + 1, following


def decrypt_file(src, dst, key: bytes) -> None:
    aes = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if fin.read(len(FILE_MAGIC)) != FILE_MAGIC:
            raise DecryptionError("Not an encrypted model file (bad header).")
        index = 0
        while True:
            header = fin.read(NONCE_SIZE + 4)
            if len(header) < NONCE_SIZE + 4:
                raise DecryptionError("Encrypted file is truncated.")
            nonce, size = header[:NONCE_SIZE], int.from_bytes(header[NONCE_SIZE:], "big")
            sealed = fin.read(size)
            last = fin.peek(1) == b""  # nothing after this chunk
            try:
                fout.write(aes.decrypt(nonce, sealed, _chunk_aad(index, last)))
            except InvalidTag as e:
                raise DecryptionError("Wrong MEMORY_KEY or corrupted model file.") from e
            if last:
                return
            index += 1
