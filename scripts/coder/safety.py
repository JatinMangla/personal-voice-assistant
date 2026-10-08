"""What the coder tools may read. Only files inside the allowed folders, and never secrets."""
import hashlib
from pathlib import Path

SECRET_SUFFIXES = {".enc", ".jks", ".keystore", ".p12", ".pfx", ".pem", ".key", ".gpg", ".kdbx"}
SECRET_NAMES = {"kaggle.json", "id_rsa", "id_ed25519", "local.properties", "ngrok.yml"}
SECRET_WORDS = ("secret", "password", "passwd", "credential", "private_key", "apikey", "api_key")
SECRET_DIRS = {"jarvis-keys", ".ssh", ".gnupg", ".aws"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "__pycache__", ".gradle",
             ".idea", ".pytest_cache", ".cxx", "dist", ".mypy_cache"}


def is_secret(path: Path) -> bool:
    name = path.name.lower()
    return (name.startswith(".env")
            or name in SECRET_NAMES
            or path.suffix.lower() in SECRET_SUFFIXES
            or any(word in name for word in SECRET_WORDS)
            or any(part.lower() in SECRET_DIRS for part in path.parts))


def resolve_allowed(path: str | Path, roots: list[Path]) -> Path | None:
    """The real path if it is a file inside one of the roots and not a secret, else None.
    Symlinks and `..` are resolved first, so they can't lead outside the roots."""
    try:
        real = Path(path).resolve()
    except OSError:
        return None
    if is_secret(real) or not real.is_file():
        return None
    for root in roots:
        try:
            real.relative_to(root.resolve())
            return real
        except ValueError:
            continue
    return None


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
