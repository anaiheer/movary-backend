from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_token
from app.models.telegram import (
    TelegramNotification,
    TelegramNotificationPreference,
    TelegramUserBinding,
)
from app.models.user import User
from app.schemas.telegram import TelegramAuthUser, TelegramTokenResponse
from app.services.cache import get_cache_client


def utcnow() -> datetime:
    return datetime.utcnow()


def get_client_ip(request) -> str:
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


async def enforce_rate_limit(
    *,
    key: str,
    limit: int,
    window_seconds: int,
    detail: str,
) -> None:
    client = get_cache_client()
    if client is None:
        return
    try:
        current = int(await client.incr(key))
        if current == 1:
            await client.expire(key, int(window_seconds))
    except RedisError:
        return
    if current > limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def serialize_telegram_auth_user(user: User) -> TelegramAuthUser:
    return TelegramAuthUser(
        id=user.id,
        username=user.username,
        email=user.email,
        status=user.status.value if hasattr(user.status, "value") else str(user.status),
        balance=float(user.balance or 0),
        vod_movie_limit=int(user.vod_movie_limit or 0),
        vod_tv_limit=int(user.vod_tv_limit or 0),
        vod_movie_used=int(user.vod_movie_used or 0),
        vod_tv_used=int(user.vod_tv_used or 0),
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
    )


def build_telegram_token_response(user: User) -> TelegramTokenResponse:
    access_token = create_token({"sub": str(user.id)}, token_type="access")
    refresh_token = create_token({"sub": str(user.id)}, token_type="refresh")
    return TelegramTokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=serialize_telegram_auth_user(user),
    )


async def get_active_binding_by_telegram_user_id(
    db: AsyncSession, telegram_user_id: str
) -> TelegramUserBinding | None:
    stmt = select(TelegramUserBinding).where(
        TelegramUserBinding.telegram_user_id == telegram_user_id,
        TelegramUserBinding.is_active.is_(True),
    )
    return await db.scalar(stmt)


async def get_active_binding_by_user_id(
    db: AsyncSession, user_id: UUID
) -> TelegramUserBinding | None:
    stmt = select(TelegramUserBinding).where(
        TelegramUserBinding.user_id == user_id,
        TelegramUserBinding.is_active.is_(True),
    )
    return await db.scalar(stmt)


async def get_or_create_notification_preferences(
    db: AsyncSession, user_id: UUID
) -> tuple[TelegramNotificationPreference, bool]:
    stmt = select(TelegramNotificationPreference).where(
        TelegramNotificationPreference.user_id == user_id
    )
    preference = await db.scalar(stmt)
    if preference is not None:
        return preference, False
    preference = TelegramNotificationPreference(user_id=user_id)
    db.add(preference)
    await db.flush()
    return preference, True


async def touch_binding(
    db: AsyncSession,
    binding: TelegramUserBinding,
    *,
    timestamp: datetime | None = None,
) -> None:
    binding.last_interaction_at = timestamp or utcnow()
    db.add(binding)


async def telegram_notification_exists(
    db: AsyncSession,
    *,
    user_id: UUID,
    notification_type: str,
    reference_id: str | None = None,
) -> bool:
    stmt = (
        select(func.count())
        .select_from(TelegramNotification)
        .where(
            TelegramNotification.user_id == user_id,
            TelegramNotification.type == notification_type,
        )
    )
    if reference_id is None:
        stmt = stmt.where(TelegramNotification.reference_id.is_(None))
    else:
        stmt = stmt.where(TelegramNotification.reference_id == reference_id)
    total = await db.scalar(stmt)
    return bool(total)


async def create_telegram_notification(
    db: AsyncSession,
    *,
    user_id: UUID,
    notification_type: str,
    title: str,
    content: str,
    reference_id: str | None = None,
    commit: bool = False,
) -> TelegramNotification | None:
    binding = await get_active_binding_by_user_id(db, user_id)
    if binding is None:
        return None

    preferences, _ = await get_or_create_notification_preferences(db, user_id)
    if not preferences.is_type_enabled(notification_type):
        return None

    notification = TelegramNotification(
        user_id=user_id,
        type=notification_type,
        title=title,
        content=content,
        reference_id=reference_id,
    )
    db.add(notification)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(notification)
    return notification


def verify_webapp_init_data(init_data: str, bot_token: str, *, max_age_seconds: int = 300) -> dict:
    pairs = parse_qsl(init_data, keep_blank_values=True)
    payload = dict(pairs)
    their_hash = payload.pop("hash", None)
    if not their_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData 验证失败")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    actual_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(actual_hash, their_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData 验证失败")

    try:
        auth_date = int(payload.get("auth_date") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData 已过期",
        ) from exc
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if auth_date <= 0 or now_ts - auth_date > max_age_seconds:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData 已过期")

    user_raw = payload.get("user")
    if not user_raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData 验证失败")
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData 验证失败",
        ) from exc


def verify_login_widget_payload(
    payload: dict,
    bot_token: str,
    *,
    max_age_seconds: int = 300,
) -> dict:
    payload_data = {key: value for key, value in payload.items() if value not in (None, "")}
    their_hash = str(payload_data.pop("hash", "")).strip()
    if not their_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram 验证失败")

    data_check_string = "\n".join(
        f"{key}={payload_data[key]}" for key in sorted(payload_data.keys())
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    actual_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(actual_hash, their_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram 验证失败")

    try:
        auth_date = int(payload_data.get("auth_date") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram 登录信息已过期",
        ) from exc

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if auth_date <= 0 or now_ts - auth_date > max_age_seconds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram 登录信息已过期",
        )

    return payload_data


def format_notification_amount(value) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))
