"""Encryption of connector secrets at rest (Fernet).

The key comes from ENCRYPTION_KEY. In local mode an empty value means a key is
generated once and kept in the data volume, so a laptop needs no setup. Server
mode always requires the env value (checked at startup).
"""

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from opsis.settings import Settings

_cache: dict[str, Fernet] = {}


class SecretsError(Exception):
    pass


def _local_key(settings: Settings) -> bytes:
    path = settings.data_dir / "secrets" / "encryption.key"
    if path.is_file():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


def _fernet(settings: Settings) -> Fernet:
    if settings.encryption_key is not None:
        key = settings.encryption_key.get_secret_value().encode()
        cache_key = "env"
    else:
        cache_key = str(settings.data_dir)
        if cache_key in _cache:
            return _cache[cache_key]
        key = _local_key(settings)
    if cache_key not in _cache or cache_key == "env":
        _cache[cache_key] = Fernet(key)
    return _cache[cache_key]


def encrypt(settings: Settings, values: dict[str, Any]) -> str | None:
    if not values:
        return None
    return _fernet(settings).encrypt(json.dumps(values).encode()).decode()


def decrypt(settings: Settings, token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        data: dict[str, Any] = json.loads(_fernet(settings).decrypt(token.encode()))
    except InvalidToken:
        raise SecretsError(
            "Saved secrets cannot be read with the current ENCRYPTION_KEY. "
            "Restore the original key, or enter the connection's secrets again."
        ) from None
    return data
