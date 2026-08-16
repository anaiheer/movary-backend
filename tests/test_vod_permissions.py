from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.subscription import Plan, PlanStatus, Subscription, SubscriptionStatus
from app.models.user import User, UserRole
from app.models.vod import VodRequest, VodRequestStatus
from app.api.routes import admin_vod as admin_vod_route

from conftest import get_app_dependencies


async def _create_user_with_token(
    *,
    vod_movie_limit: int = 0,
    vod_movie_used: int = 0,
    vod_tv_limit: int = 0,
    vod_tv_used: int = 0,
):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        user = User(
            username=f"vod_user_{uuid.uuid4().hex[:8]}",
            email=f"vod_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.USER,
            vod_movie_limit=vod_movie_limit,
            vod_movie_used=vod_movie_used,
            vod_tv_limit=vod_tv_limit,
            vod_tv_used=vod_tv_used,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id, create_token({"sub": str(user.id)}, token_type="access")


async def _create_plan_id(name: str, *, vod_movie_times: int = 1, vod_tv_times: int = 0):
    async with AsyncSessionLocal() as session:
        plan = Plan(
            group_key=f"vod-{uuid.uuid4().hex[:8]}",
            group_name=name,
            tier_level=1,
            name=name,
            duration_days=30,
            price=30,
            vod_movie_times=vod_movie_times,
            vod_tv_times=vod_tv_times,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        return plan.id


async def _create_subscription(
    *,
    user_id,
    plan_id,
    start_at: datetime,
    end_at: datetime,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
):
    async with AsyncSessionLocal() as session:
        subscription = Subscription(
            user_id=user_id,
            plan_id=plan_id,
            status=status,
            start_at=start_at,
            end_at=end_at,
        )
        session.add(subscription)
        await session.commit()


@pytest.mark.asyncio
async def test_vod_request_requires_active_subscription_and_zeroes_limits(async_client):
    user_id, token = await _create_user_with_token(
        vod_movie_limit=5,
        vod_movie_used=1,
        vod_tv_limit=4,
        vod_tv_used=2,
    )
    plan_id = await _create_plan_id("Expired VOD Plan")
    now = datetime.utcnow()
    await _create_subscription(
        user_id=user_id,
        plan_id=plan_id,
        start_at=now - timedelta(days=10),
        end_at=now - timedelta(days=1),
    )
    headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        "/api/v1/vod/requests",
        headers=headers,
        json={"title": "Movie A", "media_type": "MOVIE", "year": 2025, "tmdb_id": 1001},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "请先购买有效订阅服务"

    limits_resp = await async_client.get("/api/v1/vod/limits", headers=headers)
    assert limits_resp.status_code == 200
    assert limits_resp.json() == {
        "movie_limit": 0,
        "movie_used": 0,
        "tv_limit": 0,
        "tv_used": 0,
    }

    async with AsyncSessionLocal() as session:
        requests = (
            (await session.execute(select(VodRequest).where(VodRequest.user_id == user_id)))
            .scalars()
            .all()
        )
        assert requests == []


@pytest.mark.asyncio
async def test_vod_request_requires_remaining_quota_even_when_auto_approve_is_off(async_client):
    user_id, token = await _create_user_with_token(vod_movie_limit=1, vod_movie_used=1)
    plan_id = await _create_plan_id("Quota Plan")
    now = datetime.utcnow()
    await _create_subscription(
        user_id=user_id,
        plan_id=plan_id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=29),
    )
    headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        "/api/v1/vod/requests",
        headers=headers,
        json={"title": "Movie B", "media_type": "MOVIE", "year": 2025, "tmdb_id": 1002},
    )

    assert response.status_code == 400

    async with AsyncSessionLocal() as session:
        requests = (
            (await session.execute(select(VodRequest).where(VodRequest.user_id == user_id)))
            .scalars()
            .all()
        )
        assert requests == []


@pytest.mark.asyncio
async def test_admin_cannot_approve_vod_request_without_active_subscription(
    async_client, admin_token
):
    user_id, _ = await _create_user_with_token(vod_movie_limit=5)
    plan_id = await _create_plan_id("Admin Approval Plan")
    now = datetime.utcnow()
    await _create_subscription(
        user_id=user_id,
        plan_id=plan_id,
        start_at=now - timedelta(days=15),
        end_at=now - timedelta(days=2),
    )

    async with AsyncSessionLocal() as session:
        vod = VodRequest(
            user_id=user_id,
            title="Movie C",
            media_type="MOVIE",
            year=2025,
            tmdb_id=1003,
            status=VodRequestStatus.PENDING,
            cost_type="TIMES",
            cost_amount=1,
        )
        session.add(vod)
        await session.commit()
        await session.refresh(vod)
        vod_id = vod.id

    response = await async_client.post(
        f"/api/v1/admin/vod/requests/{vod_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "该用户已无有效订阅"

    async with AsyncSessionLocal() as session:
        refreshed_vod = await session.scalar(select(VodRequest).where(VodRequest.id == vod_id))
        user = await session.scalar(select(User).where(User.id == user_id))
        assert refreshed_vod is not None
        assert refreshed_vod.status == VodRequestStatus.PENDING
        assert refreshed_vod.quota_consumed is False
        assert user is not None
        assert int(user.vod_movie_used or 0) == 0


@pytest.mark.asyncio
async def test_admin_approval_attributes_quota_to_subscription_with_remaining_balance(
    async_client, admin_token, monkeypatch
):
    class _DummyServer:
        base_url = "http://moviepilot.local"
        api_token = "token"

    async def _fake_get_moviepilot_server(db):
        return _DummyServer()

    async def _fake_subscribe_vod(*args, **kwargs):
        return {"id": "sub-1", "state": "APPROVED"}

    monkeypatch.setattr(admin_vod_route, "_get_moviepilot_server", _fake_get_moviepilot_server)
    monkeypatch.setattr(admin_vod_route, "subscribe_vod", _fake_subscribe_vod)

    user_id, _ = await _create_user_with_token(vod_movie_limit=2, vod_movie_used=1)
    plan_a_id = await _create_plan_id("Quota Slot A")
    plan_b_id = await _create_plan_id("Quota Slot B")
    now = datetime.utcnow()
    await _create_subscription(
        user_id=user_id,
        plan_id=plan_a_id,
        start_at=now - timedelta(days=5),
        end_at=now + timedelta(days=5),
    )
    await _create_subscription(
        user_id=user_id,
        plan_id=plan_b_id,
        start_at=now - timedelta(days=3),
        end_at=now + timedelta(days=20),
    )

    async with AsyncSessionLocal() as session:
        latest_sub = await session.scalar(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.plan_id == plan_b_id)
            .order_by(Subscription.created_at.desc())
        )
        assert latest_sub is not None
        session.add(
            VodRequest(
                user_id=user_id,
                subscription_id=latest_sub.id,
                quota_consumed=True,
                status=VodRequestStatus.APPROVED,
                title="Consumed Movie",
                media_type="MOVIE",
                cost_type="TIMES",
                cost_amount=1,
            )
        )
        pending_vod = VodRequest(
            user_id=user_id,
            title="Movie D",
            media_type="MOVIE",
            year=2025,
            tmdb_id=1004,
            status=VodRequestStatus.PENDING,
            cost_type="TIMES",
            cost_amount=1,
        )
        session.add(pending_vod)
        await session.commit()
        await session.refresh(pending_vod)
        vod_id = pending_vod.id

    response = await async_client.post(
        f"/api/v1/admin/vod/requests/{vod_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        refreshed_vod = await session.scalar(select(VodRequest).where(VodRequest.id == vod_id))
        earlier_sub = await session.scalar(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.plan_id == plan_a_id)
            .order_by(Subscription.created_at.desc())
        )
        user = await session.scalar(select(User).where(User.id == user_id))
        assert refreshed_vod is not None
        assert earlier_sub is not None
        assert user is not None
        assert refreshed_vod.status == VodRequestStatus.APPROVED
        assert refreshed_vod.subscription_id == earlier_sub.id
        assert refreshed_vod.quota_consumed is True
        assert int(user.vod_movie_used or 0) == 2


@pytest.mark.asyncio
async def test_admin_batch_delete_vod_requests_returns_precise_result(
    async_client, admin_token, monkeypatch
):
    async def _noop_invalidate_user_vod_cache(user_id):
        return None

    monkeypatch.setattr(
        admin_vod_route, "_invalidate_user_vod_cache", _noop_invalidate_user_vod_cache
    )

    user_id, _ = await _create_user_with_token()

    async with AsyncSessionLocal() as session:
        vod_a = VodRequest(
            user_id=user_id,
            title="Batch Delete A",
            media_type="MOVIE",
            year=2025,
            tmdb_id=2001,
            status=VodRequestStatus.PENDING,
            cost_type="TIMES",
            cost_amount=1,
        )
        vod_b = VodRequest(
            user_id=user_id,
            title="Batch Delete B",
            media_type="TV",
            year=2025,
            tmdb_id=2002,
            status=VodRequestStatus.REJECTED,
            cost_type="TIMES",
            cost_amount=1,
        )
        session.add_all([vod_a, vod_b])
        await session.commit()
        await session.refresh(vod_a)
        await session.refresh(vod_b)

    missing_id = uuid.uuid4()
    response = await async_client.post(
        "/api/v1/admin/vod/requests/batch-delete",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"ids": [str(vod_a.id), str(vod_b.id), str(vod_a.id), str(missing_id)]},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "requested": 3,
        "deleted": 2,
        "missing": 1,
        "missing_ids": [str(missing_id)],
        "failed_ids": [],
    }

    async with AsyncSessionLocal() as session:
        remaining = (
            (
                await session.execute(
                    select(VodRequest).where(VodRequest.id.in_([vod_a.id, vod_b.id]))
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


@pytest.mark.asyncio
async def test_admin_reject_vod_request_returns_rejected_status_label(
    async_client, admin_token, monkeypatch
):
    async def _noop_invalidate_user_vod_cache(user_id):
        return None

    monkeypatch.setattr(
        admin_vod_route, "_invalidate_user_vod_cache", _noop_invalidate_user_vod_cache
    )

    user_id, _ = await _create_user_with_token()

    async with AsyncSessionLocal() as session:
        vod = VodRequest(
            user_id=user_id,
            title="Reject Label Movie",
            media_type="MOVIE",
            year=2025,
            tmdb_id=3001,
            status=VodRequestStatus.PENDING,
            cost_type="TIMES",
            cost_amount=1,
        )
        session.add(vod)
        await session.commit()
        await session.refresh(vod)
        vod_id = vod.id

    reject_response = await async_client.post(
        f"/api/v1/admin/vod/requests/{vod_id}/reject",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "duplicate request"},
    )

    assert reject_response.status_code == 200
    assert reject_response.json()["data"]["status"] == VodRequestStatus.REJECTED

    list_response = await async_client.get(
        "/api/v1/admin/vod/requests",
        headers={"Authorization": f"Bearer {admin_token}"},
        params={"status": "REJECTED"},
    )

    assert list_response.status_code == 200
    items = list_response.json()["data"]["items"]
    matched = next(item for item in items if item["id"] == str(vod_id))
    assert matched["status"] == VodRequestStatus.REJECTED
    assert matched["status_label"] == "已拒绝"


@pytest.mark.asyncio
async def test_admin_pending_filter_excludes_rejected_vod_requests(
    async_client, admin_token, monkeypatch
):
    async def _noop_invalidate_user_vod_cache(user_id):
        return None

    monkeypatch.setattr(
        admin_vod_route, "_invalidate_user_vod_cache", _noop_invalidate_user_vod_cache
    )

    user_id, _ = await _create_user_with_token()

    async with AsyncSessionLocal() as session:
        pending_vod = VodRequest(
            user_id=user_id,
            title="Pending Filter Pending",
            media_type="MOVIE",
            year=2025,
            tmdb_id=4001,
            status=VodRequestStatus.PENDING,
            cost_type="TIMES",
            cost_amount=1,
        )
        rejected_vod = VodRequest(
            user_id=user_id,
            title="Pending Filter Rejected",
            media_type="TV",
            year=2025,
            tmdb_id=4002,
            status=VodRequestStatus.REJECTED,
            cost_type="TIMES",
            cost_amount=1,
        )
        session.add_all([pending_vod, rejected_vod])
        await session.commit()
        await session.refresh(pending_vod)
        await session.refresh(rejected_vod)

    response = await async_client.get(
        "/api/v1/admin/vod/requests",
        headers={"Authorization": f"Bearer {admin_token}"},
        params={"status": "PENDING"},
    )

    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()["data"]["items"]}
    assert str(pending_vod.id) in returned_ids
    assert str(rejected_vod.id) not in returned_ids
