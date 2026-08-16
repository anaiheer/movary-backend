import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.system_settings import SystemSettings


@pytest.mark.asyncio
async def test_get_settings(async_client, admin_token):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.enabled_languages = ["zh-CN", "en-US"]
        row.default_language = "zh-CN"
        row.emby_client_apps = None
        row.social_auth_providers = None
        session.add(row)
        await session.commit()

    resp = await async_client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "data" in data
    assert "default_theme" in data["data"]
    assert "tmdb_warmup_enabled" in data["data"]
    assert "tmdb_warmup_interval_seconds" in data["data"]
    assert "invite_registration_enabled" in data["data"]
    assert data["data"]["enabled_languages"] == ["zh-CN", "en-US"]
    assert data["data"]["default_language"] == "zh-CN"
    template_map = {item["key"]: item for item in data["data"]["email_templates"]}
    assert "invitation" in template_map
    assert "site_logo" in template_map["invitation"]["variables"]
    assert "site_logo_url" in template_map["invitation"]["variables"]
    client_app_names = [item["name"] for item in data["data"]["emby_client_apps"]]
    assert client_app_names == ["forward", "小幻影视"]
    assert data["data"]["social_auth_providers"]["telegram"]["enabled"] is False
    assert data["data"]["social_auth_providers"]["google"]["display_name"] == "Google"


@pytest.mark.asyncio
async def test_get_settings_replaces_legacy_placeholder_client_apps(async_client, admin_token):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        assert row is not None
        row.emby_client_apps = [
            {
                "id": "infuse-ios",
                "name": "Infuse",
                "icon": "I",
                "platform": "iOS",
                "scheme_template": (
                    "infuse://x-callback-url/add?"
                    "url={{server_url_encoded}}&username={{username_encoded}}"
                    "&password={{password_encoded}}"
                ),
                "enabled": True,
                "is_default": False,
                "sort_order": 0,
            },
            {
                "id": "forward-ios",
                "name": "forward",
                "icon": "F",
                "platform": "iOS",
                "scheme_template": (
                    "forward://import?"
                    "url={{server_url_encoded}}&username={{username_encoded}}"
                    "&password={{password_encoded}}"
                ),
                "enabled": True,
                "is_default": True,
                "sort_order": 1,
            },
            {
                "id": "xiaohuan-windows",
                "name": "小幻影视",
                "icon": "X",
                "platform": "Windows",
                "scheme_template": (
                    "xiaohuan://add-server?"
                    "url={{server_url_encoded}}&username={{username_encoded}}"
                    "&password={{password_encoded}}"
                ),
                "enabled": True,
                "is_default": False,
                "sort_order": 2,
            },
        ]
        session.add(row)
        await session.commit()

    resp = await async_client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["emby_client_apps"]
    assert [item["name"] for item in items] == ["forward", "小幻影视"]
    assert items[0]["scheme_template"].startswith("forward://import?type=emby")
    assert items[1]["scheme_template"].startswith("rodelplayer://import?type=emby")
    assert items[0]["is_default"] is True


@pytest.mark.asyncio
async def test_update_settings(async_client, admin_token, monkeypatch):
    from app.api.routes import admin_settings as admin_settings_route
    from app.db.session import AsyncSessionLocal
    from app.models.system_task import SystemTask
    from sqlalchemy import select

    calls = {"reset": 0, "schedule": 0}

    async def fake_reset_tmdb_caches(db, *, rebuild_defaults):
        calls["reset"] += 1
        return 5, {"tmdb_images": 2, "emby_images": 1}, 9

    def fake_refresh_schedule(task):
        calls["schedule"] += 1

    monkeypatch.setattr(admin_settings_route, "_reset_tmdb_caches", fake_reset_tmdb_caches)
    monkeypatch.setattr(admin_settings_route, "refresh_schedule", fake_refresh_schedule)

    payload = {
        "default_theme": "light",
        "email_verification_enabled": True,
        "invite_registration_enabled": True,
        "enabled_languages": ["en-US", "zh-CN", "zh-CN", "unknown"],
        "default_language": "en-US",
        "smtp_host": "smtp.test.com",
        "smtp_port": 465,
        "smtp_user": "tester",
        "smtp_password": "secret",
        "smtp_from": "noreply@test.com",
        "smtp_use_tls": False,
        "smtp_use_ssl": True,
        "epay_enabled": True,
        "epay_merchant_id": "m123",
        "epay_key": "k123",
        "epay_gateway": "https://pay.example.com",
        "epay_notify_url": "https://app.example.com/api/pay/notify",
        "epay_return_url": "https://app.example.com/pay/return",
        "social_auth_providers": {
            "telegram": {
                "enabled": True,
                "allow_login": False,
                "allow_bind": False,
                "bot_username": "@movary_bot",
                "bot_display_name": "TG 登录",
                "login_mode": "widget",
            },
            "google": {
                "enabled": True,
                "allow_login": False,
                "allow_bind": False,
                "client_id": "google-client-id",
                "client_secret": "google-client-secret",
                "redirect_uri": "https://app.example.com/auth/google/callback",
                "display_name": "Google 登录",
            },
        },
        "tmdb_base_url": "https://api.themoviedb.org/3",
        "tmdb_warmup_enabled": False,
        "tmdb_warmup_interval_seconds": 1800,
    }
    resp = await async_client.put(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["default_theme"] == "light"
    assert data["data"]["email_verification_enabled"] is True
    assert data["data"]["invite_registration_enabled"] is True
    assert data["data"]["enabled_languages"] == ["en-US", "zh-CN"]
    assert data["data"]["default_language"] == "en-US"
    assert data["data"]["smtp_host"] == "smtp.test.com"
    assert data["data"]["epay_enabled"] is True
    assert data["data"]["social_auth_providers"]["telegram"]["bot_username"] == "movary_bot"
    assert data["data"]["social_auth_providers"]["telegram"]["allow_bind"] is True
    assert data["data"]["social_auth_providers"]["telegram"]["allow_login"] is True
    assert data["data"]["social_auth_providers"]["google"]["allow_login"] is True
    assert data["data"]["social_auth_providers"]["google"]["allow_bind"] is True
    assert (
        data["data"]["social_auth_providers"]["google"]["client_secret"] == "google-client-secret"
    )
    assert data["data"]["tmdb_base_url"] == "https://api.themoviedb.org/3"
    assert data["data"]["tmdb_warmup_enabled"] is False
    assert data["data"]["tmdb_warmup_interval_seconds"] == 1800
    assert calls == {"reset": 1, "schedule": 1}

    async with AsyncSessionLocal() as session:
        task = await session.scalar(select(SystemTask).where(SystemTask.key == "tmdb_sync"))
        assert task is not None
        assert task.enabled is False
        assert task.interval_seconds == 1800


@pytest.mark.asyncio
async def test_refresh_cache(async_client, admin_token, monkeypatch):
    from app.api.routes import admin_settings as admin_settings_route

    calls = {"reset": 0}

    async def fake_reset_tmdb_caches(db, *, rebuild_defaults):
        calls["reset"] += 1
        assert rebuild_defaults is True
        return 7, {"tmdb_images": 3, "emby_images": 4}, 11

    monkeypatch.setattr(admin_settings_route, "_reset_tmdb_caches", fake_reset_tmdb_caches)

    resp = await async_client.post(
        "/api/v1/admin/settings/cache/refresh",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["data_deleted"] == 7
    assert data["data"]["image_deleted"] == {"tmdb_images": 3, "emby_images": 4}
    assert data["data"]["reloaded"] == 11
    assert calls == {"reset": 1}
