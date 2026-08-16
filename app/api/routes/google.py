from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.social_account import SocialAccountBinding
from app.models.system_settings import SystemSettings
from app.models.user import User, UserStatus
from app.schemas.google import GoogleBindingOut, GoogleCredentialRequest
from app.schemas.user import TokenResponse
from app.services.google_auth import (
    GOOGLE_PROVIDER,
    build_social_token_response,
    get_active_social_binding_by_provider_user_id,
    get_active_social_binding_by_user_id,
    update_social_binding_from_google_claims,
    utcnow,
    verify_google_identity_token,
)
from app.services.social_auth import get_social_auth_provider_config, is_social_auth_action_enabled
from app.services.telegram import enforce_rate_limit, get_client_ip

router = APIRouter(prefix="/google", tags=["google"])


def _ensure_google_user_allowed(user: User) -> None:
    if user.deleted_at is not None or user.status == UserStatus.DELETED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号状态异常")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号状态异常")


async def _get_system_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _get_google_provider_config(db: AsyncSession, action: str) -> dict:
    row = await _get_system_settings(db)
    if not is_social_auth_action_enabled(row.social_auth_providers, GOOGLE_PROVIDER, action):
        detail = "Google 绑定未开启" if action == "bind" else "Google 登录未开启"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    config = get_social_auth_provider_config(row.social_auth_providers, GOOGLE_PROVIDER)
    if not str(config.get("client_id") or "").strip():
        detail = "Google 绑定未配置" if action == "bind" else "Google 登录未配置"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
    return config


@router.post("/auth", response_model=TokenResponse)
async def auth_with_google(
    payload: GoogleCredentialRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    config = await _get_google_provider_config(db, "login")
    await enforce_rate_limit(
        key=f"google:auth:{get_client_ip(request)}",
        limit=20,
        window_seconds=300,
        detail="请求过于频繁，请稍后重试",
    )

    claims = await verify_google_identity_token(
        payload.credential, str(config.get("client_id") or "")
    )
    binding = await get_active_social_binding_by_provider_user_id(
        db, GOOGLE_PROVIDER, str(claims.get("sub") or "").strip()
    )
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到绑定记录")

    user = await db.get(User, binding.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到绑定记录")
    _ensure_google_user_allowed(user)

    now = utcnow()
    update_social_binding_from_google_claims(binding, claims, timestamp=now)
    user.last_login_at = now
    db.add(binding)
    db.add(user)
    await db.commit()
    return build_social_token_response(user)


@router.post("/bind", response_model=GoogleBindingOut)
async def bind_google_account(
    payload: GoogleCredentialRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    config = await _get_google_provider_config(db, "bind")
    await enforce_rate_limit(
        key=f"google:bind:{get_client_ip(request)}",
        limit=20,
        window_seconds=300,
        detail="请求过于频繁，请稍后重试",
    )

    user = await db.get(User, current_user["user_id"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    _ensure_google_user_allowed(user)

    claims = await verify_google_identity_token(
        payload.credential, str(config.get("client_id") or "")
    )
    provider_user_id = str(claims.get("sub") or "").strip()
    binding_by_google = await get_active_social_binding_by_provider_user_id(
        db, GOOGLE_PROVIDER, provider_user_id
    )
    if binding_by_google is not None and binding_by_google.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该 Google 账号已绑定其他账户",
        )

    binding = await get_active_social_binding_by_user_id(db, user.id, GOOGLE_PROVIDER)
    if binding is None:
        binding = SocialAccountBinding(
            user_id=user.id, provider=GOOGLE_PROVIDER, provider_user_id=""
        )
    update_social_binding_from_google_claims(binding, claims)
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return GoogleBindingOut.model_validate(binding)


@router.delete("/bind")
async def unbind_google_account(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    binding = await get_active_social_binding_by_user_id(
        db, current_user["user_id"], GOOGLE_PROVIDER
    )
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Google 绑定不存在")
    now = utcnow()
    binding.is_active = False
    binding.unbound_at = now
    binding.last_interaction_at = now
    db.add(binding)
    await db.commit()
    return {"message": "unbind_success"}


@router.get("/me", response_model=GoogleBindingOut)
async def get_google_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    binding = await get_active_social_binding_by_user_id(
        db, current_user["user_id"], GOOGLE_PROVIDER
    )
    if binding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Google 绑定不存在")
    binding.last_interaction_at = utcnow()
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return GoogleBindingOut.model_validate(binding)
