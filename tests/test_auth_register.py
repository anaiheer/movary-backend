import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.system_settings import SystemSettings
from app.models.user import User


async def ensure_settings(
    email_verification_enabled: bool = True,
    site_url: str | None = None,
    invite_registration_enabled: bool = False,
):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.email_verification_enabled = email_verification_enabled
        row.invite_registration_enabled = invite_registration_enabled
        row.site_url = site_url
        row.smtp_host = "smtp.test.com"
        row.smtp_port = 587
        row.smtp_user = "tester"
        row.smtp_password = "secret"
        row.smtp_from = "noreply@test.com"
        row.smtp_use_tls = True
        row.smtp_use_ssl = False
        session.add(row)
        await session.commit()
        await session.refresh(row)


@pytest.mark.asyncio
async def test_register_requires_email_when_verification_enabled(async_client):
    await ensure_settings(email_verification_enabled=True)

    payload = {
        "username": "reg_no_email",
        "password": "Test123456",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "必须填写邮箱"


@pytest.mark.asyncio
async def test_register_requires_invite_when_invite_registration_enabled(async_client):
    await ensure_settings(email_verification_enabled=False, invite_registration_enabled=True)

    payload = {
        "email": "invite_required@example.com",
        "username": "invite_required",
        "password": "Test123456",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "必须填写邀请码"


@pytest.mark.asyncio
async def test_register_and_verify_email(async_client):
    await ensure_settings(email_verification_enabled=True)

    payload = {
        "email": "verify_user@example.com",
        "username": "verify_user",
        "password": "Test123456",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["email_verified"] is False

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == "verify_user"))).scalar()
        assert user
        token = user.email_verification_token

    verify = await async_client.get("/api/v1/auth/verify-email", params={"token": token})
    assert verify.status_code == 200
    assert verify.json()["success"] is True

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == "verify_user"))).scalar()
        assert user.email_verified is True


@pytest.mark.asyncio
async def test_register_uses_site_url_for_verification_link(async_client, monkeypatch):
    await ensure_settings(email_verification_enabled=True, site_url="https://movary.example.com")

    captured: dict[str, str] = {}

    async def fake_send_email(to_email, subject, html, text, _smtp_config):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["html"] = html
        captured["text"] = text

    monkeypatch.setattr("app.api.routes.auth.send_email", fake_send_email)

    payload = {
        "email": "site_url_verify@example.com",
        "username": "site_url_verify",
        "password": "Test123456",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 200

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == "site_url_verify"))
        ).scalar()
        assert user
        expected_url = (
            f"https://movary.example.com/verify-email?token={user.email_verification_token}"
        )

    assert captured["to_email"] == "site_url_verify@example.com"
    assert expected_url in captured["html"]
    assert expected_url in captured["text"]


@pytest.mark.asyncio
async def test_register_rolls_back_when_verification_email_send_fails(async_client, monkeypatch):
    await ensure_settings(email_verification_enabled=True)

    async def failing_send_email(*_args, **_kwargs):
        raise RuntimeError("smtp unavailable")

    monkeypatch.setattr("app.api.routes.auth.send_email", failing_send_email)

    payload = {
        "email": "rollback_verify@example.com",
        "username": "rollback_verify",
        "password": "Test123456",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "验证邮件发送失败，请稍后重试"

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == "rollback_verify"))
        ).scalar()
        assert user is None

    sent: dict[str, str] = {}

    async def success_send_email(to_email, subject, html, text, _smtp_config):
        sent["to_email"] = to_email
        sent["subject"] = subject
        sent["html"] = html
        sent["text"] = text

    monkeypatch.setattr("app.api.routes.auth.send_email", success_send_email)
    retry = await async_client.post("/api/v1/auth/register", json=payload)
    assert retry.status_code == 200, retry.text
    assert sent["to_email"] == "rollback_verify@example.com"


@pytest.mark.asyncio
async def test_register_and_login_with_two_char_chinese_username(async_client):
    await ensure_settings(email_verification_enabled=False)

    payload = {
        "email": "zhangsan@example.com",
        "username": "张三",
        "password": "Test123456",
    }
    register = await async_client.post("/api/v1/auth/register", json=payload)
    assert register.status_code == 200, register.text
    assert register.json()["username"] == "张三"

    login = await async_client.post(
        "/api/v1/auth/login",
        json={"username": "张三", "password": "Test123456"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]


@pytest.mark.asyncio
async def test_register_rejects_invalid_username_with_clear_message(async_client):
    await ensure_settings(email_verification_enabled=False)

    payload = {
        "email": "invalid_username@example.com",
        "username": "张 三",
        "password": "Test123456",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(item["loc"][-1] == "username" for item in detail)
    assert any("用户名仅支持中文、字母、数字、下划线、中划线和点" in item["msg"] for item in detail)


@pytest.mark.asyncio
async def test_register_rejects_weak_password(async_client):
    await ensure_settings(email_verification_enabled=False)

    payload = {
        "email": "weak-password@example.com",
        "username": "weak_password_user",
        "password": "12345678",
    }
    resp = await async_client.post("/api/v1/auth/register", json=payload)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any(item["loc"][-1] == "password" for item in detail)
    assert any("密码至少 8 位，且需同时包含字母和数字" in item["msg"] for item in detail)
