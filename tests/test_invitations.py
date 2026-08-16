from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import create_token, hash_password
from app.db.session import AsyncSessionLocal
from app.models.subscription import BillingCycle, Plan, PlanServerAllocationStrategy
from app.models.system_settings import SystemSettings
from app.models.user import User, UserRole


async def _create_user_token(*, role: UserRole = UserRole.USER):
    async with AsyncSessionLocal() as session:
        suffix = uuid4().hex[:8]
        username = f"user_{suffix}"
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("Test123456"),
            role=role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = create_token({"sub": str(user.id)}, token_type="access")
        return token, user


async def _create_plan():
    async with AsyncSessionLocal() as session:
        suffix = uuid4().hex[:8]
        plan = Plan(
            name=f"Invite Plan {suffix}",
            group_key=f"invite-group-{suffix}",
            group_name="Invite Group",
            tier_level=1,
            duration_days=30,
            price=Decimal("99.00"),
            default_billing_cycle=BillingCycle.MONTHLY,
            server_allocation_strategy=PlanServerAllocationStrategy.ALL,
            vod_movie_times=0,
            vod_tv_times=0,
            is_visible=True,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        return plan


async def _ensure_email_settings(
    *,
    site_name: str = "Movary",
    site_url: str = "https://movary.example.com",
    site_logo_url: str | None = "/uploads/site-logo.png",
):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.site_name = site_name
        row.site_url = site_url
        row.site_logo_url = site_logo_url
        row.smtp_host = "smtp.test.com"
        row.smtp_port = 587
        row.smtp_user = "tester"
        row.smtp_password = "secret"
        row.smtp_from = "Movary <noreply@example.com>"
        row.smtp_use_tls = True
        row.smtp_use_ssl = False
        session.add(row)
        await session.commit()
        await session.refresh(row)


@pytest.mark.asyncio
async def test_user_invitation_create_and_list_only_own_records(async_client):
    first_token, first_user = await _create_user_token()
    second_token, _ = await _create_user_token()
    first_headers = {"Authorization": f"Bearer {first_token}"}
    second_headers = {"Authorization": f"Bearer {second_token}"}

    create_first = await async_client.post(
        "/api/v1/admin/invitations",
        headers=first_headers,
        json={
            "invitee_email": "first-invitee@example.com",
            "plan_id": str(uuid4()),
            "initial_balance": "88.50",
        },
    )
    assert create_first.status_code == 200
    first_payload = create_first.json()["data"]
    assert f"invite={first_user.username}" in first_payload["invite_url"]
    assert len(first_payload["token"]) == 8

    create_second = await async_client.post(
        "/api/v1/admin/invitations",
        headers=second_headers,
        json={"invitee_email": "second-invitee@example.com"},
    )
    assert create_second.status_code == 200

    list_first = await async_client.get("/api/v1/admin/invitations", headers=first_headers)
    assert list_first.status_code == 200
    payload = list_first.json()["data"]
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["invitee_email"] == "first-invitee@example.com"
    assert item["inviter"]["username"] == first_user.username
    assert item["invite_url"].endswith(f"invite={first_user.username}&token={item['token']}")
    assert item["plan"] is None
    assert item["initial_balance"] is None
    assert item["status"] == "PENDING"
    assert len(item["token"]) == 8
    assert payload["accepted_count"] == 0


@pytest.mark.asyncio
async def test_admin_invitation_create_keeps_plan_and_balance(async_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    plan = await _create_plan()

    response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={
            "invitee_email": "admin-invitee@example.com",
            "plan_id": str(plan.id),
            "initial_balance": "12.50",
        },
    )
    assert response.status_code == 200
    assert len(response.json()["data"]["token"]) == 8

    list_response = await async_client.get("/api/v1/admin/invitations", headers=headers)
    assert list_response.status_code == 200
    item = next(
        entry
        for entry in list_response.json()["data"]["items"]
        if entry["invitee_email"] == "admin-invitee@example.com"
    )
    assert item["plan"] == {"id": str(plan.id), "name": plan.name}
    assert item["initial_balance"] in {"12.5", "12.50"}


@pytest.mark.asyncio
async def test_invitation_create_sends_email_with_absolute_links(async_client, monkeypatch):
    token, user = await _create_user_token()
    headers = {"Authorization": f"Bearer {token}"}
    await _ensure_email_settings()

    captured: dict[str, str] = {}

    async def fake_send_email(to_email, subject, html, text, _smtp_config):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["html"] = html
        captured["text"] = text

    monkeypatch.setattr("app.api.routes.admin_invitations.send_email", fake_send_email)

    response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"invitee_email": "mail-invitee@example.com"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    invite_token = payload["data"]["token"]
    expected_url = (
        f"https://movary.example.com/register?invite={user.username}&token={invite_token}"
    )

    assert payload["message"] == "邀请码已创建，邀请邮件已发送"
    assert captured["to_email"] == "mail-invitee@example.com"
    assert expected_url in captured["html"]
    assert expected_url in captured["text"]
    assert "https://movary.example.com/uploads/site-logo.png" in captured["html"]
    assert user.username in captured["subject"]


@pytest.mark.asyncio
async def test_invitation_create_keeps_record_when_email_send_fails(async_client, monkeypatch):
    token, _ = await _create_user_token()
    headers = {"Authorization": f"Bearer {token}"}
    await _ensure_email_settings()

    async def failing_send_email(*_args, **_kwargs):
        raise RuntimeError("smtp unavailable")

    monkeypatch.setattr("app.api.routes.admin_invitations.send_email", failing_send_email)

    response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"invitee_email": "mail-failed@example.com"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "邀请码已创建，但邀请邮件发送失败，请检查 SMTP 配置"

    list_response = await async_client.get("/api/v1/admin/invitations", headers=headers)
    assert list_response.status_code == 200
    assert any(
        item["invitee_email"] == "mail-failed@example.com"
        for item in list_response.json()["data"]["items"]
    )


@pytest.mark.asyncio
async def test_user_invitation_can_be_canceled(async_client):
    token, _ = await _create_user_token()
    headers = {"Authorization": f"Bearer {token}"}

    create_response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"invitee_email": "cancel-me@example.com"},
    )
    assert create_response.status_code == 200
    invitation_id = create_response.json()["data"]["id"]

    cancel_response = await async_client.post(
        f"/api/v1/admin/invitations/{invitation_id}/cancel",
        headers=headers,
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["data"]["status"] == "CANCELED"

    list_response = await async_client.get("/api/v1/admin/invitations", headers=headers)
    assert list_response.status_code == 200
    item = next(
        entry for entry in list_response.json()["data"]["items"] if entry["id"] == invitation_id
    )
    assert item["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_register_accepts_short_invite_code(async_client):
    inviter_token, inviter_user = await _create_user_token()
    headers = {"Authorization": f"Bearer {inviter_token}"}

    create_response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"invitee_email": "joined-by-code@example.com"},
    )
    assert create_response.status_code == 200
    invite_code = create_response.json()["data"]["token"]

    register_response = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": "joined-by-code@example.com",
            "username": f"joined_{uuid4().hex[:8]}",
            "phone": None,
            "password": "Test123456",
            "invite_token": invite_code,
        },
    )
    assert register_response.status_code == 200
    assert register_response.json()["username"].startswith("joined_")

    list_response = await async_client.get("/api/v1/admin/invitations", headers=headers)
    assert list_response.status_code == 200
    item = next(
        entry for entry in list_response.json()["data"]["items"] if entry["token"] == invite_code
    )
    assert item["status"] == "ACCEPTED"
    assert item["inviter"]["username"] == inviter_user.username
    assert list_response.json()["data"]["accepted_count"] >= 1


@pytest.mark.asyncio
async def test_user_batch_delete_invitations_skips_accepted(async_client):
    token, _ = await _create_user_token()
    headers = {"Authorization": f"Bearer {token}"}

    pending_response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"invitee_email": "pending-delete@example.com"},
    )
    assert pending_response.status_code == 200
    pending_invitation = pending_response.json()["data"]

    accepted_response = await async_client.post(
        "/api/v1/admin/invitations",
        headers=headers,
        json={"invitee_email": "accepted-keep@example.com"},
    )
    assert accepted_response.status_code == 200
    accepted_invitation = accepted_response.json()["data"]

    register_response = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": "accepted-keep@example.com",
            "username": f"accepted_{uuid4().hex[:8]}",
            "phone": None,
            "password": "Test123456",
            "invite_token": accepted_invitation["token"],
        },
    )
    assert register_response.status_code == 200

    missing_id = str(uuid4())
    delete_response = await async_client.post(
        "/api/v1/admin/invitations/batch-delete",
        headers=headers,
        json={"ids": [pending_invitation["id"], accepted_invitation["id"], missing_id]},
    )
    assert delete_response.status_code == 200
    payload = delete_response.json()["data"]
    assert payload == {
        "requested": 3,
        "deleted": 1,
        "missing": 1,
        "missing_ids": [missing_id],
        "failed_ids": [accepted_invitation["id"]],
    }

    list_response = await async_client.get("/api/v1/admin/invitations", headers=headers)
    assert list_response.status_code == 200
    items = list_response.json()["data"]["items"]
    assert pending_invitation["id"] not in {item["id"] for item in items}
    accepted_item = next(item for item in items if item["id"] == accepted_invitation["id"])
    assert accepted_item["status"] == "ACCEPTED"
