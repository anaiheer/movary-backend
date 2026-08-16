from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus, OrderType
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanBillingCycle,
    PlanStatus,
    Subscription,
    SubscriptionGroup,
    SubscriptionStatus,
)
from app.models.user import User, UserRole

from conftest import get_app_dependencies


@pytest.mark.asyncio
async def test_get_my_subscriptions_expires_stale_records(async_client):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        user = User(
            username=f"plans_user_{uuid.uuid4().hex[:8]}",
            email=f"plans_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.USER,
        )
        plan = Plan(
            group_key=f"plans-{uuid.uuid4().hex[:8]}",
            group_name="订阅测试",
            tier_level=1,
            name="Expired Plan",
            duration_days=30,
            price=30,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add_all([user, plan])
        await session.flush()

        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            start_at=datetime.utcnow() - timedelta(days=40),
            end_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add(subscription)
        await session.commit()
        await session.refresh(user)
        await session.refresh(subscription)
        user_id = user.id
        subscription_id = subscription.id

    token = create_token({"sub": str(user_id)}, token_type="access")
    resp = await async_client.get(
        "/api/v1/plans/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == str(subscription_id)
    assert data[0]["status"] == SubscriptionStatus.EXPIRED

    async with AsyncSessionLocal() as session:
        refreshed = await session.scalar(
            select(Subscription).where(Subscription.id == subscription_id)
        )
        assert refreshed is not None
        assert refreshed.status == SubscriptionStatus.EXPIRED


@pytest.mark.asyncio
async def test_get_plans_omits_biennial_price_field(async_client):
    async with AsyncSessionLocal() as session:
        plan = Plan(
            group_key=f"plans-{uuid.uuid4().hex[:8]}",
            group_name="Plans Listing",
            tier_level=1,
            name=f"Visible Plan {uuid.uuid4().hex[:6]}",
            duration_days=30,
            price=30,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        plan_id = str(plan.id)

    resp = await async_client.get("/api/v1/plans")

    assert resp.status_code == 200
    record = next(item for item in resp.json() if item["id"] == plan_id)
    assert "biennial_price" not in record


@pytest.mark.asyncio
async def test_purchase_preview_rejects_removed_two_year_cycle(async_client):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        user = User(
            username=f"plans_preview_{uuid.uuid4().hex[:8]}",
            email=f"plans_preview_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.USER,
        )
        plan = Plan(
            group_key=f"plans-preview-{uuid.uuid4().hex[:8]}",
            group_name="Plans Preview",
            tier_level=1,
            name=f"Preview Plan {uuid.uuid4().hex[:6]}",
            duration_days=30,
            price=30,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add_all([user, plan])
        await session.commit()
        await session.refresh(user)
        await session.refresh(plan)

    token = create_token({"sub": str(user.id)}, token_type="access")
    resp = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "TWO_YEAR"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_plans_hides_unset_cycle_and_promotes_real_default(async_client):
    async with AsyncSessionLocal() as session:
        plan = Plan(
            group_key=f"plans-cycles-{uuid.uuid4().hex[:8]}",
            group_name="Plans Cycles",
            tier_level=1,
            name=f"Cycle Plan {uuid.uuid4().hex[:6]}",
            duration_days=30,
            price=30,
            default_billing_cycle=BillingCycle.UNSET,
            monthly_price=18,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add(plan)
        await session.flush()
        session.add_all(
            [
                PlanBillingCycle(
                    plan_id=plan.id,
                    billing_cycle=BillingCycle.UNSET,
                    price=30,
                    duration_days=30,
                    is_default=True,
                    sort_order=20,
                ),
                PlanBillingCycle(
                    plan_id=plan.id,
                    billing_cycle=BillingCycle.MONTHLY,
                    price=18,
                    duration_days=30,
                    is_default=False,
                    sort_order=30,
                ),
            ]
        )
        await session.commit()
        plan_id = str(plan.id)

    resp = await async_client.get("/api/v1/plans")

    assert resp.status_code == 200
    record = next(item for item in resp.json() if item["id"] == plan_id)
    assert record["default_billing_cycle"] == "MONTHLY"
    assert [cycle["billing_cycle"] for cycle in record["cycles"]] == ["MONTHLY"]


@pytest.mark.asyncio
async def test_public_backend_does_not_mount_admin_plan_delete(async_client):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"plans_admin_{uuid.uuid4().hex[:8]}",
            email=f"plans_admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.ADMIN,
        )
        plan = Plan(
            group_key=f"plans-delete-{uuid.uuid4().hex[:8]}",
            group_name="Plans Delete",
            tier_level=1,
            name=f"Delete Plan {uuid.uuid4().hex[:6]}",
            duration_days=30,
            price=30,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add_all([admin, plan])
        await session.flush()
        order = Order(
            user_id=admin.id,
            plan_id=plan.id,
            order_no=f"OD{uuid.uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=30,
            status=OrderStatus.COMPLETED,
        )
        session.add(order)
        await session.commit()
        await session.refresh(admin)
        await session.refresh(plan)
        await session.refresh(order)
        admin_id = admin.id
        plan_id = plan.id

    token = create_token({"sub": str(admin_id)}, token_type="access")
    resp = await async_client.delete(
        f"/api/v1/admin/plans/{plan_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_public_backend_does_not_mount_admin_plan_delete_with_active_subscriptions(
    async_client,
):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"plans_admin_{uuid.uuid4().hex[:8]}",
            email=f"plans_admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.ADMIN,
        )
        plan = Plan(
            group_key=f"plans-active-{uuid.uuid4().hex[:8]}",
            group_name="Plans Active",
            tier_level=1,
            name=f"Active Plan {uuid.uuid4().hex[:6]}",
            duration_days=30,
            price=30,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add_all([admin, plan])
        await session.flush()
        session.add(
            Subscription(
                user_id=admin.id,
                plan_id=plan.id,
                status=SubscriptionStatus.ACTIVE,
                billing_cycle=BillingCycle.MONTHLY,
                start_at=datetime.utcnow(),
                end_at=datetime.utcnow() + timedelta(days=30),
            )
        )
        await session.commit()
        admin_id = admin.id
        plan_id = plan.id

    token = create_token({"sub": str(admin_id)}, token_type="access")
    resp = await async_client.delete(
        f"/api/v1/admin/plans/{plan_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_public_backend_does_not_mount_admin_plan_delete_with_stale_records(async_client):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"plans_admin_{uuid.uuid4().hex[:8]}",
            email=f"plans_admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.ADMIN,
        )
        plan = Plan(
            group_key=f"plans-stale-{uuid.uuid4().hex[:8]}",
            group_name="Plans Stale",
            tier_level=1,
            name=f"Stale Plan {uuid.uuid4().hex[:6]}",
            duration_days=30,
            price=30,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add_all([admin, plan])
        await session.flush()
        session.add(
            Subscription(
                user_id=admin.id,
                plan_id=plan.id,
                status=SubscriptionStatus.ACTIVE,
                billing_cycle=BillingCycle.TRIAL,
                start_at=datetime.utcnow() - timedelta(days=3),
                end_at=datetime.utcnow() - timedelta(days=1),
            )
        )
        await session.commit()
        admin_id = admin.id
        plan_id = plan.id

    token = create_token({"sub": str(admin_id)}, token_type="access")
    resp = await async_client.delete(
        f"/api/v1/admin/plans/{plan_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_public_backend_does_not_mount_admin_group_delete(async_client):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"group_admin_{uuid.uuid4().hex[:8]}",
            email=f"group_admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.ADMIN,
        )
        group = SubscriptionGroup(
            key=f"group-{uuid.uuid4().hex[:8]}",
            name=f"Group {uuid.uuid4().hex[:6]}",
            tier_count=3,
        )
        session.add_all([admin, group])
        await session.flush()
        session.add(
            Plan(
                group_key=group.key,
                group_name=group.name,
                tier_level=1,
                name=f"Group Plan {uuid.uuid4().hex[:6]}",
                duration_days=30,
                price=30,
                status=PlanStatus.ON,
                is_visible=True,
            )
        )
        await session.commit()
        admin_id = admin.id
        group_id = group.id

    token = create_token({"sub": str(admin_id)}, token_type="access")
    resp = await async_client.delete(
        f"/api/v1/admin/plans/groups/{group_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_public_backend_does_not_mount_admin_group_delete_when_empty(async_client):
    _, _, _, _, create_token, hash_password, _, _ = get_app_dependencies()

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"group_admin_{uuid.uuid4().hex[:8]}",
            email=f"group_admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
            role=UserRole.ADMIN,
        )
        group = SubscriptionGroup(
            key=f"group-empty-{uuid.uuid4().hex[:8]}",
            name=f"Empty Group {uuid.uuid4().hex[:6]}",
            tier_count=3,
        )
        session.add_all([admin, group])
        await session.commit()
        admin_id = admin.id
        group_id = group.id

    token = create_token({"sub": str(admin_id)}, token_type="access")
    resp = await async_client.delete(
        f"/api/v1/admin/plans/groups/{group_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404
