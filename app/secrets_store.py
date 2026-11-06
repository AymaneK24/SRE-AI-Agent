"""Encrypts stored credentials and removes secrets from text that leaves the app."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import Config


class SecretBox:
    def __init__(self, config: Config) -> None:
        self._fernet = Fernet(self._load_key(config))

    def _load_key(self, config: Config) -> bytes:
        if config.secret_key:
            digest = hashlib.sha256(config.secret_key.encode()).digest()
            return base64.urlsafe_b64encode(digest)
        path: Path = config.data_dir / "secret.key"
        config.data_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(Fernet.generate_key())
        return path.read_bytes().strip()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as error:
            raise ValueError(
                "A stored credential cannot be decrypted. The encryption key has changed, "
                "so enter the credential again."
            ) from error

    def encrypt_json(self, value: dict[str, Any]) -> str:
        return self.encrypt(json.dumps(value)) if value else ""

    def decrypt_json(self, value: str) -> dict[str, Any]:
        return json.loads(self.decrypt(value)) if value else {}


_SCRUB_PATTERNS = [
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b(\"?\s*[=:]\s*\"?)[^\s\"',&}]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
]


def scrub(text: str) -> str:
    """Remove obvious credentials from telemetry text before it is logged or sent to a model."""
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
