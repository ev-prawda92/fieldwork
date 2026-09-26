"""Encryption at rest for secrets Fieldwork has to be able to read back.

Webhook signing secrets and SSO client secrets can't be hashed, because
Fieldwork has to use them. They're encrypted with Fernet (AES-128-CBC +
HMAC-SHA256) before they touch the database.

Keys:
    FIELDWORK_SECRET_KEYS=<newest>,<older>,...   comma-separated Fernet keys
        The first key encrypts; every key can decrypt. To rotate: put a new key
        first, run `python -m fieldwork rotate-keys`, then drop the old key.
    Without it (local/demo only), a key is generated once into
        FIELDWORK_KEY_FILE (default ./.fieldwork_key, mode 0600).

In a managed deployment the key comes from the cloud KMS / secret manager into
FIELDWORK_SECRET_KEYS; the database never holds a key.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

PREFIX = "enc:v1:"


class SecretError(RuntimeError):
    pass


def _keys() -> list[bytes]:
    env = os.environ.get("FIELDWORK_SECRET_KEYS", "").strip()
    if env:
        return [k.strip().encode() for k in env.split(",") if k.strip()]
    path = Path(os.environ.get("FIELDWORK_KEY_FILE", ".fieldwork_key"))
    if not path.exists():
        key = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    return [path.read_bytes().strip()]


def using_dev_key() -> bool:
    return not os.environ.get("FIELDWORK_SECRET_KEYS", "").strip()


def _fernet() -> MultiFernet:
    try:
        return MultiFernet([Fernet(k) for k in _keys()])
    except ValueError as e:
        raise SecretError(f"FIELDWORK_SECRET_KEYS holds an invalid Fernet key: {e}")


def encrypt(plaintext: str) -> str:
    return PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt(stored: str | None) -> str | None:
    if stored is None:
        return None
    if not stored.startswith(PREFIX):
        return stored  # written before encryption existed; re-encrypted by rotate-keys
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise SecretError("can't decrypt a stored secret with the configured keys")


def rotate(stored: str) -> str:
    """Re-encrypt under the newest key (also encrypts legacy plaintext)."""
    if not stored.startswith(PREFIX):
        return encrypt(stored)
    return PREFIX + _fernet().rotate(stored[len(PREFIX):].encode()).decode()


def rotate_all(conn) -> int:
    """Re-encrypt every stored secret under the newest key. Returns count."""
    n = 0
    with conn.tx():
        for r in list(conn.execute("SELECT tenant_id, engine_key, secret FROM engine_credentials"
                                   " WHERE secret IS NOT NULL")):
            conn.execute("UPDATE engine_credentials SET secret=? WHERE tenant_id=? AND engine_key=?",
                         (rotate(r["secret"]), r["tenant_id"], r["engine_key"]))
            n += 1
        for r in list(conn.execute("SELECT tenant_id, name, secret FROM tenant_secrets")):
            conn.execute("UPDATE tenant_secrets SET secret=? WHERE tenant_id=? AND name=?",
                         (rotate(r["secret"]), r["tenant_id"], r["name"]))
            n += 1
    return n
