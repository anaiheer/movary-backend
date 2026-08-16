from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user, verify_password
from app.db.session import get_db
from app.models.system_settings import SystemSettings
from app.models.telegram import (
    TelegramNotification,
    TelegramNotificationPreference,
    TelegramUserBinding,
)
from app.models.user import User, UserStatus
from app.schemas.telegram import (
    TelegramAuthRequest,
    TelegramBindRequest,
    TelegramBindingOut,
    TelegramMarkAllReadResponse,
    TelegramMessageResponse,
    TelegramNotificationListResponse,
    TelegramNotificationOut,
    TelegramNotificationPreferenceOut,
    TelegramNotificationPreferenceUpdate,
    TelegramTokenResponse,
    TelegramWebAppAuthRequest,
    TelegramWidgetAuthRequest,
)
from app.schemas.username import normalize_login_identifier
from app.services.social_auth import is_social_auth_action_enabled
from app.services.telegram import (
    build_telegram_token_response,
    enforce_rate_limit,
    get_active_binding_by_telegram_user_id,
    get_active_binding_by_user_id,
    get_client_ip,
    get_or_create_notification_preferences,
    touch_binding,
    utcnow,
    verify_login_widget_payload,
    verify_webapp_init_data,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _ensure_telegram_user_allowed(user: User) -> None:
    if user.deleted_at is not None or user.status == UserStatus.DELETED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号状态异常")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号状态异常")


async def _get_login_user(db: AsyncSession, login_identifier: str) -> User | None:
    normalized = normalize_login_identifier(login_identifier)
    stmt = select(User).where(
        or_(User.username == normalized, User.email == normalized),
        User.deleted_at.is_(None),
    )
    return await db.scalar(stmt)


async def _get_system_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _ensure_telegram_action_enabled(
    db: AsyncSession,
    action: str,
) -> SystemSettings:
    row = await _get_system_settings(db)
    if not is_social_auth_action_enabled(row.social_auth_providers, "telegram", action):
        detail = "Telegram 绑定未开启" if action == "bind" else "Telegram 登录未开启"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return row


async def _get_required_binding(
    db: AsyncSession,
    user_id: UUID,
    *,
    touch: bool = True,
) -> TelegramUserBinding:
    binding = await get_active_binding_by_user_id(db, user_id)
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram 绑定不存在")
    if touch:
        await touch_binding(db, binding)
    return binding


@router.post("/bind", response_model=TelegramTokenResponse)
async def bind_telegram_user(
    payload: TelegramBindRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_telegram_action_enabled(db, "bind")
    await enforce_rate_limit(
        key=f"telegram:bind:{get_client_ip(request)}",
        limit=10,
        window_seconds=300,
        detail="请求过于频繁，请稍后重试",
    )

    user = await _get_login_user(db, payload.username)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    _ensure_telegram_user_allowed(user)

    binding_by_tg = await get_active_binding_by_telegram_user_id(db, payload.telegram_user_id)
    if binding_by_tg is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该 Telegram 用户已绑定其他账户",
        )
    binding_by_user = await get_active_binding_by_user_id(db, user.id)
    if binding_by_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前账户已有 Telegram 绑定",
        )

    now = utcnow()
    binding = TelegramUserBinding(
        user_id=user.id,
        telegram_user_id=payload.telegram_user_id,
        telegram_username=payload.telegram_username,
        telegram_first_name=payload.telegram_first_name,
        telegram_last_name=payload.telegram_last_name,
        telegram_language_code=payload.telegram_language_code,
        is_active=True,
        bound_at=now,
        last_interaction_at=now,
    )
    user.last_login_at = now
    db.add(user)
    db.add(binding)
    await get_or_create_notification_preferences(db, user.id)
    await db.commit()
    return build_telegram_token_response(user)


@router.post("/auth", response_model=TelegramTokenResponse)
async def auth_with_telegram(
    payload: TelegramAuthRequest,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_telegram_action_enabled(db, "login")
    await enforce_rate_limit(
        key=f"telegram:auth:{payload.telegram_user_id}",
        limit=20,
        window_seconds=300,
        detail="请求过于频繁，请稍后重试",
    )

    binding = await get_active_binding_by_telegram_user_id(db, payload.telegram_user_id)
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到绑定记录")

    user = await db.get(User, binding.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到绑定记录")
    _ensure_telegram_user_allowed(user)

    now = utcnow()
    await touch_binding(db, binding, timestamp=now)
    user.last_login_at = now
    db.add(user)
    await db.commit()
    return build_telegram_token_response(user)


@router.delete("/bind", response_model=TelegramMessageResponse)
async def unbind_telegram_user(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    binding = await get_active_binding_by_user_id(db, current_user["user_id"])
    if binding is not None:
        binding.is_active = False
        binding.unbound_at = utcnow()
        await touch_binding(db, binding, timestamp=binding.unbound_at)
        db.add(binding)

    await db.execute(
        delete(TelegramNotificationPreference).where(
            TelegramNotificationPreference.user_id == current_user["user_id"]
        )
    )
    await db.commit()
    return TelegramMessageResponse(message="unbind_success")


@router.get("/me", response_model=TelegramBindingOut)
async def get_telegram_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    binding = await _get_required_binding(db, current_user["user_id"], touch=True)
    await db.commit()
    return TelegramBindingOut.model_validate(binding)


@router.get("/notifications", response_model=TelegramNotificationListResponse)
async def list_notifications(
    unread_only: bool = Query(False),
    type: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_required_binding(db, current_user["user_id"], touch=True)

    filters = [TelegramNotification.user_id == current_user["user_id"]]
    if unread_only:
        filters.append(TelegramNotification.is_read.is_(False))
    if type:
        filters.append(TelegramNotification.type == type)

    total = int(
        await db.scalar(select(func.count()).select_from(TelegramNotification).where(*filters)) or 0
    )
    stmt = (
        select(TelegramNotification)
        .where(*filters)
        .order_by(TelegramNotification.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = (await db.execute(stmt)).scalars().all()
    await db.commit()
    pages = (total + size - 1) // size if total else 0
    return TelegramNotificationListResponse(
        items=[TelegramNotificationOut.model_validate(item) for item in items],
        total=total,
        page=page,
        size=size,
        pages=pages,
    )


@router.post(
    "/notifications/read-all",
    response_model=TelegramMarkAllReadResponse,
)
async def mark_all_notifications_read(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_required_binding(db, current_user["user_id"], touch=True)
    stmt = select(TelegramNotification).where(
        TelegramNotification.user_id == current_user["user_id"],
        TelegramNotification.is_read.is_(False),
    )
    notifications = (await db.execute(stmt)).scalars().all()
    for notification in notifications:
        notification.is_read = True
        db.add(notification)
    await db.commit()
    return TelegramMarkAllReadResponse(
        message="all_marked_as_read",
        count=len(notifications),
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=TelegramMessageResponse,
)
async def mark_notification_read(
    notification_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_required_binding(db, current_user["user_id"], touch=True)
    notification = await db.scalar(
        select(TelegramNotification).where(
            TelegramNotification.id == notification_id,
            TelegramNotification.user_id == current_user["user_id"],
        )
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知不存在")
    notification.is_read = True
    db.add(notification)
    await db.commit()
    return TelegramMessageResponse(message="marked_as_read")


@router.get(
    "/notifications/preferences",
    response_model=TelegramNotificationPreferenceOut,
)
async def get_notification_preferences(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_required_binding(db, current_user["user_id"], touch=True)
    preferences, _ = await get_or_create_notification_preferences(db, current_user["user_id"])
    await db.commit()
    return TelegramNotificationPreferenceOut.model_validate(preferences)


@router.put(
    "/notifications/preferences",
    response_model=TelegramNotificationPreferenceOut,
)
async def update_notification_preferences(
    payload: TelegramNotificationPreferenceUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_required_binding(db, current_user["user_id"], touch=True)
    preferences, _ = await get_or_create_notification_preferences(db, current_user["user_id"])
    preferences.subscription_expiry = payload.subscription_expiry
    preferences.payment = payload.payment
    preferences.vod = payload.vod
    preferences.ticket = payload.ticket
    db.add(preferences)
    await db.commit()
    return TelegramNotificationPreferenceOut.model_validate(preferences)


@router.post("/webapp/auth", response_model=TelegramTokenResponse)
async def auth_telegram_webapp(
    payload: TelegramWebAppAuthRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_telegram_action_enabled(db, "login")
    await enforce_rate_limit(
        key=f"telegram:webapp:{get_client_ip(request)}",
        limit=30,
        window_seconds=300,
        detail="请求过于频繁，请稍后重试",
    )

    bot_token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram WebApp 未配置",
        )

    webapp_user = verify_webapp_init_data(payload.init_data, bot_token)
    telegram_user_id = str(webapp_user.get("id") or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData 验证失败")

    binding = await get_active_binding_by_telegram_user_id(db, telegram_user_id)
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TG 用户未绑定账户")

    user = await db.get(User, binding.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TG 用户未绑定账户")
    _ensure_telegram_user_allowed(user)

    now = utcnow()
    await touch_binding(db, binding, timestamp=now)
    user.last_login_at = now
    db.add(user)
    await db.commit()
    return build_telegram_token_response(user)


@router.post("/widget/auth", response_model=TelegramTokenResponse)
async def auth_with_telegram_widget(
    payload: TelegramWidgetAuthRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_telegram_action_enabled(db, "login")
    await enforce_rate_limit(
        key=f"telegram:widget:auth:{get_client_ip(request)}",
        limit=30,
        window_seconds=300,
        detail="请求过于频繁，请稍后重试",
    )

    bot_token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram 登录未配置",
        )

    telegram_user = verify_login_widget_payload(payload.model_dump(mode="python"), bot_token)
    telegram_user_id = str(telegram_user.get("id") or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram 验证失败")

    binding = await get_active_binding_by_telegram_user_id(db, telegram_user_id)
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到绑定记录")

    user = await db.get(User, binding.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到绑定记录")
    _ensure_telegram_user_allowed(user)

    binding.telegram_username = payload.username
    binding.telegram_first_name = payload.first_name
    binding.telegram_last_name = payload.last_name
    await touch_binding(db, binding, timestamp=utcnow())
    user.last_login_at = utcnow()
    db.add(binding)
    db.add(user)
    await db.commit()
    return build_telegram_token_response(user)


@router.post("/widget/bind", response_model=TelegramBindingOut)
async def bind_telegram_widget_for_current_user(
    payload: TelegramWidgetAuthRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_telegram_action_enabled(db, "bind")

    bot_token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram 绑定未配置",
        )

    telegram_user = verify_login_widget_payload(payload.model_dump(mode="python"), bot_token)
    telegram_user_id = str(telegram_user.get("id") or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram 验证失败")

    binding_by_tg = await get_active_binding_by_telegram_user_id(db, telegram_user_id)
    binding_by_user = await get_active_binding_by_user_id(db, current_user["user_id"])

    if binding_by_tg and binding_by_tg.user_id != current_user["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该 Telegram 用户已绑定其他账户",
        )
    if binding_by_user and binding_by_user.telegram_user_id != telegram_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前账户已有 Telegram 绑定，请先解绑",
        )

    now = utcnow()
    binding = binding_by_user or binding_by_tg
    if binding is None:
        binding = TelegramUserBinding(
            user_id=current_user["user_id"],
            telegram_user_id=telegram_user_id,
            bound_at=now,
            is_active=True,
        )

    binding.user_id = current_user["user_id"]
    binding.telegram_user_id = telegram_user_id
    binding.telegram_username = payload.username
    binding.telegram_first_name = payload.first_name
    binding.telegram_last_name = payload.last_name
    binding.is_active = True
    binding.unbound_at = None
    if not binding.bound_at:
        binding.bound_at = now
    binding.last_interaction_at = now
    db.add(binding)
    await get_or_create_notification_preferences(db, current_user["user_id"])
    await db.commit()
    await db.refresh(binding)
    return TelegramBindingOut.model_validate(binding)
