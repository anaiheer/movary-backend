import pytest
from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import select
from uuid import UUID

from app.db.session import AsyncSessionLocal
from app.models.emby import EmbyAccount, EmbyServer
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanServerAssignment,
    Subscription,
    SubscriptionStatus,
)
from app.models.system_settings import SystemSettings
from app.models.user import User


async def ensure_settings(email_verification_enabled: bool = True, site_url: str | None = None):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.email_verification_enabled = email_verification_enabled
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
async def test_admin_can_create_user_with_two_char_chinese_username(async_client, admin_token):
    resp = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "李雷",
            "email": "lilei@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_admin_create_user_rejects_invalid_username(async_client, admin_token):
    resp = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "李 雷",
            "email": "bad-admin-user@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any(item["loc"][-1] == "username" for item in detail)
    assert any("用户名仅支持中文、字母、数字、下划线、中划线和点" in item["msg"] for item in detail)


@pytest.mark.asyncio
async def test_admin_create_user_rejects_weak_password(async_client, admin_token):
    resp = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "weak_admin_user",
            "email": "weak-admin-user@example.com",
            "password": "abcdefgh",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any(item["loc"][-1] == "password" for item in detail)
    assert any("密码至少 8 位，且需同时包含字母和数字" in item["msg"] for item in detail)


@pytest.mark.asyncio
async def test_admin_update_email_requires_reverification(async_client, admin_token, monkeypatch):
    await ensure_settings(email_verification_enabled=True, site_url="https://movary.example.com")

    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_edit_user",
            "email": "old-admin-edit@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    captured: dict[str, str] = {}

    async def fake_send_email(to_email, subject, html, text, _smtp_config):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["html"] = html
        captured["text"] = text

    monkeypatch.setattr("app.api.routes.admin_users.send_email", fake_send_email)

    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={"email": "new-admin-edit@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["message"] == "用户已更新，验证邮件已发送到新邮箱"
    assert captured["to_email"] == "new-admin-edit@example.com"

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar()
        assert user
        assert user.email == "new-admin-edit@example.com"
        assert user.email_verified is False
        assert user.email_verified_at is None
        assert user.email_verification_token
        assert (
            f"https://movary.example.com/verify-email?token={user.email_verification_token}"
            in captured["html"]
        )


@pytest.mark.asyncio
async def test_admin_update_user_rejects_short_emby_password(async_client, admin_token):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "emby_password_short",
            "email": "emby-password-short@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={"emby_password": "12345"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert update.status_code == 400, update.text
    assert update.json()["detail"] == "Emby 密码过短"


@pytest.mark.asyncio
async def test_admin_update_user_rejects_weak_new_password(async_client, admin_token):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_password_update",
            "email": "admin-password-update@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={"reset_password": True, "new_password": "abcdefgh"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert update.status_code == 422, update.text
    detail = update.json()["detail"]
    assert any(item["loc"][-1] == "new_password" for item in detail)
    assert any("密码至少 8 位，且需同时包含字母和数字" in item["msg"] for item in detail)


@pytest.mark.asyncio
async def test_admin_update_email_rolls_back_when_verification_email_fails(
    async_client, admin_token, monkeypatch
):
    await ensure_settings(email_verification_enabled=True)

    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_edit_rollback",
            "email": "admin-edit-before@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    async def failing_send_email(*_args, **_kwargs):
        raise RuntimeError("smtp unavailable")

    monkeypatch.setattr("app.api.routes.admin_users.send_email", failing_send_email)

    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={"email": "admin-edit-after@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert update.status_code == 502, update.text
    assert update.json()["detail"] == "新邮箱验证邮件发送失败，修改未生效，请稍后重试"

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar()
        assert user
        assert user.email == "admin-edit-before@example.com"
        assert user.email_verified is True


@pytest.mark.asyncio
async def test_admin_update_user_supports_multiple_subscriptions(async_client, admin_token):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "multi_sub_editor",
            "email": "multi-sub-editor@example.com",
            "password": "Test123456",
            "role": "USER",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    async with AsyncSessionLocal() as session:
        basic = Plan(
            name="Admin Multi Basic",
            group_key="admin-multi-basic",
            group_name="基础服务",
            tier_level=1,
            description="basic",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=3,
            vod_tv_times=1,
        )
        premium = Plan(
            name="Admin Multi Premium",
            group_key="admin-multi-premium",
            group_name="进阶服务",
            tier_level=2,
            description="premium",
            duration_days=30,
            price=Decimal("20.00"),
            vod_movie_times=5,
            vod_tv_times=2,
        )
        session.add_all([basic, premium])
        await session.commit()
        await session.refresh(basic)
        await session.refresh(premium)

    start_at = datetime.utcnow().replace(microsecond=0)
    payload = {
        "subscriptions": [
            {
                "plan_id": str(basic.id),
                "billing_cycle": BillingCycle.MONTHLY.value,
                "start_at": start_at.isoformat(),
                "end_at": (start_at + timedelta(days=30)).isoformat(),
            },
            {
                "plan_id": str(premium.id),
                "billing_cycle": BillingCycle.QUARTERLY.value,
                "start_at": start_at.isoformat(),
                "end_at": (start_at + timedelta(days=90)).isoformat(),
            },
        ]
    }
    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert update.status_code == 200, update.text

    async with AsyncSessionLocal() as session:
        rows = (
            (await session.execute(select(Subscription).where(Subscription.user_id == user_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert {sub.billing_cycle for sub in rows} == {
            BillingCycle.MONTHLY,
            BillingCycle.QUARTERLY,
        }


@pytest.mark.asyncio
async def test_admin_user_list_marks_canceled_subscription_status(async_client, admin_token):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_sub_hidden",
            "email": "admin-sub-hidden@example.com",
            "password": "Test123456",
            "role": "ADMIN",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    async with AsyncSessionLocal() as session:
        plan = Plan(
            name="Admin Hidden Plan",
            group_key="admin-hidden-plan",
            group_name="Hidden Group",
            tier_level=1,
            description="hidden",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=plan.id,
                status=SubscriptionStatus.CANCELED,
                billing_cycle=BillingCycle.TRIAL,
                start_at=datetime.utcnow() - timedelta(days=2),
                end_at=datetime.utcnow() - timedelta(days=1),
            )
        )
        await session.commit()

    resp = await async_client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]["items"]
    target = next(item for item in rows if item["id"] == str(user_id))
    assert target["subscription"]["status"] == "CANCELED"
    assert target["subscription"]["plan"]["name"] == "Admin Hidden Plan"
    assert target["subscription_summary"]["status"] == "CANCELED"
    assert target["subscription_summary"]["active_count"] == 0
    assert target["subscription_summary"]["items"] == []


@pytest.mark.asyncio
async def test_admin_user_list_filter_canceled_excludes_active_users(async_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    canceled_user = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_sub_canceled",
            "email": "admin-sub-canceled@example.com",
            "password": "Test123456",
            "role": "ADMIN",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers=headers,
    )
    active_user = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_sub_active",
            "email": "admin-sub-active@example.com",
            "password": "Test123456",
            "role": "ADMIN",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers=headers,
    )
    assert canceled_user.status_code == 200, canceled_user.text
    assert active_user.status_code == 200, active_user.text
    canceled_user_id = UUID(canceled_user.json()["data"]["id"])
    active_user_id = UUID(active_user.json()["data"]["id"])

    async with AsyncSessionLocal() as session:
        canceled_plan = Plan(
            name="Canceled Filter Plan",
            group_key="canceled-filter-plan",
            group_name="Canceled Filter Group",
            tier_level=1,
            description="canceled",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        active_plan = Plan(
            name="Active Filter Plan",
            group_key="active-filter-plan",
            group_name="Active Filter Group",
            tier_level=1,
            description="active",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        session.add_all([canceled_plan, active_plan])
        await session.flush()
        session.add_all(
            [
                Subscription(
                    user_id=canceled_user_id,
                    plan_id=canceled_plan.id,
                    status=SubscriptionStatus.CANCELED,
                    billing_cycle=BillingCycle.TRIAL,
                    start_at=datetime.utcnow() - timedelta(days=3),
                    end_at=datetime.utcnow() - timedelta(days=1),
                ),
                Subscription(
                    user_id=active_user_id,
                    plan_id=active_plan.id,
                    status=SubscriptionStatus.ACTIVE,
                    billing_cycle=BillingCycle.MONTHLY,
                    start_at=datetime.utcnow() - timedelta(days=1),
                    end_at=datetime.utcnow() + timedelta(days=29),
                ),
            ]
        )
        await session.commit()

    resp = await async_client.get(
        "/api/v1/admin/users",
        params={"subscription_status": "CANCELED"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]["items"]
    row_ids = {item["id"] for item in rows}
    assert str(canceled_user_id) in row_ids
    assert str(active_user_id) not in row_ids
    target = next(item for item in rows if item["id"] == str(canceled_user_id))
    assert target["subscription_summary"]["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_admin_update_user_with_empty_subscriptions_clears_canceled_history(
    async_client, admin_token
):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_sub_clear",
            "email": "admin-sub-clear@example.com",
            "password": "Test123456",
            "role": "ADMIN",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    async with AsyncSessionLocal() as session:
        plan = Plan(
            name="Admin Clear Plan",
            group_key="admin-clear-plan",
            group_name="Clear Group",
            tier_level=1,
            description="clear",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=plan.id,
                status=SubscriptionStatus.CANCELED,
                billing_cycle=BillingCycle.TRIAL,
                start_at=datetime.utcnow() - timedelta(days=2),
                end_at=datetime.utcnow() - timedelta(days=1),
            )
        )
        await session.commit()

    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={"subscriptions": []},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert update.status_code == 200, update.text

    async with AsyncSessionLocal() as session:
        rows = (
            (await session.execute(select(Subscription).where(Subscription.user_id == user_id)))
            .scalars()
            .all()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_admin_update_user_clearing_subscriptions_removes_emby_accounts(
    async_client, admin_token, monkeypatch
):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_emby_clear",
            "email": "admin-emby-clear@example.com",
            "password": "Test123456",
            "role": "ADMIN",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    deleted_users: list[tuple[str, str]] = []

    async def fake_delete(server, emby_user_id):
        deleted_users.append((server.name, emby_user_id))

    monkeypatch.setattr("app.services.emby_accounts._delete_emby_user", fake_delete)

    async with AsyncSessionLocal() as session:
        server = EmbyServer(
            name="Clear Server",
            base_url="http://emby-clear.local:8096",
            api_key="clear-key",
            is_active=True,
            is_default=True,
            priority=1,
        )
        plan = Plan(
            name="Admin Emby Clear Plan",
            group_key="admin-emby-clear-plan",
            group_name="Clear Emby Group",
            tier_level=1,
            description="clear",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        session.add_all([server, plan])
        await session.flush()
        session.add(
            PlanServerAssignment(
                plan_id=plan.id,
                server_id=server.id,
                template_emby_user_id="clear-template",
                template_emby_username="clear-template",
            )
        )
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=plan.id,
                status=SubscriptionStatus.ACTIVE,
                billing_cycle=BillingCycle.MONTHLY,
                start_at=datetime.utcnow() - timedelta(days=1),
                end_at=datetime.utcnow() + timedelta(days=29),
                auto_renew=False,
            )
        )
        session.add(
            EmbyAccount(
                user_id=user_id,
                emby_server_id=server.id,
                emby_user_id="emby-clear-user",
                emby_username="admin_emby_clear",
                emby_password=None,
                status="ENABLED",
            )
        )
        await session.commit()
        server_name = server.name

    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={"subscriptions": []},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert update.status_code == 200, update.text
    assert deleted_users == [(server_name, "emby-clear-user")]

    async with AsyncSessionLocal() as session:
        subs = (
            (await session.execute(select(Subscription).where(Subscription.user_id == user_id)))
            .scalars()
            .all()
        )
        accounts = (
            (await session.execute(select(EmbyAccount).where(EmbyAccount.user_id == user_id)))
            .scalars()
            .all()
        )
        assert subs == []
        assert accounts == []


@pytest.mark.asyncio
async def test_admin_update_user_subscriptions_remove_only_orphaned_emby_accounts(
    async_client, admin_token, monkeypatch
):
    create = await async_client.post(
        "/api/v1/admin/users",
        json={
            "username": "admin_emby_partial",
            "email": "admin-emby-partial@example.com",
            "password": "Test123456",
            "role": "ADMIN",
            "balance": "0.00",
            "expire_remind": True,
            "vod_movie_limit": 0,
            "vod_tv_limit": 0,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200, create.text
    user_id = UUID(create.json()["data"]["id"])

    deleted_users: list[tuple[str, str]] = []

    async def fake_delete(server, emby_user_id):
        deleted_users.append((server.name, emby_user_id))

    async def noop_ensure(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.emby_accounts._delete_emby_user", fake_delete)
    monkeypatch.setattr("app.api.routes.admin_users._ensure_emby_accounts", noop_ensure)

    async with AsyncSessionLocal() as session:
        server_keep = EmbyServer(
            name="Keep Server",
            base_url="http://emby-keep.local:8096",
            api_key="keep-key",
            is_active=True,
            is_default=True,
            priority=1,
        )
        server_drop = EmbyServer(
            name="Drop Server",
            base_url="http://emby-drop.local:8096",
            api_key="drop-key",
            is_active=True,
            is_default=False,
            priority=2,
        )
        keep_plan = Plan(
            name="Admin Emby Keep Plan",
            group_key="admin-emby-keep-plan",
            group_name="Keep Emby Group",
            tier_level=1,
            description="keep",
            duration_days=30,
            price=Decimal("10.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        drop_plan = Plan(
            name="Admin Emby Drop Plan",
            group_key="admin-emby-drop-plan",
            group_name="Drop Emby Group",
            tier_level=2,
            description="drop",
            duration_days=30,
            price=Decimal("20.00"),
            vod_movie_times=1,
            vod_tv_times=1,
        )
        session.add_all([server_keep, server_drop, keep_plan, drop_plan])
        await session.flush()
        keep_plan_id = keep_plan.id
        session.add_all(
            [
                PlanServerAssignment(
                    plan_id=keep_plan.id,
                    server_id=server_keep.id,
                    template_emby_user_id="keep-template",
                    template_emby_username="keep-template",
                ),
                PlanServerAssignment(
                    plan_id=drop_plan.id,
                    server_id=server_drop.id,
                    template_emby_user_id="drop-template",
                    template_emby_username="drop-template",
                ),
                Subscription(
                    user_id=user_id,
                    plan_id=keep_plan.id,
                    status=SubscriptionStatus.ACTIVE,
                    billing_cycle=BillingCycle.MONTHLY,
                    start_at=datetime.utcnow() - timedelta(days=2),
                    end_at=datetime.utcnow() + timedelta(days=28),
                    auto_renew=False,
                ),
                Subscription(
                    user_id=user_id,
                    plan_id=drop_plan.id,
                    status=SubscriptionStatus.ACTIVE,
                    billing_cycle=BillingCycle.MONTHLY,
                    start_at=datetime.utcnow() - timedelta(days=2),
                    end_at=datetime.utcnow() + timedelta(days=28),
                    auto_renew=False,
                ),
                EmbyAccount(
                    user_id=user_id,
                    emby_server_id=server_keep.id,
                    emby_user_id="emby-keep-user",
                    emby_username="admin_emby_partial",
                    emby_password=None,
                    status="ENABLED",
                ),
                EmbyAccount(
                    user_id=user_id,
                    emby_server_id=server_drop.id,
                    emby_user_id="emby-drop-user",
                    emby_username="admin_emby_partial",
                    emby_password=None,
                    status="ENABLED",
                ),
            ]
        )
        await session.commit()
        drop_server_name = server_drop.name

    start_at = datetime.utcnow().replace(microsecond=0)
    update = await async_client.put(
        f"/api/v1/admin/users/{user_id}",
        json={
            "subscriptions": [
                {
                    "plan_id": str(keep_plan_id),
                    "billing_cycle": BillingCycle.MONTHLY.value,
                    "start_at": start_at.isoformat(),
                    "end_at": (start_at + timedelta(days=30)).isoformat(),
                }
            ]
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert update.status_code == 200, update.text
    assert deleted_users == [(drop_server_name, "emby-drop-user")]

    async with AsyncSessionLocal() as session:
        accounts = (
            (await session.execute(select(EmbyAccount).where(EmbyAccount.user_id == user_id)))
            .scalars()
            .all()
        )
        assert len(accounts) == 1
        assert accounts[0].emby_user_id == "emby-keep-user"
