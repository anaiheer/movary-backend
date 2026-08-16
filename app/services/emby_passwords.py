from __future__ import annotations

import base64
import os
from typing import Optional, TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_PREFIX = "enc:v1:"
_NONCE_SIZE = 12


def is_encrypted(value: str | None) -> bool:
    return bool(value and value.startswith(_PREFIX))


def _load_key() -> Optional[bytes]:
    raw = (settings.EMBY_PASSWORD_KEY or "").strip()
    if not raw:
        return None
    # Prefer urlsafe base64, fall back to hex or raw bytes.
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = raw.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise ValueError("EMBY_PASSWORD_KEY must be 16/24/32 bytes (base64/hex/raw).")
    return key


def validate_emby_password_key() -> None:
    key = _load_key()
    if not key:
        raise ValueError("EMBY_PASSWORD_KEY is not configured.")
    _get_aesgcm()


def _get_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise ValueError("cryptography is required for Emby password encryption.") from exc
    return AESGCM


def encrypt_emby_password(password: str | None) -> str | None:
    if not password:
        return None
    key = _load_key()
    if not key:
        raise ValueError("EMBY_PASSWORD_KEY is not configured.")
    aesgcm = _get_aesgcm()(key)
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, password.encode("utf-8"), None)
    token = base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")
    return f"{_PREFIX}{token}"


def decrypt_emby_password(value: str | None) -> str | None:
    if not value:
        return None
    if not value.startswith(_PREFIX):
        return value
    key = _load_key()
    if not key:
        raise ValueError("EMBY_PASSWORD_KEY is not configured.")
    token = value[len(_PREFIX) :]
    data = base64.urlsafe_b64decode(token)
    if len(data) <= _NONCE_SIZE:
        return None
    nonce = data[:_NONCE_SIZE]
    ciphertext = data[_NONCE_SIZE:]
    plain = _get_aesgcm()(key).decrypt(nonce, ciphertext, None)
    return plain.decode("utf-8")


async def migrate_emby_passwords(db: "AsyncSession") -> dict:
    from sqlalchemy import select
    from app.models.emby import EmbyAccount

    stmt = select(EmbyAccount).where(EmbyAccount.emby_password.is_not(None))
    accounts = (await db.execute(stmt)).scalars().all()
    updated = 0
    skipped = 0
    for account in accounts:
        raw = account.emby_password
        if not raw:
            skipped += 1
            continue
        if is_encrypted(raw):
            skipped += 1
            continue
        account.emby_password = encrypt_emby_password(raw)
        db.add(account)
        updated += 1
    await db.commit()
    return {"updated": updated, "skipped": skipped}
