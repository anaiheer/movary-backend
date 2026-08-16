from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus, OrderType, RefundStatus
from app.models.user import User


async def _create_dashboard_order(
    *,
    amount: int,
    status: OrderStatus,
    created_at: datetime,
    refund_status: RefundStatus = RefundStatus.NONE,
    payable_amount: int | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        user = User(
            username=f"dashboard_user_{uuid4().hex[:8]}",
            email=f"dashboard_user_{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=amount,
            status=status,
            pay_provider="EPAY",
            created_at=created_at,
            refund_status=refund_status,
            pay_payload={
                "payable_amount": payable_amount if payable_amount is not None else amount
            },
        )
        session.add(order)
        await session.commit()


@pytest.mark.asyncio
async def test_dashboard_total_revenue_excludes_refunded_realized_amount(async_client, admin_token):
    now = datetime.utcnow()

    before_response = await async_client.get(
        "/api/v1/admin/dashboard/overview",
        params={"granularity": "day", "periods": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert before_response.status_code == 200, before_response.text
    before_payload = before_response.json()["data"]
    before_total = float(before_payload["kpi"]["total_revenue"])
    before_trend = float(before_payload["charts"]["revenue_trend"][0]["value"])

    await _create_dashboard_order(amount=120, status=OrderStatus.COMPLETED, created_at=now)
    await _create_dashboard_order(
        amount=80,
        status=OrderStatus.REFUNDED,
        created_at=now,
        refund_status=RefundStatus.REFUNDED,
    )

    response = await async_client.get(
        "/api/v1/admin/dashboard/overview",
        params={"granularity": "day", "periods": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]

    assert float(payload["kpi"]["total_revenue"]) == pytest.approx(before_total + 120)
    assert len(payload["charts"]["revenue_trend"]) == 1
    assert float(payload["charts"]["revenue_trend"][0]["value"]) == pytest.approx(
        before_trend + 120
    )


@pytest.mark.asyncio
async def test_dashboard_revenue_trend_groups_by_order_created_at(async_client, admin_token):
    now = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = now - timedelta(days=1)
    await _create_dashboard_order(amount=60, status=OrderStatus.COMPLETED, created_at=yesterday)
    await _create_dashboard_order(amount=40, status=OrderStatus.COMPLETED, created_at=now)

    response = await async_client.get(
        "/api/v1/admin/dashboard/overview",
        params={"granularity": "day", "periods": 2},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    trend = response.json()["data"]["charts"]["revenue_trend"]

    assert len(trend) == 2
    assert float(trend[0]["value"]) >= 60
    assert float(trend[1]["value"]) >= 40
