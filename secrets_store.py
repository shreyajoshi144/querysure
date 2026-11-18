"""Encrypted storage for connection secrets (Sprint 4).

Secrets are a small JSON object (today just ``{"password": ...}``) encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The
plaintext also carries the owning connection's id, and ``decrypt`` refuses a ciphertext whose id differs, so a stored blob cannot
be copied onto another connection row. Keys come from config.CONNECTIONS_ENCRYPTION_KEYS (first encrypts, all decrypt, for
rotation); only in development is a key derived from JWT_SECRET_KEY so local use needs no setup. Nothing here logs a value, and
callers must never return a decrypted secret in an API response.
"""
from __future__ import annotations

import base64
import hashlib
import json

import config


class SecretsUnavailable(RuntimeError):
    """No usable encryption key (production without CONNECTIONS_ENCRYPTION_KEY) or an undecryptable blob."""


def _keys() -> list:
    if config.CONNECTIONS_ENCRYPTION_KEYS:
        return list(config.CONNECTIONS_ENCRYPTION_KEYS)
    if config.IS_PRODUCTION:
        return []
    digest = hashlib.sha256(b"querysure-connections-dev:" + (config.JWT_SECRET_KEY or "").encode()).digest()
    return [base64.urlsafe_b64encode(digest).decode()]


def configured() -> bool:
    return bool(_keys())


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet
    keys = _keys()
    if not keys:
        raise SecretsUnavailable("CONNECTIONS_ENCRYPTION_KEY is not configured.")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception as exc:  # noqa: BLE001 - a malformed key must not echo its value
        raise SecretsUnavailable("CONNECTIONS_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt(connection_id: str, secrets: dict) -> str:
    payload = json.dumps({"id": connection_id, "secrets": secrets}, separators=(",", ":")).encode()
    return _fernet().encrypt(payload).decode()


def decrypt(connection_id: str, token: str) -> dict:
    from cryptography.fernet import InvalidToken
    try:
        data = json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as exc:
        raise SecretsUnavailable("The stored connection secret cannot be decrypted (wrong or rotated-out key).") from exc
    if data.get("id") != connection_id:
        raise SecretsUnavailable("The stored connection secret does not belong to this connection.")
    return data.get("secrets") or {}
