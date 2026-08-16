import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.system_settings import SystemSettings


@pytest.mark.asyncio
async def test_public_settings_exposes_email_verification(async_client):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.site_name = "Movary"
        row.site_url = "https://movary.example.com"
        row.enabled_languages = ["en-US"]
        row.default_language = "en-US"
        row.email_verification_enabled = True
        row.invite_registration_enabled = True
        row.social_auth_providers = {
            "telegram": {
                "enabled": True,
                "allow_login": False,
                "allow_bind": False,
                "bot_username": "@movary_bot",
                "bot_display_name": "Telegram 登录",
                "login_mode": "widget",
            },
            "google": {
                "enabled": True,
                "allow_login": False,
                "allow_bind": False,
                "client_id": "google-client-id",
                "client_secret": "super-secret",
                "redirect_uri": "https://movary.example.com/auth/google/callback",
                "display_name": "Google 登录",
            },
        }
        session.add(row)
        await session.commit()

    resp = await async_client.get("/api/v1/settings/public")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["site_url"] == "https://movary.example.com"
    assert data["enabled_languages"] == ["en-US"]
    assert data["default_language"] == "en-US"
    assert data["email_verification_enabled"] is True
    assert data["invite_registration_enabled"] is True
    assert data["social_auth_providers"]["telegram"]["bot_username"] == "movary_bot"
    assert data["social_auth_providers"]["telegram"]["allow_login"] is True
    assert data["social_auth_providers"]["telegram"]["allow_bind"] is True
    assert data["social_auth_providers"]["google"]["client_id"] == "google-client-id"
    assert data["social_auth_providers"]["google"]["client_secret"] is None
    assert data["social_auth_providers"]["google"]["allow_login"] is True
    assert data["social_auth_providers"]["google"]["allow_bind"] is True


@pytest.mark.asyncio
async def test_public_settings_exposes_default_client_apps(async_client):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.emby_client_apps = None
        session.add(row)
        await session.commit()

    resp = await async_client.get("/api/v1/settings/public")
    assert resp.status_code == 200
    items = resp.json()["data"]["emby_client_apps"]
    names = [item["name"] for item in items]
    assert names == ["forward", "小幻影视"]
    assert len(items) == 2
    assert items[0]["platform"] == "iOS"
    assert items[0]["is_default"] is True
    assert items[-1]["platform"] == "Windows"
