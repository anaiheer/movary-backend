from datetime import datetime, timedelta
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_token
from app.db.session import AsyncSessionLocal
from app.models.balance import BalanceTransaction
from app.models.order import (
    Order,
    OrderSettlementStatus,
    OrderStatus,
    OrderType,
    OrderValueLink,
    RefundStatus,
)
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanBillingCycle,
    PlanStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.system_settings import SystemSettings
from app.models.user import User, UserRole
from app.models.vod import VodRequest
from app.services import payments as payment_service
from app.services.payments import handle_paid_order
from app.services.plan_purchase import get_cycle_end_at
from app.services.subscriptions import cleanup_expired_subscription_data


async def _ensure_refund_settings(
    *,
    enabled: bool = True,
    window_days: int = 0,
    monthly_limit: int = 0,
    monthly_window_days: int = 30,
    forbid_if_vod_used: bool = False,
    vod_used_threshold: int = 0,
):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.refund_enabled = enabled
        row.refund_window_days = window_days
        row.refund_user_monthly_limit = monthly_limit
        row.refund_user_monthly_window_days = monthly_window_days
        row.refund_forbid_if_vod_used = forbid_if_vod_used
        row.refund_vod_used_threshold = vod_used_threshold
        session.add(row)
        await session.commit()


async def _ensure_epay_refund_settings():
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.refund_enabled = True
        row.refund_window_days = 0
        row.refund_user_monthly_limit = 0
        row.refund_user_monthly_window_days = 30
        row.epay_enabled = True
        row.epay_gateway = "https://example.com"
        row.epay_merchant_id = "demo-pid"
        row.epay_key = "demo-key"
        session.add(row)
        await session.commit()


async def _ensure_subscription_retention_days(days: int):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.subscription_retention_days = days
        session.add(row)
        await session.commit()


async def _create_user(*, balance: Decimal = Decimal("0.00")):
    async with AsyncSessionLocal() as session:
        username = f"user_{uuid.uuid4().hex[:8]}"
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="hashed",
            balance=balance,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _create_plan(
    *,
    name: str,
    price: Decimal,
    movie_limit: int,
    tv_limit: int,
    group_key: str | None = None,
    group_name: str | None = None,
    tier_level: int = 1,
    is_visible: bool = True,
):
    async with AsyncSessionLocal() as session:
        plan = Plan(
            group_key=group_key or f"group-{uuid.uuid4().hex[:8]}",
            group_name=group_name or name,
            tier_level=tier_level,
            name=name,
            description=name,
            duration_days=30,
            price=price,
            default_billing_cycle=BillingCycle.MONTHLY,
            monthly_price=price,
            vod_movie_times=movie_limit,
            vod_tv_times=tv_limit,
            status=PlanStatus.ON,
            is_visible=is_visible,
        )
        session.add(plan)
        await session.flush()
        session.add(
            PlanBillingCycle(
                plan_id=plan.id,
                billing_cycle=BillingCycle.MONTHLY,
                price=price,
                duration_days=30,
                is_default=True,
                sort_order=30,
            )
        )
        await session.commit()
        await session.refresh(plan)
        return plan


async def _add_plan_cycle(
    *, plan_id, billing_cycle: BillingCycle, price: Decimal, duration_days: int = 0
):
    async with AsyncSessionLocal() as session:
        session.add(
            PlanBillingCycle(
                plan_id=plan_id,
                billing_cycle=billing_cycle,
                price=price,
                duration_days=duration_days,
                is_default=False,
                sort_order=90,
            )
        )
        plan = await session.get(Plan, plan_id)
        if billing_cycle == BillingCycle.LIFETIME:
            plan.lifetime_price = price
        await session.commit()


@pytest.mark.asyncio
async def test_hidden_plan_cannot_create_order(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Hidden Checkout Plan",
        price=Decimal("9.90"),
        movie_limit=1,
        tv_limit=1,
        group_key="hidden-checkout",
        group_name="隐藏下单",
        tier_level=1,
        is_visible=False,
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id)},
        headers=headers,
    )

    assert response.status_code == 404


async def _create_subscription(
    *,
    user_id,
    plan_id,
    start_at: datetime,
    end_at: datetime,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    billing_cycle: BillingCycle = BillingCycle.MONTHLY,
):
    async with AsyncSessionLocal() as session:
        subscription = Subscription(
            user_id=user_id,
            plan_id=plan_id,
            status=status,
            billing_cycle=billing_cycle,
            start_at=start_at,
            end_at=end_at,
        )
        session.add(subscription)
        await session.commit()
        await session.refresh(subscription)
        return subscription


async def _create_order(
    *,
    user_id,
    plan_id,
    amount: Decimal,
    order_type: OrderType = OrderType.PLAN,
    status: OrderStatus = OrderStatus.CREATED,
    pay_provider: str | None = None,
    subscription_id=None,
    pay_payload: dict | None = None,
    paid_at: datetime | None = None,
    refunded_at: datetime | None = None,
    refund_status: RefundStatus = RefundStatus.NONE,
    created_at: datetime | None = None,
):
    async with AsyncSessionLocal() as session:
        order = Order(
            user_id=user_id,
            plan_id=plan_id,
            order_no=f"OD{uuid.uuid4().hex[:12]}",
            type=order_type,
            amount=float(amount),
            status=status,
            pay_provider=pay_provider,
            subscription_id=subscription_id,
            pay_payload=pay_payload or {},
            paid_at=paid_at,
            refunded_at=refunded_at,
            refund_status=refund_status,
            created_at=created_at or datetime.utcnow(),
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


def _auth_headers(user_id) -> dict[str, str]:
    token = create_token({"sub": str(user_id)}, token_type="access")
    return {"Authorization": f"Bearer {token}"}


async def _pay_order_with_balance(async_client, order_id, headers):
    response = await async_client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"pay_type": "balance"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response


async def _request_and_approve_balance_refund(async_client, order_id, user_headers, admin_headers):
    request_response = await async_client.post(
        f"/api/v1/orders/{order_id}/refund",
        json={"refund_to": "balance"},
        headers=user_headers,
    )
    assert request_response.status_code == 200, request_response.text

    approve_response = await async_client.post(
        f"/api/v1/admin/orders/{order_id}/refund",
        json={"refund_to": "balance"},
        headers=admin_headers,
    )
    assert approve_response.status_code == 200, approve_response.text
    return approve_response


async def _create_vod_request(*, user_id, subscription_id, media_type: str = "MOVIE"):
    async with AsyncSessionLocal() as session:
        vod = VodRequest(
            user_id=user_id,
            subscription_id=subscription_id,
            quota_consumed=True,
            status="APPROVED",
            title=f"{media_type} request",
            media_type=media_type,
            cost_type="TIMES",
            cost_amount=1,
        )
        session.add(vod)
        await session.commit()
        await session.refresh(vod)
        return vod


@pytest.mark.asyncio
async def test_direct_purchase_order_starts_new_order_chain(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Chain Direct Plan",
        price=Decimal("19.90"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"chain-direct-{uuid.uuid4().hex[:8]}",
        group_name="订单链首购",
        tier_level=1,
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    order_id = uuid.UUID(response.json()["data"]["order"]["id"])

    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        assert order.order_chain_id == order.id
        assert order.root_order_id == order.id
        assert order.parent_order_id is None
        assert order.settlement_status == OrderSettlementStatus.OPEN


@pytest.mark.asyncio
async def test_renew_order_reuses_existing_order_chain(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Chain Renew Plan",
        price=Decimal("12.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"chain-renew-{uuid.uuid4().hex[:8]}",
        group_name="订单链续费",
        tier_level=1,
    )
    now = datetime.utcnow()
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=3),
        end_at=now + timedelta(days=27),
    )
    root_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("12.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now - timedelta(days=3),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(subscription.id),
        },
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    renew_order_id = uuid.UUID(response.json()["data"]["order"]["id"])

    async with AsyncSessionLocal() as session:
        renew_order = await session.get(Order, renew_order_id)
        assert renew_order.parent_order_id == root_order.id
        assert renew_order.order_chain_id == root_order.order_chain_id
        assert renew_order.root_order_id == root_order.root_order_id
        assert renew_order.settlement_status == OrderSettlementStatus.OPEN


@pytest.mark.asyncio
async def test_replace_trial_order_uses_existing_chain_and_marks_trial_consumed(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Chain Trial Plan",
        price=Decimal("18.00"),
        movie_limit=3,
        tv_limit=2,
        group_key=f"chain-trial-{uuid.uuid4().hex[:8]}",
        group_name="订单链试用",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.TRIAL,
        price=Decimal("0.00"),
        duration_days=3,
    )
    now = datetime.utcnow()
    trial_subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=2),
        billing_cycle=BillingCycle.TRIAL,
    )
    trial_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("0.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="ZERO",
        subscription_id=trial_subscription.id,
        paid_at=now - timedelta(days=1),
        pay_payload={
            "billing_cycle": "TRIAL",
            "duration_days": 3,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(trial_subscription.id),
        },
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id), "billing_cycle": "MONTHLY"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    replace_order_id = uuid.UUID(response.json()["data"]["order"]["id"])

    async with AsyncSessionLocal() as session:
        replace_order = await session.get(Order, replace_order_id)
        assert replace_order.parent_order_id == trial_order.id
        assert replace_order.order_chain_id == trial_order.order_chain_id
        assert replace_order.root_order_id == trial_order.root_order_id

        replace_order.pay_provider = "ZERO"
        session.add(replace_order)
        await handle_paid_order(replace_order, "TRIAL-REPLACE-1", {"money": "0.00"}, session)

    async with AsyncSessionLocal() as session:
        refreshed_trial_order = await session.get(Order, trial_order.id)
        value_links = (
            (
                await session.execute(
                    select(OrderValueLink).where(OrderValueLink.target_order_id == replace_order_id)
                )
            )
            .scalars()
            .all()
        )
        assert refreshed_trial_order.superseded_by_order_id == replace_order_id
        assert refreshed_trial_order.settlement_status == OrderSettlementStatus.CONSUMED
        assert [link.relation_type for link in value_links] == ["REPLACE_TRIAL"]


@pytest.mark.asyncio
async def test_upgrade_to_lifetime_creates_value_link_and_marks_source_consumed(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Chain Lifetime Plan",
        price=Decimal("20.00"),
        movie_limit=3,
        tv_limit=2,
        group_key=f"chain-lifetime-{uuid.uuid4().hex[:8]}",
        group_name="订单链永久",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
        duration_days=0,
    )
    now = datetime.utcnow()
    monthly_subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=10),
        end_at=now + timedelta(days=20),
        billing_cycle=BillingCycle.MONTHLY,
    )
    monthly_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("20.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=monthly_subscription.id,
        paid_at=now - timedelta(days=10),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(monthly_subscription.id),
        },
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id), "billing_cycle": "LIFETIME"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    lifetime_order_id = uuid.UUID(response.json()["data"]["order"]["id"])

    async with AsyncSessionLocal() as session:
        lifetime_order = await session.get(Order, lifetime_order_id)
        assert lifetime_order.parent_order_id == monthly_order.id
        assert lifetime_order.order_chain_id == monthly_order.order_chain_id
        assert lifetime_order.root_order_id == monthly_order.root_order_id

        lifetime_order.pay_provider = "BALANCE"
        session.add(lifetime_order)
        await handle_paid_order(lifetime_order, "LIFETIME-UPGRADE-1", {"money": "80.00"}, session)

    async with AsyncSessionLocal() as session:
        refreshed_monthly_order = await session.get(Order, monthly_order.id)
        value_link = (
            (
                await session.execute(
                    select(OrderValueLink).where(
                        OrderValueLink.target_order_id == lifetime_order_id
                    )
                )
            )
            .scalars()
            .one()
        )

        assert refreshed_monthly_order.superseded_by_order_id == lifetime_order_id
        assert refreshed_monthly_order.settlement_status == OrderSettlementStatus.CONSUMED
        assert value_link.source_order_id == monthly_order.id
        assert value_link.relation_type == "UPGRADE_TO_LIFETIME"


@pytest.mark.asyncio
async def test_order_detail_includes_order_chain_snapshot(async_client):
    user = await _create_user(balance=Decimal("200.00"))
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Chain Detail Plan",
        price=Decimal("20.00"),
        movie_limit=3,
        tv_limit=2,
        group_key=f"chain-detail-{uuid.uuid4().hex[:8]}",
        group_name="订单链详情",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
        duration_days=0,
    )
    now = datetime.utcnow()
    monthly_subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=10),
        end_at=now + timedelta(days=20),
        billing_cycle=BillingCycle.MONTHLY,
    )
    monthly_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("20.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=monthly_subscription.id,
        paid_at=now - timedelta(days=10),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(monthly_subscription.id),
        },
    )

    create_response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id), "billing_cycle": "LIFETIME"},
        headers=headers,
    )
    assert create_response.status_code == 200, create_response.text
    lifetime_order_id = create_response.json()["data"]["order"]["id"]

    await _pay_order_with_balance(async_client, lifetime_order_id, headers)

    detail_response = await async_client.get(
        f"/api/v1/orders/{lifetime_order_id}",
        headers=headers,
    )
    assert detail_response.status_code == 200, detail_response.text
    payload = detail_response.json()
    chain = payload["order_chain"]

    assert chain["chain_id"] == str(monthly_order.order_chain_id)
    assert chain["root_order_id"] == str(monthly_order.root_order_id)
    assert chain["current_order_id"] == lifetime_order_id
    assert [item["order_no"] for item in chain["orders"]] == [
        monthly_order.order_no,
        payload["order"]["order_no"],
    ]
    assert chain["orders"][0]["settlement_status"] == OrderSettlementStatus.CONSUMED.value
    assert chain["orders"][1]["is_current"] is True
    assert chain["orders"][1]["purchase_action"] == "UPGRADE"
    assert chain["value_links"][0]["source_order_id"] == str(monthly_order.id)
    assert chain["value_links"][0]["target_order_id"] == lifetime_order_id
    assert chain["value_links"][0]["relation_type"] == "UPGRADE_TO_LIFETIME"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("cancel", OrderStatus.CANCELED.value), ("close", OrderStatus.TIMEOUT.value)],
)
async def test_pending_lifetime_upgrade_can_be_abandoned_without_poisoning_chain(
    async_client,
    action,
    expected_status,
):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name=f"Pending Lifetime {action}",
        price=Decimal("15.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"pending-lifetime-{action}-{uuid.uuid4().hex[:8]}",
        group_name="取消中的永久升级",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("120.00"),
        duration_days=0,
    )
    now = datetime.utcnow()
    monthly_subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=5),
        end_at=now + timedelta(days=25),
        billing_cycle=BillingCycle.MONTHLY,
    )
    root_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("15.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=monthly_subscription.id,
        paid_at=now - timedelta(days=5),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(monthly_subscription.id),
        },
    )

    first_response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id), "billing_cycle": "LIFETIME"},
        headers=headers,
    )
    assert first_response.status_code == 200, first_response.text
    pending_order = first_response.json()["data"]["order"]

    abandon_response = await async_client.post(
        f"/api/v1/orders/{pending_order['id']}/{action}",
        headers=headers,
    )
    assert abandon_response.status_code == 200, abandon_response.text
    assert abandon_response.json()["data"]["order"]["status"] == expected_status

    preview_response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "LIFETIME"},
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    preview_payload = preview_response.json()
    assert preview_payload["allowed"] is True
    assert preview_payload["action"] == "UPGRADE"

    second_response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id), "billing_cycle": "LIFETIME"},
        headers=headers,
    )
    assert second_response.status_code == 200, second_response.text
    second_order_id = uuid.UUID(second_response.json()["data"]["order"]["id"])

    async with AsyncSessionLocal() as session:
        refreshed_root = await session.get(Order, root_order.id)
        first_db_order = await session.get(Order, uuid.UUID(pending_order["id"]))
        second_db_order = await session.get(Order, second_order_id)

        assert first_db_order.parent_order_id == refreshed_root.id
        assert first_db_order.order_chain_id == refreshed_root.order_chain_id
        assert first_db_order.status.value == expected_status
        assert second_db_order.parent_order_id == refreshed_root.id
        assert second_db_order.order_chain_id == refreshed_root.order_chain_id
        assert second_db_order.status == OrderStatus.CREATED


@pytest.mark.asyncio
async def test_unlimited_refunds_allow_repeated_direct_purchase_cycles(async_client, admin_token):
    await _ensure_refund_settings()
    user = await _create_user(balance=Decimal("180.00"))
    user_token = create_token({"sub": str(user.id)}, token_type="access")
    user_headers = {"Authorization": f"Bearer {user_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    plan = await _create_plan(
        name="Repeat Refund Monthly",
        price=Decimal("20.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"repeat-monthly-{uuid.uuid4().hex[:8]}",
        group_name="重复退款月付",
        tier_level=1,
    )

    refunded_order_ids: list[uuid.UUID] = []
    for _ in range(3):
        create_response = await async_client.post(
            "/api/v1/orders",
            json={"plan_id": str(plan.id)},
            headers=user_headers,
        )
        assert create_response.status_code == 200, create_response.text
        order_id = uuid.UUID(create_response.json()["data"]["order"]["id"])

        await _pay_order_with_balance(async_client, order_id, user_headers)
        await _request_and_approve_balance_refund(
            async_client,
            order_id,
            user_headers,
            admin_headers,
        )
        refunded_order_ids.append(order_id)

    async with AsyncSessionLocal() as session:
        refreshed_user = await session.get(User, user.id)
        refunded_orders = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.id.in_(refunded_order_ids))
                    .order_by(Order.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert float(refreshed_user.balance) == pytest.approx(Decimal("180.00"))
        assert [order.status for order in refunded_orders] == [OrderStatus.REFUNDED] * 3
        assert [order.refund_status for order in refunded_orders] == [RefundStatus.REFUNDED] * 3
        assert [order.settlement_status for order in refunded_orders] == [
            OrderSettlementStatus.REFUNDED
        ] * 3


@pytest.mark.asyncio
async def test_unlimited_refunds_allow_rebuying_lifetime_after_each_refund(
    async_client,
    admin_token,
):
    await _ensure_refund_settings()
    user = await _create_user(balance=Decimal("320.00"))
    user_token = create_token({"sub": str(user.id)}, token_type="access")
    user_headers = {"Authorization": f"Bearer {user_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    plan = await _create_plan(
        name="Repeat Refund Lifetime",
        price=Decimal("18.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"repeat-lifetime-{uuid.uuid4().hex[:8]}",
        group_name="重复退款永久",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
        duration_days=0,
    )

    refunded_lifetime_orders: list[uuid.UUID] = []
    for _ in range(2):
        preview_response = await async_client.get(
            f"/api/v1/plans/{plan.id}/purchase-preview",
            params={"billing_cycle": "LIFETIME"},
            headers=user_headers,
        )
        assert preview_response.status_code == 200, preview_response.text
        preview_payload = preview_response.json()
        assert preview_payload["allowed"] is True
        assert preview_payload["action"] == "DIRECT_PURCHASE"

        create_response = await async_client.post(
            "/api/v1/orders",
            json={"plan_id": str(plan.id), "billing_cycle": "LIFETIME"},
            headers=user_headers,
        )
        assert create_response.status_code == 200, create_response.text
        order_id = uuid.UUID(create_response.json()["data"]["order"]["id"])

        await _pay_order_with_balance(async_client, order_id, user_headers)
        await _request_and_approve_balance_refund(
            async_client,
            order_id,
            user_headers,
            admin_headers,
        )
        refunded_lifetime_orders.append(order_id)

    final_preview_response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=user_headers,
    )
    assert final_preview_response.status_code == 200, final_preview_response.text
    final_preview_payload = final_preview_response.json()
    assert final_preview_payload["allowed"] is True
    assert final_preview_payload["action"] == "DIRECT_PURCHASE"

    async with AsyncSessionLocal() as session:
        refreshed_user = await session.get(User, user.id)
        refunded_orders = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.id.in_(refunded_lifetime_orders))
                    .order_by(Order.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert float(refreshed_user.balance) == pytest.approx(Decimal("320.00"))
        assert [order.status for order in refunded_orders] == [OrderStatus.REFUNDED] * 2
        assert [order.refund_status for order in refunded_orders] == [RefundStatus.REFUNDED] * 2


@pytest.mark.asyncio
async def test_refund_block_uses_order_value_links_even_without_payload_sources(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Link Refund Guard Plan",
        price=Decimal("30.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"link-refund-guard-{uuid.uuid4().hex[:8]}",
        group_name="链式退款拦截",
        tier_level=1,
    )
    now = datetime.utcnow()
    source_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("30.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=now - timedelta(days=2),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
        },
    )
    target_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("70.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=now - timedelta(days=1),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "credit_amount": 30.0,
            "payable_amount": 70.0,
        },
    )

    async with AsyncSessionLocal() as session:
        db_source = await session.get(Order, source_order.id)
        db_target = await session.get(Order, target_order.id)
        db_target.order_chain_id = db_source.order_chain_id
        db_target.root_order_id = db_source.root_order_id
        db_target.parent_order_id = db_source.id
        session.add(db_target)
        session.add(
            OrderValueLink(
                order_chain_id=db_source.order_chain_id,
                source_order_id=db_source.id,
                target_order_id=db_target.id,
                relation_type="UPGRADE_TO_LIFETIME",
                consumed_amount=Decimal("30.00"),
            )
        )
        await session.commit()

    response = await async_client.post(
        f"/api/v1/orders/{source_order.id}/refund",
        json={"refund_to": "balance"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "当前订单价值已用于后续升级，不能重复退款"


@pytest.mark.asyncio
async def test_refunding_latest_order_traverses_order_value_links_without_payload_sources(
    async_client,
):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Link Refund Chain Plan",
        price=Decimal("30.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"link-refund-chain-{uuid.uuid4().hex[:8]}",
        group_name="链式退款联动",
        tier_level=1,
    )
    now = datetime.utcnow()
    source_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("30.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=now - timedelta(days=3),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
        },
    )
    middle_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("70.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=now - timedelta(days=2),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "credit_amount": 30.0,
            "payable_amount": 70.0,
        },
    )
    latest_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("80.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=now - timedelta(days=1),
        refund_status=RefundStatus.PENDING,
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "credit_amount": 100.0,
            "payable_amount": 80.0,
            "refund": {"refund_to": "balance", "money": "180.00", "status": "PENDING"},
        },
    )

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.flush()

        db_source = await session.get(Order, source_order.id)
        db_middle = await session.get(Order, middle_order.id)
        db_latest = await session.get(Order, latest_order.id)

        db_middle.order_chain_id = db_source.order_chain_id
        db_middle.root_order_id = db_source.root_order_id
        db_middle.parent_order_id = db_source.id
        db_latest.order_chain_id = db_source.order_chain_id
        db_latest.root_order_id = db_source.root_order_id
        db_latest.parent_order_id = db_middle.id

        session.add_all([db_middle, db_latest])
        session.add_all(
            [
                OrderValueLink(
                    order_chain_id=db_source.order_chain_id,
                    source_order_id=db_source.id,
                    target_order_id=db_middle.id,
                    relation_type="UPGRADE_TO_LIFETIME",
                    consumed_amount=Decimal("30.00"),
                ),
                OrderValueLink(
                    order_chain_id=db_source.order_chain_id,
                    source_order_id=db_middle.id,
                    target_order_id=db_latest.id,
                    relation_type="UPGRADE_LIFETIME_TIER",
                    consumed_amount=Decimal("100.00"),
                ),
            ]
        )
        await session.commit()
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")

    response = await async_client.post(
        f"/api/v1/admin/orders/{latest_order.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text

    async with AsyncSessionLocal() as session:
        refreshed_source = await session.get(Order, source_order.id)
        refreshed_middle = await session.get(Order, middle_order.id)
        source_refund_state = (refreshed_source.pay_payload or {}).get("refund") or {}
        middle_refund_state = (refreshed_middle.pay_payload or {}).get("refund") or {}

        assert refreshed_source.refund_status == RefundStatus.REFUNDED
        assert refreshed_middle.refund_status == RefundStatus.REFUNDED
        assert refreshed_source.status == OrderStatus.REFUNDED
        assert refreshed_middle.status == OrderStatus.REFUNDED
        assert (
            refreshed_source.settlement_status == OrderSettlementStatus.COVERED_BY_DESCENDANT_REFUND
        )
        assert (
            refreshed_middle.settlement_status == OrderSettlementStatus.COVERED_BY_DESCENDANT_REFUND
        )
        assert source_refund_state.get("covered_by_order_id") == str(latest_order.id)
        assert middle_refund_state.get("covered_by_order_id") == str(latest_order.id)


@pytest.mark.asyncio
async def test_cross_group_purchase_aggregates_current_entitlements(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _noop)

    user = await _create_user()
    basic = await _create_plan(
        name="Basic",
        price=Decimal("10.00"),
        movie_limit=5,
        tv_limit=2,
        group_key="basic-stream",
        group_name="基础服务",
        tier_level=1,
    )
    premium = await _create_plan(
        name="Premium",
        price=Decimal("20.00"),
        movie_limit=20,
        tv_limit=8,
        group_key="premium-stream",
        group_name="高级服务",
        tier_level=1,
    )

    now = datetime.utcnow()
    current_sub = await _create_subscription(
        user_id=user.id,
        plan_id=basic.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=10),
    )
    await _create_vod_request(user_id=user.id, subscription_id=current_sub.id, media_type="MOVIE")

    order = await _create_order(
        user_id=user.id,
        plan_id=premium.id,
        amount=Decimal("20.00"),
        pay_payload={"duration_days": 30, "billing_cycle": "MONTHLY"},
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "TRADE-1", {"money": "20.00"}, session)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        subs = (
            (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.start_at.asc(), Subscription.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(subs) == 2
        assert db_user.vod_movie_limit == 25
        assert db_user.vod_tv_limit == 10
        assert db_user.vod_movie_used == 1
        assert db_user.vod_tv_used == 0
        assert subs[1].start_at <= current_sub.end_at


@pytest.mark.asyncio
async def test_purchase_preview_marks_same_group_upgrade(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic = await _create_plan(
        name="Preview Basic",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="preview-stream",
        group_name="影音会员",
        tier_level=1,
    )
    premium = await _create_plan(
        name="Preview Premium",
        price=Decimal("20.00"),
        movie_limit=8,
        tv_limit=4,
        group_key="preview-stream",
        group_name="影音会员",
        tier_level=2,
    )

    now = datetime.utcnow()
    await _create_subscription(
        user_id=user.id,
        plan_id=basic.id,
        start_at=now - timedelta(days=10),
        end_at=now + timedelta(days=20),
    )

    response = await async_client.get(
        f"/api/v1/plans/{premium.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["action"] == "UPGRADE"
    assert payload["current_subscription"]["plan_id"] == str(basic.id)
    assert payload["target_plan"]["tier_level"] == 2
    assert payload["credit_amount"] > 0
    assert payload["payable_amount"] < payload["base_price"]


@pytest.mark.asyncio
async def test_same_plan_switch_to_lifetime_uses_remaining_credit(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Lifetime Switch Plan",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="lifetime-switch",
        group_name="永久补差价",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
    )

    now = datetime.utcnow()
    await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=10),
        end_at=now + timedelta(days=20),
    )
    await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("10.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=now - timedelta(days=10),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
        },
    )

    response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "LIFETIME"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["action"] == "UPGRADE"
    assert payload["credit_amount"] > 0
    assert payload["payable_amount"] < payload["base_price"]


@pytest.mark.asyncio
async def test_non_refunded_lifetime_blocks_further_purchases_in_group(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Lifetime Block Plan",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="lifetime-block",
        group_name="永久锁定",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
    )

    now = datetime.utcnow()
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
    )
    await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("100.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now - timedelta(days=1),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "duration_days": 0,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(subscription.id),
        },
    )

    preview_response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview_payload = preview_response.json()
    assert preview_payload["allowed"] is False
    assert preview_payload["action"] == "BLOCKED"
    assert "永久套餐" in preview_payload["message"]

    order_response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id), "billing_cycle": "MONTHLY"},
        headers=headers,
    )
    assert order_response.status_code == 400
    assert "永久套餐" in order_response.json()["detail"]


@pytest.mark.asyncio
async def test_unpaid_or_canceled_lifetime_order_does_not_block_repurchase(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Lifetime Pending Plan",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="lifetime-pending",
        group_name="永久未支付",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
    )

    await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("100.00"),
        status=OrderStatus.CANCELED,
        pay_provider=None,
        pay_payload={
            "billing_cycle": "LIFETIME",
            "duration_days": 0,
            "purchase_action": "DIRECT_PURCHASE",
        },
    )

    preview_response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )

    assert preview_response.status_code == 200
    preview_payload = preview_response.json()
    assert preview_payload["allowed"] is True
    assert preview_payload["action"] == "DIRECT_PURCHASE"


@pytest.mark.asyncio
async def test_refunded_lifetime_allows_repurchase_even_if_legacy_status_was_not_synced(
    async_client,
):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Lifetime Rebuy Plan",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="lifetime-rebuy",
        group_name="永久重购",
        tier_level=1,
    )
    await _add_plan_cycle(
        plan_id=plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
    )

    now = datetime.utcnow()
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=2),
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
        status=SubscriptionStatus.CANCELED,
    )
    await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("100.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now - timedelta(days=1),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "duration_days": 0,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(subscription.id),
        },
        refunded_at=now,
        refund_status=RefundStatus.REFUNDED,
    )

    preview_response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )

    assert preview_response.status_code == 200
    preview_payload = preview_response.json()
    assert preview_payload["allowed"] is True
    assert preview_payload["action"] == "DIRECT_PURCHASE"


@pytest.mark.asyncio
async def test_lifetime_plan_can_upgrade_to_higher_tier_lifetime(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic_plan = await _create_plan(
        name="Lifetime Basic",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="lifetime-upgrade",
        group_name="永久升级",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Lifetime Premium",
        price=Decimal("20.00"),
        movie_limit=6,
        tv_limit=3,
        group_key="lifetime-upgrade",
        group_name="永久升级",
        tier_level=2,
    )
    await _add_plan_cycle(
        plan_id=basic_plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("100.00"),
    )
    await _add_plan_cycle(
        plan_id=premium_plan.id,
        billing_cycle=BillingCycle.LIFETIME,
        price=Decimal("180.00"),
    )

    now = datetime.utcnow()
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=basic_plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
    )
    await _create_order(
        user_id=user.id,
        plan_id=basic_plan.id,
        amount=Decimal("100.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now - timedelta(days=1),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "duration_days": 0,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(subscription.id),
        },
    )

    preview_response = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "LIFETIME"},
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview_payload = preview_response.json()
    assert preview_payload["allowed"] is True
    assert preview_payload["action"] == "UPGRADE"
    assert preview_payload["payable_amount"] < preview_payload["base_price"]


@pytest.mark.asyncio
async def test_cycle_table_drives_preview_and_order_amount(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Cycle Driven Plan",
        price=Decimal("99.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="cycle-driven",
        group_name="周期结算",
        tier_level=1,
    )

    async with AsyncSessionLocal() as session:
        db_plan = await session.get(Plan, plan.id)
        db_plan.price = Decimal("15.00")
        db_plan.monthly_price = Decimal("15.00")
        monthly_cycle = await session.scalar(
            select(PlanBillingCycle).where(
                PlanBillingCycle.plan_id == plan.id,
                PlanBillingCycle.billing_cycle == BillingCycle.MONTHLY,
            )
        )
        monthly_cycle.price = Decimal("15.00")
        session.add(db_plan)
        session.add(monthly_cycle)
        await session.commit()

    preview_response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        headers=headers,
    )

    assert preview_response.status_code == 200
    preview_payload = preview_response.json()
    assert preview_payload["billing_cycle"] == "MONTHLY"
    assert preview_payload["base_price"] == 15.0
    assert preview_payload["payable_amount"] == 15.0

    order_response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id)},
        headers=headers,
    )

    assert order_response.status_code == 200
    order_payload = order_response.json()
    assert order_payload["data"]["order"]["amount"] == 15.0


@pytest.mark.asyncio
async def test_same_group_downgrade_is_rejected(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic = await _create_plan(
        name="Downgrade Basic",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="downgrade-stream",
        group_name="影音会员",
        tier_level=1,
    )
    premium = await _create_plan(
        name="Downgrade Premium",
        price=Decimal("20.00"),
        movie_limit=6,
        tv_limit=3,
        group_key="downgrade-stream",
        group_name="影音会员",
        tier_level=2,
    )

    now = datetime.utcnow()
    await _create_subscription(
        user_id=user.id,
        plan_id=premium.id,
        start_at=now - timedelta(days=2),
        end_at=now + timedelta(days=28),
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(basic.id)},
        headers=headers,
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_upgrade_replaces_same_group_subscription_and_keeps_other_groups(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _noop)

    user = await _create_user()
    base = await _create_plan(
        name="Upgrade Base",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="upgrade-stream",
        group_name="影音会员",
        tier_level=1,
    )
    premium = await _create_plan(
        name="Upgrade Premium",
        price=Decimal("20.00"),
        movie_limit=9,
        tv_limit=4,
        group_key="upgrade-stream",
        group_name="影音会员",
        tier_level=2,
    )
    addon = await _create_plan(
        name="Family Addon",
        price=Decimal("8.00"),
        movie_limit=3,
        tv_limit=2,
        group_key="family-addon",
        group_name="家庭扩展",
        tier_level=1,
    )

    now = datetime.utcnow()
    old_group_sub = await _create_subscription(
        user_id=user.id,
        plan_id=base.id,
        start_at=now - timedelta(days=5),
        end_at=now + timedelta(days=25),
    )
    await _create_subscription(
        user_id=user.id,
        plan_id=addon.id,
        start_at=now - timedelta(days=3),
        end_at=now + timedelta(days=27),
    )

    order = await _create_order(
        user_id=user.id,
        plan_id=premium.id,
        amount=Decimal("12.00"),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "UPGRADE",
            "source_subscription_id": str(old_group_sub.id),
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "UPGRADE-1", {"money": "12.00"}, session)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        old_sub = await session.get(Subscription, old_group_sub.id)
        rows = (
            (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        current_premium = [sub for sub in rows if sub.plan_id == premium.id]

        assert old_sub.status == SubscriptionStatus.CANCELED
        assert len(current_premium) == 1
        assert current_premium[0].start_at >= now
        assert db_user.vod_movie_limit == 12
        assert db_user.vod_tv_limit == 6


@pytest.mark.asyncio
async def test_refund_of_future_subscription_recomputes_current_entitlements(
    async_client, admin_token
):
    await _ensure_refund_settings()
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic = await _create_plan(
        name="Basic Refund",
        price=Decimal("10.00"),
        movie_limit=4,
        tv_limit=1,
        group_key="refund-basic",
        group_name="基础服务",
        tier_level=1,
    )
    premium = await _create_plan(
        name="Premium Refund",
        price=Decimal("20.00"),
        movie_limit=10,
        tv_limit=6,
        group_key="refund-premium",
        group_name="高级服务",
        tier_level=1,
    )

    now = datetime.utcnow()
    current_sub = await _create_subscription(
        user_id=user.id,
        plan_id=basic.id,
        start_at=now - timedelta(days=2),
        end_at=now + timedelta(days=8),
    )
    future_sub = await _create_subscription(
        user_id=user.id,
        plan_id=premium.id,
        start_at=now + timedelta(days=8),
        end_at=now + timedelta(days=38),
    )
    await _create_vod_request(user_id=user.id, subscription_id=current_sub.id, media_type="MOVIE")

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        db_user.vod_movie_limit = 10
        db_user.vod_tv_limit = 6
        db_user.vod_movie_used = 0
        db_user.vod_tv_used = 0
        session.add(db_user)
        await session.commit()

    order = await _create_order(
        user_id=user.id,
        plan_id=premium.id,
        amount=Decimal("20.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=future_sub.id,
        paid_at=now,
        pay_payload={"subscription_id": str(future_sub.id)},
    )

    response = await async_client.post(
        f"/api/v1/orders/{order.id}/refund",
        json={"refund_to": "balance"},
        headers=headers,
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        requested_order = await session.get(Order, order.id)
        requested_future = await session.get(Subscription, future_sub.id)
        requested_user = await session.get(User, user.id)
        assert requested_order.status == OrderStatus.COMPLETED
        assert requested_order.refund_status == RefundStatus.PENDING
        assert requested_future.status == SubscriptionStatus.ACTIVE
        assert requested_user.vod_movie_limit == 10
        assert requested_user.vod_tv_limit == 6

    approve_response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert approve_response.status_code == 200

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        refunded_order = await session.get(Order, order.id)
        refreshed_future = await session.get(Subscription, future_sub.id)
        assert db_user.vod_movie_limit == 4
        assert db_user.vod_tv_limit == 1
        assert db_user.vod_movie_used == 1
        assert db_user.vod_tv_used == 0
        assert refunded_order.status == OrderStatus.REFUNDED
        assert refunded_order.refund_status == RefundStatus.REFUNDED
        assert refreshed_future is None


@pytest.mark.asyncio
async def test_user_refund_rejects_duplicate_in_progress(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(name="Refund Guard", price=Decimal("9.90"), movie_limit=2, tv_limit=1)
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        pay_payload={"refund": {"out_refund_no": "RF-EXISTING", "status": "PENDING"}},
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/orders/{order.id}/refund",
        json={"refund_to": "balance"},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "退款处理中，请勿重复提交"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("later_order_status", "later_refund_status"),
    [
        (OrderStatus.COMPLETED, RefundStatus.NONE),
        (OrderStatus.REFUNDED, RefundStatus.REFUNDED),
    ],
)
async def test_consumed_upgrade_order_cannot_be_refunded_twice(
    async_client,
    later_order_status,
    later_refund_status,
):
    await _ensure_refund_settings()
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    group_key = f"refund-chain-{uuid.uuid4().hex[:8]}"

    basic_plan = await _create_plan(
        name="Refund Chain Tier 1",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=group_key,
        group_name="退款链路",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Refund Chain Tier 2",
        price=Decimal("20.00"),
        movie_limit=4,
        tv_limit=2,
        group_key=group_key,
        group_name="退款链路",
        tier_level=2,
    )

    now = datetime.utcnow()
    first_lifetime_sub = await _create_subscription(
        user_id=user.id,
        plan_id=basic_plan.id,
        start_at=now - timedelta(days=2),
        end_at=now + timedelta(days=36500),
        status=SubscriptionStatus.CANCELED,
        billing_cycle=BillingCycle.LIFETIME,
    )
    second_lifetime_sub = await _create_subscription(
        user_id=user.id,
        plan_id=premium_plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
    )

    first_upgrade_order = await _create_order(
        user_id=user.id,
        plan_id=basic_plan.id,
        amount=Decimal("60.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=first_lifetime_sub.id,
        paid_at=now - timedelta(days=2),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "base_amount": 100.0,
            "credit_amount": 40.0,
            "payable_amount": 60.0,
            "subscription_id": str(first_lifetime_sub.id),
        },
    )
    await _create_order(
        user_id=user.id,
        plan_id=premium_plan.id,
        amount=Decimal("80.00"),
        status=later_order_status,
        pay_provider="BALANCE",
        subscription_id=second_lifetime_sub.id,
        paid_at=now - timedelta(days=1),
        refunded_at=now if later_order_status == OrderStatus.REFUNDED else None,
        refund_status=later_refund_status,
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "base_amount": 180.0,
            "credit_amount": 100.0,
            "payable_amount": 80.0,
            "subscription_id": str(second_lifetime_sub.id),
            "source_subscription_ids": [str(first_lifetime_sub.id)],
        },
    )

    response = await async_client.post(
        f"/api/v1/orders/{first_upgrade_order.id}/refund",
        json={"refund_to": "balance"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "当前订单价值已用于后续升级，不能重复退款"


@pytest.mark.asyncio
async def test_user_refund_renewal_order_rolls_back_duration_instead_of_cancel(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Renew Refund Plan",
        price=Decimal("9.90"),
        movie_limit=2,
        tv_limit=1,
        group_key="renew-refund",
        group_name="续费退款",
        tier_level=1,
    )
    now = datetime.utcnow()
    original_end_at = now + timedelta(days=20)
    renewed_end_at = original_end_at + timedelta(days=30)
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=10),
        end_at=renewed_end_at,
    )
    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")
    await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now - timedelta(days=10),
        pay_payload={
            "purchase_action": "DIRECT_PURCHASE",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
        },
    )
    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now,
        pay_payload={
            "purchase_action": "RENEW",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
            "renewal_of_subscription_id": str(subscription.id),
            "refund": {"refund_to": "balance", "money": "9.90", "status": "PENDING"},
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        source_orders = (
            (await session.execute(select(Order).where(Order.subscription_id == subscription.id)))
            .scalars()
            .all()
        )
        original_order = next(
            order_item
            for order_item in source_orders
            if (order_item.pay_payload or {}).get("purchase_action") == "DIRECT_PURCHASE"
        )
        refunded_order = await session.get(Order, order.id)
        updated_subscription = await session.get(Subscription, subscription.id)
        assert original_order.status == OrderStatus.COMPLETED
        assert original_order.refund_status == RefundStatus.NONE
        assert refunded_order.status == OrderStatus.REFUNDED
        assert refunded_order.refund_status == RefundStatus.REFUNDED
        assert updated_subscription.status == SubscriptionStatus.ACTIVE
        assert updated_subscription.end_at == original_end_at


@pytest.mark.asyncio
async def test_refunding_one_renewal_does_not_mark_other_same_subscription_orders_refunded(
    async_client,
):
    await _ensure_refund_settings()
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    plan = await _create_plan(
        name="Renew Refund Isolation Plan",
        price=Decimal("9.90"),
        movie_limit=2,
        tv_limit=1,
        group_key="renew-refund-isolation",
        group_name="续费退款隔离",
        tier_level=1,
    )
    now = datetime.utcnow()
    start_at = now - timedelta(days=20)
    first_end_at = start_at + timedelta(days=30)
    second_end_at = first_end_at + timedelta(days=30)
    third_end_at = second_end_at + timedelta(days=30)
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=start_at,
        end_at=third_end_at,
    )

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")

    original_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=start_at,
        pay_payload={
            "purchase_action": "DIRECT_PURCHASE",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
        },
    )
    refunded_renewal = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=first_end_at,
        pay_payload={
            "purchase_action": "RENEW",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
            "renewal_of_subscription_id": str(subscription.id),
            "refund": {"refund_to": "balance", "money": "9.90", "status": "PENDING"},
        },
        refund_status=RefundStatus.PENDING,
    )
    later_renewal = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=second_end_at,
        pay_payload={
            "purchase_action": "RENEW",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
            "renewal_of_subscription_id": str(subscription.id),
        },
    )

    response = await async_client.post(
        f"/api/v1/admin/orders/{refunded_renewal.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text

    async with AsyncSessionLocal() as session:
        refreshed_original = await session.get(Order, original_order.id)
        refreshed_refunded = await session.get(Order, refunded_renewal.id)
        refreshed_later = await session.get(Order, later_renewal.id)

        assert refreshed_original.status == OrderStatus.COMPLETED
        assert refreshed_original.refund_status == RefundStatus.NONE
        assert refreshed_later.status == OrderStatus.COMPLETED
        assert refreshed_later.refund_status == RefundStatus.NONE
        assert refreshed_refunded.status == OrderStatus.REFUNDED
        assert refreshed_refunded.refund_status == RefundStatus.REFUNDED

    later_refund_response = await async_client.post(
        f"/api/v1/orders/{later_renewal.id}/refund",
        json={"refund_to": "balance"},
        headers=headers,
    )

    assert later_refund_response.status_code == 200, later_refund_response.text


@pytest.mark.asyncio
async def test_user_refund_original_order_keeps_subscription_if_later_renewal_exists(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Original Refund Plan",
        price=Decimal("9.90"),
        movie_limit=2,
        tv_limit=1,
        group_key="original-refund",
        group_name="首单退款",
        tier_level=1,
    )
    now = datetime.utcnow()
    start_at = now - timedelta(days=5)
    first_end_at = start_at + timedelta(days=30)
    renewed_end_at = first_end_at + timedelta(days=30)
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=start_at,
        end_at=renewed_end_at,
    )
    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")

    first_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=start_at,
        pay_payload={
            "purchase_action": "DIRECT_PURCHASE",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
            "refund": {"refund_to": "balance", "money": "9.90", "status": "PENDING"},
        },
    )
    await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("9.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=first_end_at,
        pay_payload={
            "purchase_action": "RENEW",
            "duration_days": 30,
            "subscription_id": str(subscription.id),
            "renewal_of_subscription_id": str(subscription.id),
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, first_order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{first_order.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        refunded_order = await session.get(Order, first_order.id)
        updated_subscription = await session.get(Subscription, subscription.id)
        assert refunded_order.status == OrderStatus.REFUNDED
        assert refunded_order.refund_status == RefundStatus.REFUNDED
        assert updated_subscription.status == SubscriptionStatus.ACTIVE
        assert updated_subscription.end_at == first_end_at


@pytest.mark.asyncio
async def test_upgrade_to_lifetime_balance_refund_returns_credit_and_cash(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Lifetime Refund Upgrade",
        price=Decimal("20.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="lifetime-refund-balance",
        group_name="永久退款余额",
        tier_level=1,
    )
    now = datetime.utcnow()
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now,
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
    )

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("60.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=subscription.id,
        paid_at=now,
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "base_amount": 100.0,
            "credit_amount": 40.0,
            "payable_amount": 60.0,
            "subscription_id": str(subscription.id),
            "refund": {"refund_to": "balance", "money": "100.00", "status": "PENDING"},
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        refunded_order = await session.get(Order, order.id)
        updated_user = await session.get(User, user.id)
        updated_subscription = await session.get(Subscription, subscription.id)
        refund_state = (refunded_order.pay_payload or {}).get("refund") or {}
        assert updated_user.balance == pytest.approx(100.0)
        assert refunded_order.status == OrderStatus.REFUNDED
        assert refunded_order.refund_status == RefundStatus.REFUNDED
        assert refund_state.get("money") == "100.00"
        assert refund_state.get("provider_money") == "60.00"
        assert refund_state.get("balance_credit_money") == "40.00"
        assert updated_subscription is None


@pytest.mark.asyncio
async def test_upgrade_to_lifetime_original_refund_splits_provider_and_balance_credit(
    async_client, monkeypatch
):
    await _ensure_epay_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Lifetime Refund Original",
        price=Decimal("20.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="lifetime-refund-original",
        group_name="永久退款原路",
        tier_level=1,
    )
    now = datetime.utcnow()
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now,
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
    )

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("60.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        subscription_id=subscription.id,
        paid_at=now,
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "base_amount": 100.0,
            "credit_amount": 40.0,
            "payable_amount": 60.0,
            "subscription_id": str(subscription.id),
            "refund": {"refund_to": "original", "money": "100.00", "status": "PENDING"},
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    captured = {}

    async def _fake_epay_refund(*args, **kwargs):
        captured["money"] = kwargs["money"]
        return {"refund_no": "RF-DEMO"}

    async def _fake_epay_refund_query(*args, **kwargs):
        return {"status": "1"}

    monkeypatch.setattr("app.services.refunds.epay_refund", _fake_epay_refund)
    monkeypatch.setattr("app.services.refunds.epay_refund_query", _fake_epay_refund_query)

    approve_response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "original"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve_response.status_code == 200
    assert captured["money"] == "60.00"

    query_response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund/query",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert query_response.status_code == 200

    async with AsyncSessionLocal() as session:
        refunded_order = await session.get(Order, order.id)
        updated_user = await session.get(User, user.id)
        updated_subscription = await session.get(Subscription, subscription.id)
        refund_state = (refunded_order.pay_payload or {}).get("refund") or {}
        balance_tx = (
            await session.execute(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user.id,
                    BalanceTransaction.reason == "REFUND_CREDIT",
                )
            )
        ).scalar_one()

        assert updated_user.balance == pytest.approx(40.0)
        assert refunded_order.status == OrderStatus.REFUNDED
        assert refunded_order.refund_status == RefundStatus.REFUNDED
        assert refund_state.get("money") == "100.00"
        assert refund_state.get("provider_money") == "60.00"
        assert refund_state.get("balance_credit_money") == "40.00"
        assert balance_tx.delta == pytest.approx(40.0)
        assert updated_subscription is None


@pytest.mark.asyncio
async def test_refunding_latest_upgrade_marks_all_related_orders_as_refunded(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Refund Chain Base",
        price=Decimal("10.00"),
        movie_limit=2,
        tv_limit=1,
        group_key=f"refund-mark-chain-{uuid.uuid4().hex[:8]}",
        group_name="退款链路标记",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Refund Chain Premium",
        price=Decimal("20.00"),
        movie_limit=4,
        tv_limit=2,
        group_key=plan.group_key,
        group_name="退款链路标记",
        tier_level=2,
    )
    now = datetime.utcnow()
    base_sub = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=30),
        end_at=now - timedelta(days=1),
        status=SubscriptionStatus.CANCELED,
    )
    first_lifetime_sub = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=36500),
        status=SubscriptionStatus.CANCELED,
        billing_cycle=BillingCycle.LIFETIME,
    )
    latest_lifetime_sub = await _create_subscription(
        user_id=user.id,
        plan_id=premium_plan.id,
        start_at=now,
        end_at=now + timedelta(days=36500),
        billing_cycle=BillingCycle.LIFETIME,
    )

    first_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("10.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=base_sub.id,
        paid_at=now - timedelta(days=30),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
            "subscription_id": str(base_sub.id),
        },
    )
    second_order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("90.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=first_lifetime_sub.id,
        paid_at=now - timedelta(days=1),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "base_amount": 100.0,
            "credit_amount": 10.0,
            "payable_amount": 90.0,
            "subscription_id": str(first_lifetime_sub.id),
            "source_subscription_ids": [str(base_sub.id)],
        },
    )

    async with AsyncSessionLocal() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:8]}",
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="test",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        admin_token = create_token({"sub": str(admin.id)}, token_type="access")

    latest_order = await _create_order(
        user_id=user.id,
        plan_id=premium_plan.id,
        amount=Decimal("80.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        subscription_id=latest_lifetime_sub.id,
        paid_at=now,
        pay_payload={
            "billing_cycle": "LIFETIME",
            "purchase_action": "UPGRADE",
            "base_amount": 180.0,
            "credit_amount": 100.0,
            "payable_amount": 80.0,
            "subscription_id": str(latest_lifetime_sub.id),
            "source_subscription_ids": [str(first_lifetime_sub.id)],
            "refund": {"refund_to": "balance", "money": "180.00", "status": "PENDING"},
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, latest_order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{latest_order.id}/refund",
        json={"refund_to": "balance"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        refreshed_first_order = await session.get(Order, first_order.id)
        refreshed_second_order = await session.get(Order, second_order.id)
        refund_state_first = (refreshed_first_order.pay_payload or {}).get("refund") or {}
        refund_state_second = (refreshed_second_order.pay_payload or {}).get("refund") or {}

        assert refreshed_first_order.refund_status == RefundStatus.REFUNDED
        assert refreshed_second_order.refund_status == RefundStatus.REFUNDED
        assert refund_state_first.get("covered_by_order_id") == str(latest_order.id)
        assert refund_state_second.get("covered_by_order_id") == str(latest_order.id)


@pytest.mark.asyncio
async def test_expired_subscription_cleanup_respects_retention_window():
    await _ensure_subscription_retention_days(30)
    user = await _create_user()
    plan = await _create_plan(
        name="Expired Retention Plan",
        price=Decimal("9.90"),
        movie_limit=2,
        tv_limit=1,
        group_key="expired-retention",
        group_name="过期保留",
        tier_level=1,
    )
    now = datetime.utcnow()
    old_expired = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=90),
        end_at=now - timedelta(days=40),
        status=SubscriptionStatus.EXPIRED,
    )
    recent_expired = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=20),
        end_at=now - timedelta(days=5),
        status=SubscriptionStatus.EXPIRED,
    )

    async with AsyncSessionLocal() as session:
        deleted, affected = await cleanup_expired_subscription_data(session, now=now)
        assert deleted == 1
        assert affected == 1

    async with AsyncSessionLocal() as session:
        assert await session.get(Subscription, old_expired.id) is None
        assert await session.get(Subscription, recent_expired.id) is not None


@pytest.mark.asyncio
async def test_admin_refund_rejects_amount_above_order_amount(async_client, admin_token):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Admin Refund", price=Decimal("12.00"), movie_limit=3, tv_limit=1
    )
    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("12.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=datetime.utcnow(),
        pay_payload={"refund": {"refund_to": "balance", "money": "12.00", "status": "PENDING"}},
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "balance", "money": "20.00"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "退款金额不能超过订单可退金额"


@pytest.mark.asyncio
async def test_admin_refund_rejects_partial_amount(async_client, admin_token):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Admin Partial Refund", price=Decimal("12.00"), movie_limit=3, tv_limit=1
    )
    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("12.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=datetime.utcnow(),
        pay_payload={"refund": {"refund_to": "balance", "money": "12.00", "status": "PENDING"}},
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "balance", "money": "1.00"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "当前仅支持全额退款"


@pytest.mark.asyncio
async def test_admin_can_reject_pending_refund(async_client, admin_token):
    await _ensure_refund_settings()
    user = await _create_user()
    plan = await _create_plan(
        name="Admin Reject Refund", price=Decimal("12.00"), movie_limit=3, tv_limit=1
    )
    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("12.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        pay_payload={"refund": {"refund_to": "original", "money": "12.00", "status": "PENDING"}},
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund/reject",
        json={"reason": "manual review failed"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200

    async with AsyncSessionLocal() as session:
        rejected_order = await session.get(Order, order.id)
        assert rejected_order.refund_status == RefundStatus.REJECTED
        assert rejected_order.refund_reject_reason == "manual review failed"


@pytest.mark.asyncio
async def test_balance_payment_rejects_recharge_order(async_client):
    user = await _create_user(balance=Decimal("50.00"))
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await async_client.post(
        "/api/v1/orders/recharge",
        json={"amount": "10.00"},
        headers=headers,
    )
    assert create_resp.status_code == 200
    order_id = create_resp.json()["data"]["order"]["id"]

    pay_resp = await async_client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"pay_type": "balance"},
        headers=headers,
    )

    assert pay_resp.status_code == 400
    assert pay_resp.json()["detail"] == "充值订单不支持余额支付"


@pytest.mark.asyncio
async def test_user_refund_rejects_recharge_orders(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("30.00"),
        order_type=OrderType.RECHARGE,
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        pay_payload={"recharge_amount": 30.0},
    )

    response = await async_client.post(
        f"/api/v1/orders/{order.id}/refund",
        json={"refund_to": "original"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "充值订单暂不支持退款，请联系管理员处理"


@pytest.mark.asyncio
async def test_admin_refund_rejects_recharge_orders(async_client, admin_token):
    await _ensure_refund_settings()
    user = await _create_user()
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("30.00"),
        order_type=OrderType.RECHARGE,
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        pay_payload={
            "recharge_amount": 30.0,
            "refund": {"refund_to": "original", "money": "30.00", "status": "PENDING"},
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        db_order.refund_status = RefundStatus.PENDING
        session.add(db_order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order.id}/refund",
        json={"refund_to": "original"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "充值订单暂不支持退款，请联系管理员处理"


@pytest.mark.asyncio
async def test_balance_payment_rolls_back_when_fulfillment_fails(async_client, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("emby failed")

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _boom)

    user = await _create_user(balance=Decimal("50.00"))
    plan = await _create_plan(
        name="Balance Purchase", price=Decimal("9.90"), movie_limit=5, tv_limit=2
    )
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(plan.id)},
        headers=headers,
    )
    assert create_resp.status_code == 200
    order_id = create_resp.json()["data"]["order"]["id"]

    with pytest.raises(RuntimeError, match="emby failed"):
        await async_client.post(
            f"/api/v1/orders/{order_id}/pay",
            json={"pay_type": "balance"},
            headers=headers,
        )

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        db_order = await session.get(Order, uuid.UUID(order_id))
        txns = (
            (
                await session.execute(
                    select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        subs = (
            (await session.execute(select(Subscription).where(Subscription.user_id == user.id)))
            .scalars()
            .all()
        )
        assert float(db_user.balance) == 50.0
        assert db_order.status == OrderStatus.CREATED
        assert db_order.pay_provider is None
        assert txns == []
        assert subs == []


@pytest.mark.asyncio
async def test_replace_trial_cancels_trial_and_credits_surplus_balance(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _noop)

    user = await _create_user()
    plan = await _create_plan(
        name="Trial Upgrade",
        price=Decimal("6.00"),
        movie_limit=3,
        tv_limit=1,
        group_key="trial-upgrade",
        group_name="观影计划",
        tier_level=1,
    )

    async with AsyncSessionLocal() as session:
        db_plan = await session.get(Plan, plan.id)
        db_plan.trial_price = Decimal("12.00")
        db_plan.trial_days = 30
        session.add(db_plan)
        await session.commit()

    now = datetime.utcnow()
    trial_sub = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=10),
        end_at=now + timedelta(days=20),
    )
    async with AsyncSessionLocal() as session:
        db_trial_sub = await session.get(Subscription, trial_sub.id)
        db_trial_sub.billing_cycle = "TRIAL"
        session.add(db_trial_sub)
        await session.commit()

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("0.00"),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "REPLACE_TRIAL",
            "source_subscription_id": str(trial_sub.id),
            "source_subscription_ids": [str(trial_sub.id)],
            "carry_balance_amount": 2.0,
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "TRIAL-UPGRADE-1", {"money": "0.00"}, session)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        db_trial_sub = await session.get(Subscription, trial_sub.id)
        rows = (
            (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        balance_txns = (
            (
                await session.execute(
                    select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )

        assert db_trial_sub.status == SubscriptionStatus.CANCELED
        assert any(sub.id != trial_sub.id and sub.plan_id == plan.id for sub in rows)
        assert float(db_user.balance) == 2.0
        assert any(txn.reason == "PLAN_UPGRADE_CREDIT" for txn in balance_txns)


@pytest.mark.asyncio
async def test_renewal_appends_after_current_subscription(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _noop)

    user = await _create_user()
    plan = await _create_plan(
        name="Renew Plan",
        price=Decimal("15.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="renew-stream",
        group_name="续费计划",
        tier_level=1,
    )

    now = datetime.utcnow()
    current_sub = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=now - timedelta(days=5),
        end_at=now + timedelta(days=25),
    )

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("15.00"),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "RENEW",
            "source_subscription_id": str(current_sub.id),
            "renewal_of_subscription_id": str(current_sub.id),
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "RENEW-1", {"money": "15.00"}, session)

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        renewed = rows[-1]
        assert len(rows) == 1
        assert renewed.id == current_sub.id
        assert renewed.start_at == current_sub.start_at
        assert renewed.end_at == get_cycle_end_at(current_sub.end_at, BillingCycle.MONTHLY)


@pytest.mark.asyncio
async def test_trial_plan_cannot_renew_but_can_upgrade_to_higher_trial(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    trial_plan = await _create_plan(
        name="Trial Tier 1",
        price=Decimal("15.00"),
        movie_limit=1,
        tv_limit=0,
        group_key="trial-flow",
        group_name="试用升级链路",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Premium Tier 2",
        price=Decimal("25.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="trial-flow",
        group_name="试用升级链路",
        tier_level=2,
    )

    async with AsyncSessionLocal() as session:
        db_trial_plan = await session.get(Plan, trial_plan.id)
        db_trial_plan.trial_price = Decimal("1.00")
        db_trial_plan.trial_days = 7
        db_premium_plan = await session.get(Plan, premium_plan.id)
        db_premium_plan.trial_price = Decimal("5.00")
        db_premium_plan.trial_days = 7
        session.add(
            PlanBillingCycle(
                plan_id=trial_plan.id,
                billing_cycle=BillingCycle.TRIAL,
                price=Decimal("1.00"),
                duration_days=7,
                is_default=False,
                sort_order=10,
            )
        )
        session.add(
            PlanBillingCycle(
                plan_id=premium_plan.id,
                billing_cycle=BillingCycle.TRIAL,
                price=Decimal("5.00"),
                duration_days=7,
                is_default=False,
                sort_order=10,
            )
        )
        session.add(db_trial_plan)
        session.add(db_premium_plan)
        await session.commit()

    now = datetime.utcnow()
    trial_sub = await _create_subscription(
        user_id=user.id,
        plan_id=trial_plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=6),
    )
    async with AsyncSessionLocal() as session:
        db_trial_sub = await session.get(Subscription, trial_sub.id)
        db_trial_sub.billing_cycle = BillingCycle.TRIAL
        session.add(db_trial_sub)
        await session.commit()

    renew_preview = await async_client.get(
        f"/api/v1/plans/{trial_plan.id}/purchase-preview",
        params={"billing_cycle": "TRIAL"},
        headers=headers,
    )
    assert renew_preview.status_code == 200
    renew_payload = renew_preview.json()
    assert renew_payload["allowed"] is False
    assert renew_payload["action"] == "BLOCKED"

    replace_preview = await async_client.get(
        f"/api/v1/plans/{trial_plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )
    assert replace_preview.status_code == 200
    replace_payload = replace_preview.json()
    assert replace_payload["allowed"] is True
    assert replace_payload["action"] == "REPLACE_TRIAL"

    upgrade_preview = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )
    assert upgrade_preview.status_code == 200
    upgrade_payload = upgrade_preview.json()
    assert upgrade_payload["allowed"] is True
    assert upgrade_payload["action"] == "UPGRADE"

    trial_upgrade_preview = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "TRIAL"},
        headers=headers,
    )
    assert trial_upgrade_preview.status_code == 200
    trial_upgrade_payload = trial_upgrade_preview.json()
    assert trial_upgrade_payload["allowed"] is True
    assert trial_upgrade_payload["action"] == "UPGRADE"
    assert trial_upgrade_payload["billing_cycle"] == "TRIAL"

    trial_upgrade_order = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(premium_plan.id), "billing_cycle": "TRIAL"},
        headers=headers,
    )
    assert trial_upgrade_order.status_code == 200


@pytest.mark.asyncio
async def test_formal_purchase_in_group_blocks_future_trial_purchase(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic_plan = await _create_plan(
        name="Formal Tier 1",
        price=Decimal("15.00"),
        movie_limit=1,
        tv_limit=0,
        group_key="trial-blocked-by-formal",
        group_name="Formal Blocks Trial",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Formal Tier 2",
        price=Decimal("25.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="trial-blocked-by-formal",
        group_name="Formal Blocks Trial",
        tier_level=2,
    )

    async with AsyncSessionLocal() as session:
        db_premium_plan = await session.get(Plan, premium_plan.id)
        db_premium_plan.trial_price = Decimal("5.00")
        db_premium_plan.trial_days = 7
        session.add(
            PlanBillingCycle(
                plan_id=premium_plan.id,
                billing_cycle=BillingCycle.TRIAL,
                price=Decimal("5.00"),
                duration_days=7,
                is_default=False,
                sort_order=10,
            )
        )
        session.add(db_premium_plan)
        await session.commit()

    now = datetime.utcnow()
    await _create_subscription(
        user_id=user.id,
        plan_id=basic_plan.id,
        start_at=now - timedelta(days=2),
        end_at=now + timedelta(days=28),
    )

    response = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "TRIAL"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["action"] == "BLOCKED"
    assert payload["billing_cycle"] == "TRIAL"


@pytest.mark.asyncio
async def test_formal_order_history_blocks_trial_even_after_subscription_record_removed(
    async_client,
):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic_plan = await _create_plan(
        name="Formal History Tier 1",
        price=Decimal("15.00"),
        movie_limit=1,
        tv_limit=0,
        group_key="trial-blocked-by-order-history",
        group_name="Formal History Blocks Trial",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Formal History Tier 2",
        price=Decimal("25.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="trial-blocked-by-order-history",
        group_name="Formal History Blocks Trial",
        tier_level=2,
    )

    async with AsyncSessionLocal() as session:
        db_premium_plan = await session.get(Plan, premium_plan.id)
        db_premium_plan.trial_price = Decimal("5.00")
        db_premium_plan.trial_days = 7
        session.add(
            PlanBillingCycle(
                plan_id=premium_plan.id,
                billing_cycle=BillingCycle.TRIAL,
                price=Decimal("5.00"),
                duration_days=7,
                is_default=False,
                sort_order=10,
            )
        )
        session.add(db_premium_plan)
        await session.commit()

    await _create_order(
        user_id=user.id,
        plan_id=basic_plan.id,
        amount=Decimal("15.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=datetime.utcnow(),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "DIRECT_PURCHASE",
        },
    )

    response = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "TRIAL"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["action"] == "BLOCKED"
    assert payload["billing_cycle"] == "TRIAL"


@pytest.mark.asyncio
async def test_formal_purchase_in_group_blocks_trial_order_creation(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    basic_plan = await _create_plan(
        name="Order Block Formal Tier 1",
        price=Decimal("15.00"),
        movie_limit=1,
        tv_limit=0,
        group_key="trial-order-blocked-by-formal",
        group_name="Order Block Formal Trial",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Order Block Formal Tier 2",
        price=Decimal("25.00"),
        movie_limit=4,
        tv_limit=2,
        group_key="trial-order-blocked-by-formal",
        group_name="Order Block Formal Trial",
        tier_level=2,
    )

    async with AsyncSessionLocal() as session:
        db_premium_plan = await session.get(Plan, premium_plan.id)
        db_premium_plan.trial_price = Decimal("5.00")
        db_premium_plan.trial_days = 7
        session.add(
            PlanBillingCycle(
                plan_id=premium_plan.id,
                billing_cycle=BillingCycle.TRIAL,
                price=Decimal("5.00"),
                duration_days=7,
                is_default=False,
                sort_order=10,
            )
        )
        session.add(db_premium_plan)
        await session.commit()

    now = datetime.utcnow()
    await _create_subscription(
        user_id=user.id,
        plan_id=basic_plan.id,
        start_at=now - timedelta(days=2),
        end_at=now + timedelta(days=28),
    )

    response = await async_client.post(
        "/api/v1/orders",
        json={"plan_id": str(premium_plan.id), "billing_cycle": "TRIAL"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "正式套餐" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trial_preview_respects_user_trial_block_flag(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Blocked Trial Plan",
        price=Decimal("15.00"),
        movie_limit=1,
        tv_limit=0,
        group_key="trial-user-flag",
        group_name="Trial User Flag",
        tier_level=1,
    )

    async with AsyncSessionLocal() as session:
        db_plan = await session.get(Plan, plan.id)
        db_plan.trial_price = Decimal("1.00")
        db_plan.trial_days = 7
        db_user = await session.get(User, user.id)
        db_user.trial_used = True
        session.add(
            PlanBillingCycle(
                plan_id=plan.id,
                billing_cycle=BillingCycle.TRIAL,
                price=Decimal("1.00"),
                duration_days=7,
                is_default=False,
                sort_order=10,
            )
        )
        session.add(db_plan)
        session.add(db_user)
        await session.commit()

    response = await async_client.get(
        f"/api/v1/plans/{plan.id}/purchase-preview",
        params={"billing_cycle": "TRIAL"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["action"] == "BLOCKED"


@pytest.mark.asyncio
async def test_upgrade_with_lower_target_price_carries_surplus_to_balance(
    monkeypatch, async_client
):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _noop)

    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    base_plan = await _create_plan(
        name="Expensive Lv1",
        price=Decimal("30.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="surplus-upgrade",
        group_name="补差价测试",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Cheaper Lv2",
        price=Decimal("10.00"),
        movie_limit=5,
        tv_limit=3,
        group_key="surplus-upgrade",
        group_name="补差价测试",
        tier_level=2,
    )

    now = datetime.utcnow()
    current_sub = await _create_subscription(
        user_id=user.id,
        plan_id=base_plan.id,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=29),
    )

    preview_response = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["allowed"] is True
    assert preview["action"] == "UPGRADE"
    assert preview["payable_amount"] == 0.0
    assert preview["carry_balance_amount"] > 0.0

    order = await _create_order(
        user_id=user.id,
        plan_id=premium_plan.id,
        amount=Decimal("0.00"),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "UPGRADE",
            "base_amount": preview["base_price"],
            "credit_amount": preview["credit_amount"],
            "payable_amount": preview["payable_amount"],
            "carry_balance_amount": preview["carry_balance_amount"],
            "source_subscription_id": str(current_sub.id),
            "source_subscription_ids": [str(current_sub.id)],
        },
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "UPGRADE-SURPLUS-1", {"money": "0.00"}, session)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        db_old_sub = await session.get(Subscription, current_sub.id)
        rows = (
            (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        upgraded = [row for row in rows if row.plan_id == premium_plan.id]
        assert db_old_sub.status == SubscriptionStatus.CANCELED
        assert len(upgraded) == 1
        assert float(db_user.balance) == pytest.approx(preview["carry_balance_amount"])


@pytest.mark.asyncio
async def test_upgrade_to_shorter_cycle_restarts_term_instead_of_extending(
    monkeypatch, async_client
):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(payment_service, "ensure_emby_accounts_for_user", _noop)

    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    base_plan = await _create_plan(
        name="Yearly Basic",
        price=Decimal("120.00"),
        movie_limit=2,
        tv_limit=1,
        group_key="upgrade-shorter-cycle",
        group_name="升级短周期测试",
        tier_level=1,
    )
    premium_plan = await _create_plan(
        name="Monthly Premium",
        price=Decimal("20.00"),
        movie_limit=8,
        tv_limit=4,
        group_key="upgrade-shorter-cycle",
        group_name="升级短周期测试",
        tier_level=2,
    )

    now = datetime.utcnow()
    old_end_at = now + timedelta(days=320)
    current_sub = await _create_subscription(
        user_id=user.id,
        plan_id=base_plan.id,
        start_at=now - timedelta(days=45),
        end_at=old_end_at,
    )

    preview_response = await async_client.get(
        f"/api/v1/plans/{premium_plan.id}/purchase-preview",
        params={"billing_cycle": "MONTHLY"},
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["allowed"] is True
    assert preview["action"] == "UPGRADE"

    order = await _create_order(
        user_id=user.id,
        plan_id=premium_plan.id,
        amount=Decimal(str(preview["payable_amount"])),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 30,
            "purchase_action": "UPGRADE",
            "base_amount": preview["base_price"],
            "credit_amount": preview["credit_amount"],
            "payable_amount": preview["payable_amount"],
            "carry_balance_amount": preview["carry_balance_amount"],
            "source_subscription_id": str(current_sub.id),
            "source_subscription_ids": [str(current_sub.id)],
        },
    )

    paid_before = datetime.utcnow()
    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "UPGRADE-SHORTER-1", {"money": "0.00"}, session)
    paid_after = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        old_sub = await session.get(Subscription, current_sub.id)
        rows = (
            (
                await session.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        upgraded = next(sub for sub in rows if sub.plan_id == premium_plan.id)

        assert old_sub.status == SubscriptionStatus.CANCELED
        assert old_sub.end_at <= paid_after
        assert upgraded.status == SubscriptionStatus.ACTIVE
        assert paid_before <= upgraded.start_at <= paid_after
        assert upgraded.end_at > upgraded.start_at
        assert upgraded.end_at < old_end_at
        assert upgraded.end_at == get_cycle_end_at(upgraded.start_at, BillingCycle.MONTHLY)

    subscriptions_response = await async_client.get("/api/v1/plans/me", headers=headers)
    assert subscriptions_response.status_code == 200
    payload = subscriptions_response.json()
    assert all(item["status"] != "CANCELED" for item in payload)
    assert any(item["plan_id"] == str(premium_plan.id) for item in payload)
    assert all(item["plan_id"] != str(base_plan.id) for item in payload)


@pytest.mark.asyncio
async def test_recharge_order_increases_balance():
    user = await _create_user(balance=Decimal("5.00"))

    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("30.00"),
        order_type=OrderType.RECHARGE,
        pay_payload={"recharge_amount": 30.0},
    )

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order.id)
        await handle_paid_order(db_order, "RECHARGE-1", {"money": "30.00"}, session)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        db_order = await session.get(Order, order.id)
        txns = (
            (
                await session.execute(
                    select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert float(db_user.balance) == 35.0
        assert db_order.status == OrderStatus.COMPLETED
        assert any(txn.reason == "RECHARGE" for txn in txns)


@pytest.mark.asyncio
async def test_user_orders_include_purchase_action_and_amount_breakdown(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Order Summary Plan",
        price=Decimal("30.00"),
        movie_limit=3,
        tv_limit=1,
        group_key="order-summary",
        group_name="订单摘要测试",
        tier_level=2,
    )

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("12.50"),
        pay_payload={
            "billing_cycle": "MONTHLY",
            "duration_days": 31,
            "purchase_action": "UPGRADE",
            "base_amount": 30.0,
            "credit_amount": 17.5,
            "payable_amount": 12.5,
            "carry_balance_amount": 0.0,
        },
    )

    list_response = await async_client.get("/api/v1/orders", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.json()
    record = next(item for item in list_payload if item["id"] == str(order.id))
    assert record["purchase_action"] == "UPGRADE"
    assert record["purchase_action_label"] == "升级订阅"
    assert record["base_amount"] == 30.0
    assert record["credit_amount"] == 17.5
    assert record["payable_amount"] == 12.5

    detail_response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["order"]["purchase_action"] == "UPGRADE"
    assert detail_payload["order"]["duration_days"] == 31


@pytest.mark.asyncio
async def test_upgrade_to_lifetime_uses_period_to_lifetime_label(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Lifetime Label Plan",
        price=Decimal("88.00"),
        movie_limit=3,
        tv_limit=1,
        group_key="lifetime-label",
        group_name="永久文案测试",
        tier_level=2,
    )

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("58.00"),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "source_billing_cycle": "MONTHLY",
            "duration_days": 0,
            "purchase_action": "UPGRADE",
            "base_amount": 88.0,
            "credit_amount": 30.0,
            "payable_amount": 58.0,
            "carry_balance_amount": 0.0,
        },
    )

    list_response = await async_client.get("/api/v1/orders", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.json()
    record = next(item for item in list_payload if item["id"] == str(order.id))
    assert record["purchase_action"] == "UPGRADE"
    assert record["purchase_action_label"] == "周期转永久"

    detail_response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["order"]["purchase_action"] == "UPGRADE"
    assert detail_payload["order"]["purchase_action_label"] == "周期转永久"


@pytest.mark.asyncio
async def test_replace_trial_with_lifetime_uses_trial_to_lifetime_label(async_client):
    user = await _create_user()
    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    plan = await _create_plan(
        name="Trial To Lifetime Label Plan",
        price=Decimal("88.00"),
        movie_limit=3,
        tv_limit=1,
        group_key="trial-lifetime-label",
        group_name="试用转永久文案测试",
        tier_level=2,
    )

    order = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("68.00"),
        pay_payload={
            "billing_cycle": "LIFETIME",
            "source_billing_cycle": "TRIAL",
            "duration_days": 0,
            "purchase_action": "REPLACE_TRIAL",
            "base_amount": 88.0,
            "credit_amount": 20.0,
            "payable_amount": 68.0,
            "carry_balance_amount": 0.0,
        },
    )

    list_response = await async_client.get("/api/v1/orders", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.json()
    record = next(item for item in list_payload if item["id"] == str(order.id))
    assert record["purchase_action"] == "REPLACE_TRIAL"
    assert record["purchase_action_label"] == "试用转永久"

    detail_response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["order"]["purchase_action"] == "REPLACE_TRIAL"
    assert detail_payload["order"]["purchase_action_label"] == "试用转永久"


@pytest.mark.asyncio
async def test_get_orders_finalizes_stale_created_order(async_client):
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("19.90"),
        status=OrderStatus.CREATED,
        created_at=datetime.utcnow() - timedelta(minutes=31),
    )

    response = await async_client.get("/api/v1/orders", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    record = next(item for item in payload if item["id"] == str(order.id))
    assert record["status"] == OrderStatus.TIMEOUT.value

    async with AsyncSessionLocal() as session:
        saved = await session.get(Order, order.id)
        assert saved.status == OrderStatus.TIMEOUT


@pytest.mark.asyncio
async def test_get_order_detail_finalizes_stale_created_order(async_client):
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("29.90"),
        status=OrderStatus.CREATED,
        created_at=datetime.utcnow() - timedelta(minutes=31),
    )

    response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["order"]["status"] == OrderStatus.TIMEOUT.value


@pytest.mark.asyncio
async def test_cannot_pay_stale_created_order(async_client):
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("39.90"),
        status=OrderStatus.CREATED,
        created_at=datetime.utcnow() - timedelta(minutes=31),
    )

    response = await async_client.post(
        f"/api/v1/orders/{order.id}/pay",
        json={"pay_type": "alipay"},
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "订单不可支付"

    async with AsyncSessionLocal() as session:
        saved = await session.get(Order, order.id)
        assert saved.status == OrderStatus.TIMEOUT


@pytest.mark.asyncio
async def test_epay_order_detail_exposes_refund_eligibility(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("49.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        pay_payload={"pay_type": "alipay"},
    )

    response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility == {
        "can_request": True,
        "available_methods": ["original", "balance"],
        "reason": None,
    }


@pytest.mark.asyncio
async def test_balance_order_detail_exposes_refund_eligibility(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("59.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=datetime.utcnow(),
        pay_payload={"pay_type": "balance"},
    )

    response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility == {
        "can_request": True,
        "available_methods": ["balance"],
        "reason": None,
    }


@pytest.mark.asyncio
async def test_recharge_order_detail_blocks_refund_eligibility(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("66.00"),
        order_type=OrderType.RECHARGE,
        status=OrderStatus.COMPLETED,
        pay_provider="BALANCE",
        paid_at=datetime.utcnow(),
        pay_payload={"pay_type": "balance"},
    )

    response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility == {
        "can_request": False,
        "available_methods": [],
        "reason": "充值订单暂不支持退款，请联系管理员处理",
    }


@pytest.mark.asyncio
async def test_expired_refund_window_blocks_refund_eligibility(async_client):
    await _ensure_refund_settings(window_days=7)
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("79.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow() - timedelta(days=8),
        pay_payload={"pay_type": "alipay"},
    )

    response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility["can_request"] is False
    assert eligibility["available_methods"] == []
    assert eligibility["reason"] == "退款时间窗口已过期"


@pytest.mark.asyncio
async def test_refund_in_progress_blocks_refund_eligibility(async_client):
    await _ensure_refund_settings()
    user = await _create_user()
    headers = _auth_headers(user.id)
    order = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("88.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        refund_status=RefundStatus.PENDING,
        pay_payload={"pay_type": "alipay"},
    )

    response = await async_client.get(f"/api/v1/orders/{order.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility["can_request"] is False
    assert eligibility["available_methods"] == []
    assert eligibility["reason"] == "退款处理中，请勿重复提交"


@pytest.mark.asyncio
async def test_monthly_refund_limit_blocks_refund_eligibility(async_client):
    await _ensure_refund_settings(monthly_limit=1, monthly_window_days=30)
    user = await _create_user()
    headers = _auth_headers(user.id)

    await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("39.00"),
        status=OrderStatus.REFUNDED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow() - timedelta(days=2),
        refunded_at=datetime.utcnow() - timedelta(days=1),
        refund_status=RefundStatus.REFUNDED,
        pay_payload={"pay_type": "alipay"},
    )
    target = await _create_order(
        user_id=user.id,
        plan_id=None,
        amount=Decimal("99.00"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        paid_at=datetime.utcnow(),
        pay_payload={"pay_type": "alipay"},
    )

    response = await async_client.get(f"/api/v1/orders/{target.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility["can_request"] is False
    assert eligibility["available_methods"] == []
    assert "退款次数已达上限" in eligibility["reason"]


@pytest.mark.asyncio
async def test_vod_usage_blocks_refund_eligibility(async_client):
    await _ensure_refund_settings(forbid_if_vod_used=True, vod_used_threshold=0)
    user = await _create_user()
    headers = _auth_headers(user.id)
    plan = await _create_plan(
        name="Refund Eligibility Plan",
        price=Decimal("19.90"),
        movie_limit=1,
        tv_limit=1,
        group_key="refund-eligibility",
        group_name="退款资格",
    )
    subscription = await _create_subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=datetime.utcnow() - timedelta(days=1),
        end_at=datetime.utcnow() + timedelta(days=29),
    )
    target = await _create_order(
        user_id=user.id,
        plan_id=plan.id,
        amount=Decimal("19.90"),
        status=OrderStatus.COMPLETED,
        pay_provider="EPAY",
        subscription_id=subscription.id,
        paid_at=datetime.utcnow(),
        pay_payload={"pay_type": "alipay", "subscription_id": str(subscription.id)},
    )

    async with AsyncSessionLocal() as session:
        session.add(
            VodRequest(
                user_id=user.id,
                subscription_id=subscription.id,
                title="Refund Locked Movie",
                media_type="MOVIE",
                quota_consumed=True,
                status="SUCCEEDED",
                cost_type="TIMES",
                cost_amount=1,
            )
        )
        await session.commit()

    response = await async_client.get(f"/api/v1/orders/{target.id}", headers=headers)

    assert response.status_code == 200, response.text
    eligibility = response.json()["order"]["refund_eligibility"]
    assert eligibility["can_request"] is False
    assert eligibility["available_methods"] == []
    assert "点播已使用 1 次" in eligibility["reason"]
