import os
from pathlib import Path

from cryptography.fernet import Fernet


def write_private_file(path: Path, data: bytes) -> None:
    """Create a file readable only by the owner; fails if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class SecretBox:
    """Encrypts secrets (printer API keys, tokens) at rest."""

    def __init__(self, key_file: Path) -> None:
        if not key_file.exists():
            write_private_file(key_file, Fernet.generate_key())
        self._fernet = Fernet(key_file.read_bytes().strip())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
