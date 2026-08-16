from __future__ import annotations

from datetime import datetime

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_token
from app.models.social_account import SocialAccountBinding
from app.models.user import User
from app.schemas.user import TokenResponse

GOOGLE_PROVIDER = "google"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ALLOWED_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def utcnow() -> datetime:
    return datetime.utcnow()


def build_social_token_response(user: User) -> TokenResponse:
    access_token = create_token({"sub": str(user.id)}, token_type="access")
    refresh_token = create_token({"sub": str(user.id)}, token_type="refresh")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def verify_google_identity_token(credential: str, client_id: str) -> dict:
    if not credential.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google 凭据不能为空")
    if not client_id.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google 登录未配置",
        )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(GOOGLE_TOKENINFO_URL, params={"id_token": credential})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google 验证服务暂不可用",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google 验证失败")

    payload = response.json()
    issuer = str(payload.get("iss") or "").strip()
    audience = str(payload.get("aud") or "").strip()
    subject = str(payload.get("sub") or "").strip()
    if audience != client_id or issuer not in GOOGLE_ALLOWED_ISSUERS or not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google 验证失败")
    return payload


async def get_active_social_binding_by_provider_user_id(
    db: AsyncSession,
    provider: str,
    provider_user_id: str,
) -> SocialAccountBinding | None:
    stmt = select(SocialAccountBinding).where(
        SocialAccountBinding.provider == provider,
        SocialAccountBinding.provider_user_id == provider_user_id,
        SocialAccountBinding.is_active.is_(True),
    )
    return await db.scalar(stmt)


async def get_active_social_binding_by_user_id(
    db: AsyncSession,
    user_id,
    provider: str,
) -> SocialAccountBinding | None:
    stmt = select(SocialAccountBinding).where(
        SocialAccountBinding.user_id == user_id,
        SocialAccountBinding.provider == provider,
        SocialAccountBinding.is_active.is_(True),
    )
    return await db.scalar(stmt)


def update_social_binding_from_google_claims(
    binding: SocialAccountBinding,
    claims: dict,
    *,
    timestamp: datetime | None = None,
) -> SocialAccountBinding:
    now = timestamp or utcnow()
    binding.provider = GOOGLE_PROVIDER
    binding.provider_user_id = str(claims.get("sub") or "").strip()
    binding.provider_email = str(claims.get("email") or "").strip() or None
    binding.provider_name = str(claims.get("name") or "").strip() or None
    binding.provider_avatar_url = str(claims.get("picture") or "").strip() or None
    binding.is_active = True
    binding.bound_at = binding.bound_at or now
    binding.unbound_at = None
    binding.last_interaction_at = now
    return binding
