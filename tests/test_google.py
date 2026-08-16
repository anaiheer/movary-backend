from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_token, hash_password
from app.db.session import AsyncSessionLocal
from app.models.social_account import SocialAccountBinding
from app.models.system_settings import SystemSettings
from app.models.user import User


async def _create_user(
    *,
    username_prefix: str = "google_user",
    password: str = "Test123456",
) -> User:
    async with AsyncSessionLocal() as session:
        username = f"{username_prefix}_{uuid.uuid4().hex[:8]}"
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password(password),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _enable_google_social_auth(*, enabled: bool = True) -> None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if row is None:
            row = SystemSettings()
        row.social_auth_providers = {
            "telegram": {
                "enabled": False,
                "allow_login": False,
                "allow_bind": False,
                "bot_username": None,
                "bot_display_name": "Telegram",
                "login_mode": "widget",
            },
            "google": {
                "enabled": enabled,
                "allow_login": False,
                "allow_bind": False,
                "client_id": "google-client-id",
                "client_secret": "google-client-secret",
                "redirect_uri": "https://movary.example.com/auth/google/callback",
                "display_name": "Google",
            },
        }
        session.add(row)
        await session.commit()


def _claims(
    *,
    sub: str = "google-user-123",
    email: str = "google-user@example.com",
    name: str = "Google User",
) -> dict:
    return {
        "sub": sub,
        "email": email,
        "name": name,
        "picture": "https://example.com/avatar.png",
        "aud": "google-client-id",
        "iss": "https://accounts.google.com",
    }


@pytest.mark.asyncio
async def test_google_bind_auth_and_unbind_flow(async_client, monkeypatch):
    from app.api.routes import google as google_route

    user = await _create_user()
    await _enable_google_social_auth()

    async def fake_verify_google_identity_token(credential, client_id):
        return _claims()

    monkeypatch.setattr(
        google_route,
        "verify_google_identity_token",
        fake_verify_google_identity_token,
    )

    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    bind_resp = await async_client.post(
        "/api/v1/google/bind",
        headers=headers,
        json={"credential": "mock-google-credential"},
    )
    assert bind_resp.status_code == 200
    assert bind_resp.json()["provider"] == "google"
    assert bind_resp.json()["provider_email"] == "google-user@example.com"

    auth_resp = await async_client.post(
        "/api/v1/google/auth",
        json={"credential": "mock-google-credential"},
    )
    assert auth_resp.status_code == 200
    assert auth_resp.json()["access_token"]
    assert auth_resp.json()["refresh_token"]

    me_resp = await async_client.get("/api/v1/google/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["provider_user_id"] == "google-user-123"

    unbind_resp = await async_client.delete("/api/v1/google/bind", headers=headers)
    assert unbind_resp.status_code == 200
    assert unbind_resp.json() == {"message": "unbind_success"}

    async with AsyncSessionLocal() as session:
        binding = await session.scalar(
            select(SocialAccountBinding).where(
                SocialAccountBinding.user_id == user.id,
                SocialAccountBinding.provider == "google",
            )
        )
        assert binding is not None
        assert binding.is_active is False
        assert binding.unbound_at is not None


@pytest.mark.asyncio
async def test_google_auth_requires_existing_binding(async_client, monkeypatch):
    from app.api.routes import google as google_route

    await _enable_google_social_auth()

    async def fake_verify_google_identity_token(credential, client_id):
        return _claims(sub="missing-binding")

    monkeypatch.setattr(
        google_route,
        "verify_google_identity_token",
        fake_verify_google_identity_token,
    )

    resp = await async_client.post(
        "/api/v1/google/auth",
        json={"credential": "mock-google-credential"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "未找到绑定记录"
