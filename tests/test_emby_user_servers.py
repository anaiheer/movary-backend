import pytest
from datetime import datetime, timedelta
from sqlalchemy import select

from app.core.security import create_token
from app.db.session import AsyncSessionLocal
from app.models.emby import EmbyAccount, EmbyServer
from app.models.subscription import (
    Plan,
    PlanServerAssignment,
    Subscription,
    SubscriptionStatus,
    BillingCycle,
)
from app.models.user import User
from app.services.emby_accounts import ensure_emby_accounts_for_user


async def create_user_and_token():
    async with AsyncSessionLocal() as session:
        user = User(
            username="emby_user",
            email="emby_user@example.com",
            password_hash="$2b$12$Zb2aTFDqQ6dYxq2H2QmBfuXq5QkU7b.1iKk9ppn7Q3aY5G0s1Q0kW",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = create_token({"sub": str(user.id)}, token_type="access")
        return user, token


@pytest.mark.asyncio
async def test_get_my_emby_servers(async_client):
    user, token = await create_user_and_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncSessionLocal() as session:
        server = EmbyServer(
            name="S1",
            base_url="http://emby.local:8096",
            api_key="test-key",
            is_active=True,
            is_default=True,
            priority=1,
        )
        plan = Plan(
            group_key="emby-p1",
            group_name="Emby 测试",
            tier_level=1,
            name="P1",
            description="plan",
            duration_days=30,
            price=9.9,
            vod_movie_times=5,
            vod_tv_times=5,
            is_visible=True,
        )
        session.add_all([server, plan])
        await session.commit()
        await session.refresh(server)
        await session.refresh(plan)

        assign = PlanServerAssignment(
            plan_id=plan.id,
            server_id=server.id,
            template_emby_user_id="t1",
            template_emby_username="temp",
        )
        sub = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.UNSET,
            start_at=datetime.utcnow(),
            end_at=datetime.utcnow() + timedelta(days=30),
            auto_renew=False,
        )
        session.add_all([assign, sub])
        await session.commit()

    resp = await async_client.get("/api/v1/emby/me/servers", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"]
    assert data["items"][0]["server_name"] == "S1"


@pytest.mark.asyncio
async def test_ensure_emby_accounts_reenables_disabled_account(monkeypatch):
    disabled_states: list[tuple[str, str]] = []

    async def fake_ensure_user(_server, username, _password):
        return "existing-user-id", username

    async def fake_set_status(_server, emby_user_id, desired_status):
        disabled_states.append((emby_user_id, desired_status))

    monkeypatch.setattr("app.services.emby_accounts._ensure_emby_user", fake_ensure_user)
    monkeypatch.setattr("app.services.emby_accounts._set_emby_user_status", fake_set_status)
    monkeypatch.setattr("app.services.emby_accounts.encrypt_emby_password", lambda value: value)

    async with AsyncSessionLocal() as session:
        user = User(
            username="emby_restore_user",
            email="emby_restore@example.com",
            password_hash="hashed",
        )
        server = EmbyServer(
            name="Restore Server",
            base_url="http://emby-restore.local:8096",
            api_key="restore-key",
            is_active=True,
            is_default=True,
            priority=1,
        )
        plan = Plan(
            group_key="emby-restore-plan",
            group_name="Restore Group",
            tier_level=1,
            name="Restore Plan",
            description="plan",
            duration_days=30,
            price=9.9,
            vod_movie_times=5,
            vod_tv_times=5,
            is_visible=True,
        )
        session.add_all([user, server, plan])
        await session.flush()
        session.add(
            PlanServerAssignment(
                plan_id=plan.id,
                server_id=server.id,
                template_emby_user_id="restore-template",
                template_emby_username="restore-template",
            )
        )
        session.add(
            EmbyAccount(
                user_id=user.id,
                emby_server_id=server.id,
                emby_user_id="existing-user-id",
                emby_username=user.username,
                emby_password=None,
                status="DISABLED",
            )
        )
        await session.commit()
        await session.refresh(user)

        await ensure_emby_accounts_for_user(session, user, plan.id, "Restore123")
        await session.commit()

        account = await session.scalar(select(EmbyAccount).where(EmbyAccount.user_id == user.id))
        assert account is not None
        assert account.status == "ENABLED"
        assert disabled_states == [("existing-user-id", "ENABLED")]
