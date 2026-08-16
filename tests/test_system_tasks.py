import pytest
from datetime import datetime, timedelta
from sqlalchemy import delete, select
from uuid import UUID


@pytest.mark.asyncio
async def test_ensure_default_tasks_creates_tmdb_cache_persist_task():
    from app.db.session import AsyncSessionLocal
    from app.models.system_task import SystemTask
    from app.services import system_tasks

    async with AsyncSessionLocal() as session:
        await session.execute(delete(SystemTask))
        await session.commit()

    await system_tasks.ensure_default_tasks()

    async with AsyncSessionLocal() as session:
        task = await session.scalar(
            select(SystemTask).where(SystemTask.key == "tmdb_cache_persist")
        )
        assert task is not None
        assert task.enabled is True
        assert task.interval_seconds == 1800


@pytest.mark.asyncio
async def test_ensure_default_tasks_creates_subscription_expire_sync_task():
    from app.db.session import AsyncSessionLocal
    from app.models.system_task import SystemTask
    from app.services import system_tasks

    async with AsyncSessionLocal() as session:
        await session.execute(delete(SystemTask))
        await session.commit()

    await system_tasks.ensure_default_tasks()

    async with AsyncSessionLocal() as session:
        task = await session.scalar(
            select(SystemTask).where(SystemTask.key == "subscription_expire_sync")
        )
        assert task is not None
        assert task.enabled is True
        assert task.interval_seconds == 300


@pytest.mark.asyncio
async def test_ensure_default_tasks_reenables_locked_task():
    from app.db.session import AsyncSessionLocal
    from app.models.system_task import SystemTask
    from app.services import system_tasks

    await system_tasks.ensure_default_tasks()

    async with AsyncSessionLocal() as session:
        task = await session.scalar(
            select(SystemTask).where(SystemTask.key == "tmdb_cache_persist")
        )
        assert task is not None
        task.enabled = False
        session.add(task)
        await session.commit()

    await system_tasks.ensure_default_tasks()

    async with AsyncSessionLocal() as session:
        task = await session.scalar(
            select(SystemTask).where(SystemTask.key == "tmdb_cache_persist")
        )
        assert task is not None
        assert task.enabled is True


@pytest.mark.asyncio
async def test_sync_redis_cache_to_db(monkeypatch):
    from app.db.session import AsyncSessionLocal
    from app.models.tmdb_cache import TmdbCache
    from app.services import tmdb as tmdb_service

    class DummyRedisClient:
        def __init__(self):
            self.scan_calls = 0

        async def scan(self, cursor, match, count):
            assert cursor in {0, "0"}
            assert match == f"{tmdb_service.REDIS_CACHE_PREFIX}*"
            assert count == tmdb_service.REDIS_SYNC_SCAN_COUNT
            self.scan_calls += 1
            return (
                0,
                [
                    f"{tmdb_service.REDIS_CACHE_PREFIX}discover:page=1",
                    f"{tmdb_service.REDIS_CACHE_PREFIX}genres:movie",
                ],
            )

    payloads = {
        f"{tmdb_service.REDIS_CACHE_PREFIX}discover:page=1": {"results": [{"id": "movie-1"}]},
        f"{tmdb_service.REDIS_CACHE_PREFIX}genres:movie": {"results": [{"id": "genre-1"}]},
    }

    async def fake_get_many_json(keys):
        return [payloads.get(key) for key in keys]

    monkeypatch.setattr(tmdb_service, "get_cache_client", lambda: DummyRedisClient())
    monkeypatch.setattr(tmdb_service, "get_many_json", fake_get_many_json)

    async with AsyncSessionLocal() as session:
        await session.execute(delete(TmdbCache))
        await session.commit()

        result = await tmdb_service.sync_redis_cache_to_db(session)
        assert result == {"checked": 2, "updated": 2}

    payloads[f"{tmdb_service.REDIS_CACHE_PREFIX}discover:page=1"] = {"results": [{"id": "movie-2"}]}

    async with AsyncSessionLocal() as session:
        result = await tmdb_service.sync_redis_cache_to_db(session)
        assert result == {"checked": 2, "updated": 2}

        rows = (
            (await session.execute(select(TmdbCache).order_by(TmdbCache.cache_key.asc())))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].cache_key == "discover:page=1"
        assert rows[0].payload == {"results": [{"id": "movie-2"}]}
        assert rows[1].cache_key == "genres:movie"
        assert rows[1].payload == {"results": [{"id": "genre-1"}]}


@pytest.mark.asyncio
async def test_update_tmdb_cache_persist_task_keeps_enabled(async_client, admin_token, monkeypatch):
    from app.api.routes import admin_tasks as admin_tasks_route
    from app.db.session import AsyncSessionLocal
    from app.models.system_task import SystemTask
    from app.services import system_tasks

    def fake_refresh_schedule(task):
        return None

    monkeypatch.setattr(admin_tasks_route, "refresh_schedule", fake_refresh_schedule)

    await system_tasks.ensure_default_tasks()

    async with AsyncSessionLocal() as session:
        task = await session.scalar(
            select(SystemTask).where(SystemTask.key == "tmdb_cache_persist")
        )
        assert task is not None
        task_id = str(task.id)

    resp = await async_client.put(
        f"/api/v1/admin/tasks/{task_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"interval_seconds": 2400, "enabled": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["enabled"] is True
    assert data["data"]["interval_seconds"] == 2400

    async with AsyncSessionLocal() as session:
        task = await session.scalar(select(SystemTask).where(SystemTask.id == UUID(task_id)))
        assert task is not None
        assert task.enabled is True
        assert task.interval_seconds == 2400


@pytest.mark.asyncio
async def test_run_subscription_expire_sync_disables_orphaned_emby_accounts(monkeypatch):
    from app.db.session import AsyncSessionLocal
    from app.models.emby import EmbyAccount, EmbyServer
    from app.models.subscription import (
        BillingCycle,
        Plan,
        PlanServerAssignment,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.system_task import SystemTask
    from app.models.system_settings import SystemSettings
    from app.models.user import User
    from app.services import system_tasks

    disabled_users: list[tuple[str, str, str]] = []

    async def fake_set_status(server, emby_user_id, desired_status):
        disabled_users.append((server.name, emby_user_id, desired_status))

    monkeypatch.setattr("app.services.emby_accounts._set_emby_user_status", fake_set_status)

    async with AsyncSessionLocal() as session:
        # Keep aggregate counters deterministic when this test runs after tests
        # that intentionally leave expired subscriptions behind.
        await session.execute(delete(Subscription))
        await session.execute(delete(SystemTask))
        await session.commit()

    await system_tasks.ensure_default_tasks()

    async with AsyncSessionLocal() as session:
        task = await session.scalar(
            select(SystemTask).where(SystemTask.key == "subscription_expire_sync")
        )
        user = User(
            username="expire_sync_user",
            email="expire-sync@example.com",
            password_hash="hashed",
        )
        server = EmbyServer(
            name="Expire Sync Server",
            base_url="http://emby-expire.local:8096",
            api_key="expire-key",
            is_active=True,
            is_default=True,
            priority=1,
        )
        plan = Plan(
            name="Expire Sync Plan",
            group_key="expire-sync-plan",
            group_name="Expire Sync Group",
            tier_level=1,
            description="expire",
            duration_days=30,
            price=9.9,
            vod_movie_times=1,
            vod_tv_times=1,
        )
        session.add_all([user, server, plan])
        await session.flush()
        session.add(
            PlanServerAssignment(
                plan_id=plan.id,
                server_id=server.id,
                template_emby_user_id="expire-template",
                template_emby_username="expire-template",
            )
        )
        session.add(
            Subscription(
                user_id=user.id,
                plan_id=plan.id,
                status=SubscriptionStatus.ACTIVE,
                billing_cycle=BillingCycle.MONTHLY,
                start_at=datetime.utcnow() - timedelta(days=31),
                end_at=datetime.utcnow() - timedelta(minutes=5),
                auto_renew=False,
            )
        )
        session.add(
            EmbyAccount(
                user_id=user.id,
                emby_server_id=server.id,
                emby_user_id="expire-user-id",
                emby_username="expire_sync_user",
                emby_password=None,
                status="ENABLED",
            )
        )
        settings = (await session.execute(select(SystemSettings))).scalar()
        if not settings:
            settings = SystemSettings()
        settings.subscription_retention_days = 30
        session.add(settings)
        await session.commit()
        task_id = str(task.id)
        user_id = user.id

    result = await system_tasks.run_task(task_id)
    assert result == {"checked": 1, "updated": 1, "message": "ok"}
    assert disabled_users == [("Expire Sync Server", "expire-user-id", "DISABLED")]

    async with AsyncSessionLocal() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        account = await session.scalar(select(EmbyAccount).where(EmbyAccount.user_id == user_id))
        assert subscription is not None
        assert subscription.status == SubscriptionStatus.EXPIRED
        assert account is not None
        assert account.status == "DISABLED"
