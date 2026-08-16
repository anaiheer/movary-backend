from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import uuid
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import create_token, hash_password
from app.db.session import AsyncSessionLocal
from app.models.telegram import (
    TelegramNotification,
    TelegramNotificationPreference,
    TelegramUserBinding,
)
from app.models.system_settings import SystemSettings
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import User


async def _create_user(*, username_prefix: str = "tg_user", password: str = "Test123456") -> User:
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


async def _create_binding(
    user_id,
    *,
    telegram_user_id: str = "123456789",
    telegram_username: str = "telegram_user",
) -> TelegramUserBinding:
    async with AsyncSessionLocal() as session:
        binding = TelegramUserBinding(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            is_active=True,
        )
        session.add(binding)
        session.add(TelegramNotificationPreference(user_id=user_id))
        await session.commit()
        await session.refresh(binding)
        return binding


async def _enable_telegram_social_auth(*, enabled: bool = True) -> None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if row is None:
            row = SystemSettings()
        row.social_auth_providers = {
            "telegram": {
                "enabled": enabled,
                "allow_login": False,
                "allow_bind": False,
                "bot_username": "movary_test_bot",
                "bot_display_name": "Telegram",
                "login_mode": "widget",
            },
            "google": {
                "enabled": False,
                "allow_login": False,
                "allow_bind": False,
                "client_id": None,
                "client_secret": None,
                "redirect_uri": None,
                "display_name": "Google",
            },
        }
        session.add(row)
        await session.commit()


def _build_init_data(bot_token: str, telegram_user_id: str) -> str:
    user_payload = {
        "id": int(telegram_user_id),
        "first_name": "Tele",
        "last_name": "Gram",
        "username": "telegram_user",
        "language_code": "zh-hans",
    }
    payload = {
        "query_id": "AAHdF5eAAAAAA",
        "user": json.dumps(user_payload, separators=(",", ":")),
        "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    payload["hash"] = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(payload)


@pytest.mark.asyncio
async def test_telegram_bind_auth_and_unbind_flow(async_client):
    user = await _create_user()
    await _enable_telegram_social_auth()

    bind_resp = await async_client.post(
        "/api/v1/telegram/bind",
        json={
            "telegram_user_id": "123456789",
            "telegram_username": "movary_tg",
            "telegram_first_name": "John",
            "telegram_last_name": "Doe",
            "telegram_language_code": "zh",
            "username": user.username,
            "password": "Test123456",
        },
    )
    assert bind_resp.status_code == 200
    bind_data = bind_resp.json()
    assert bind_data["access_token"]
    assert bind_data["refresh_token"]
    assert bind_data["user"]["id"] == str(user.id)

    auth_resp = await async_client.post(
        "/api/v1/telegram/auth",
        json={"telegram_user_id": "123456789"},
    )
    assert auth_resp.status_code == 200
    auth_data = auth_resp.json()
    assert auth_data["user"]["id"] == str(user.id)

    headers = {"Authorization": f"Bearer {bind_data['access_token']}"}
    me_resp = await async_client.get("/api/v1/telegram/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["telegram_username"] == "movary_tg"
    assert me_resp.json()["last_interaction_at"] is not None

    prefs_resp = await async_client.get(
        "/api/v1/telegram/notifications/preferences", headers=headers
    )
    assert prefs_resp.status_code == 200
    assert prefs_resp.json() == {
        "subscription_expiry": True,
        "payment": True,
        "vod": True,
        "ticket": True,
    }

    update_resp = await async_client.put(
        "/api/v1/telegram/notifications/preferences",
        headers=headers,
        json={
            "subscription_expiry": True,
            "payment": False,
            "vod": True,
            "ticket": False,
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["payment"] is False
    assert update_resp.json()["ticket"] is False

    unbind_resp = await async_client.delete("/api/v1/telegram/bind", headers=headers)
    assert unbind_resp.status_code == 200
    assert unbind_resp.json() == {"message": "unbind_success"}

    async with AsyncSessionLocal() as session:
        binding = await session.scalar(
            select(TelegramUserBinding).where(TelegramUserBinding.user_id == user.id)
        )
        prefs = await session.scalar(
            select(TelegramNotificationPreference).where(
                TelegramNotificationPreference.user_id == user.id
            )
        )
        assert binding is not None
        assert binding.is_active is False
        assert binding.unbound_at is not None
        assert prefs is None


@pytest.mark.asyncio
async def test_telegram_notifications_read_flow(async_client):
    user = await _create_user(username_prefix="tg_notify")
    await _create_binding(user.id, telegram_user_id="987654321")
    await _enable_telegram_social_auth()

    async with AsyncSessionLocal() as session:
        first = TelegramNotification(
            user_id=user.id,
            type="payment_success",
            title="支付成功",
            content="订单A支付成功",
        )
        second = TelegramNotification(
            user_id=user.id,
            type="ticket_reply",
            title="工单收到回复",
            content="工单有新回复",
        )
        session.add_all([first, second])
        await session.commit()
        await session.refresh(first)
        second_id = second.id

    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    list_resp = await async_client.get("/api/v1/telegram/notifications", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 2

    mark_one_resp = await async_client.post(
        f"/api/v1/telegram/notifications/{second_id}/read",
        headers=headers,
    )
    assert mark_one_resp.status_code == 200
    assert mark_one_resp.json() == {"message": "marked_as_read"}

    mark_all_resp = await async_client.post(
        "/api/v1/telegram/notifications/read-all",
        headers=headers,
    )
    assert mark_all_resp.status_code == 200
    assert mark_all_resp.json()["message"] == "all_marked_as_read"
    assert mark_all_resp.json()["count"] == 1

    unread_resp = await async_client.get(
        "/api/v1/telegram/notifications",
        headers=headers,
        params={"unread_only": "true"},
    )
    assert unread_resp.status_code == 200
    assert unread_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_telegram_webapp_auth(async_client, monkeypatch):
    user = await _create_user(username_prefix="tg_webapp")
    await _create_binding(user.id, telegram_user_id="55667788")
    await _enable_telegram_social_auth()
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "bot-secret")

    resp = await async_client.post(
        "/api/v1/telegram/webapp/auth",
        json={"init_data": _build_init_data("bot-secret", "55667788")},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["id"] == str(user.id)


@pytest.mark.asyncio
async def test_admin_ticket_reply_creates_telegram_notification(async_client, admin_token):
    user = await _create_user(username_prefix="tg_ticket")
    await _create_binding(user.id, telegram_user_id="11223344")

    async with AsyncSessionLocal() as session:
        ticket = Ticket(
            user_id=user.id,
            subject="TG ticket",
            status=TicketStatus.OPEN,
            priority=TicketPriority.MEDIUM,
            last_reply_at=datetime.utcnow() - timedelta(hours=1),
        )
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        ticket_id = ticket.id

    resp = await async_client.post(
        f"/api/v1/admin/tickets/{ticket_id}/messages",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"content": "管理员已回复"},
    )
    assert resp.status_code == 200

    async with AsyncSessionLocal() as session:
        notification = await session.scalar(
            select(TelegramNotification)
            .where(TelegramNotification.user_id == user.id)
            .order_by(TelegramNotification.created_at.desc())
        )
        assert notification is not None
        assert notification.type == "ticket_reply"
        assert notification.reference_id == str(ticket_id)
