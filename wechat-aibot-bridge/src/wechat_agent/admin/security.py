"""Password, token and encrypted-secret primitives using reviewed algorithms."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PBKDF2_ITERATIONS = 600_000


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    groups = sum(
        bool(pattern.search(password))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"\d"), re.compile(r"[^\w]"))
    )
    if groups < 3:
        raise ValueError("Password must use at least three character groups")


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = _unb64(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt_text), int(raw_iterations)
        )
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(48)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SecretBox:
    """AES-256-GCM envelope for credentials stored in SQLite."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Admin master key must be exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def load(cls, key_path: Path) -> "SecretBox":
        env_key = os.getenv("ADMIN_MASTER_KEY", "").strip()
        if env_key:
            key = _unb64(env_key)
        else:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            if key_path.exists():
                key = _unb64(key_path.read_text(encoding="ascii").strip())
            else:
                key = secrets.token_bytes(32)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                descriptor = os.open(key_path, flags, 0o600)
                try:
                    os.write(descriptor, _b64(key).encode("ascii"))
                finally:
                    os.close(descriptor)
        return cls(key)

    def encrypt(self, plaintext: str, *, context: str = "wecom-admin") -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode(), context.encode())
        return "v1." + _b64(nonce + ciphertext)

    def decrypt(self, envelope: str, *, context: str = "wecom-admin") -> str:
        if not envelope.startswith("v1."):
            raise ValueError("Unsupported secret envelope")
        payload = _unb64(envelope[3:])
        return self._cipher.decrypt(payload[:12], payload[12:], context.encode()).decode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
