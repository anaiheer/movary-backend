import pytest
import uuid
from sqlalchemy import select
from urllib.parse import parse_qs, urlparse

from app.core.security import create_token
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.models.subscription import BillingCycle, Plan, PlanBillingCycle, PlanStatus, Subscription
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.epay import sign_params


async def ensure_settings(
    *,
    site_url: str | None = None,
    epay_notify_url: str | None = None,
    epay_return_url: str | None = None,
):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.epay_enabled = True
        row.epay_gateway = "https://pay.example.com"
        row.epay_merchant_id = "m123"
        row.epay_key = "k123"
        row.site_url = site_url
        row.epay_notify_url = epay_notify_url
        row.epay_return_url = epay_return_url
        session.add(row)
        await session.commit()


async def create_plan():
    async with AsyncSessionLocal() as session:
        group_key = f"epay-{uuid.uuid4().hex[:8]}"
        plan = Plan(
            group_key=group_key,
            group_name="支付测试",
            tier_level=1,
            name="测试套餐",
            description="test",
            duration_days=30,
            price=9.9,
            vod_movie_times=10,
            vod_tv_times=5,
            status=PlanStatus.ON,
            is_visible=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            PlanBillingCycle(
                plan_id=plan.id,
                billing_cycle=BillingCycle.MONTHLY,
                price=9.9,
                duration_days=30,
                is_default=True,
                sort_order=30,
            )
        )
        await session.commit()
        await session.refresh(plan)
        return plan


async def create_user():
    async with AsyncSessionLocal() as session:
        username = f"pay_user_{uuid.uuid4().hex[:8]}"
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="$2b$12$Zb2aTFDqQ6dYxq2H2QmBfuXq5QkU7b.1iKk9ppn7Q3aY5G0s1Q0kW",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.mark.asyncio
async def test_epay_notify_creates_subscription(async_client):
    await ensure_settings(
        epay_notify_url="https://app.example.com/api/v1/pay/epay/notify",
        epay_return_url="https://app.example.com/api/v1/pay/epay/return",
    )
    plan = await create_plan()
    user = await create_user()

    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await async_client.post(
        "/api/v1/orders", json={"plan_id": str(plan.id)}, headers=headers
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["success"] is True
    order = payload["data"]["order"]

    pay_resp = await async_client.post(
        f"/api/v1/orders/{order['id']}/pay",
        json={"pay_type": "alipay"},
        headers=headers,
    )
    assert pay_resp.status_code == 200
    order_no = order["order_no"]

    notify_payload = {
        "pid": "m123",
        "type": "alipay",
        "out_trade_no": order_no,
        "trade_no": "T123",
        "trade_status": "TRADE_SUCCESS",
        "money": "9.90",
    }
    notify_payload["sign_type"] = "MD5"
    notify_payload["sign"] = sign_params(notify_payload, "k123")

    notify_resp = await async_client.post("/api/v1/pay/epay/notify", data=notify_payload)
    assert notify_resp.status_code == 200
    assert notify_resp.text == "success"

    async with AsyncSessionLocal() as session:
        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user.id))
        ).scalar()
        assert sub is not None


@pytest.mark.asyncio
async def test_epay_notify_rejects_amount_mismatch(async_client):
    await ensure_settings()
    plan = await create_plan()
    user = await create_user()

    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await async_client.post(
        "/api/v1/orders", json={"plan_id": str(plan.id)}, headers=headers
    )
    assert create_resp.status_code == 200
    order = create_resp.json()["data"]["order"]

    pay_resp = await async_client.post(
        f"/api/v1/orders/{order['id']}/pay",
        json={"pay_type": "alipay"},
        headers=headers,
    )
    assert pay_resp.status_code == 200

    notify_payload = {
        "pid": "m123",
        "type": "alipay",
        "out_trade_no": order["order_no"],
        "trade_no": "T124",
        "trade_status": "TRADE_SUCCESS",
        "money": "0.01",
    }
    notify_payload["sign_type"] = "MD5"
    notify_payload["sign"] = sign_params(notify_payload, "k123")

    notify_resp = await async_client.post("/api/v1/pay/epay/notify", data=notify_payload)
    assert notify_resp.status_code == 200
    assert notify_resp.text == "fail"

    async with AsyncSessionLocal() as session:
        sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user.id))
        ).scalar()
        db_order = await session.scalar(select(Order).where(Order.id == uuid.UUID(order["id"])))
        assert sub is None
        assert db_order is not None
        assert db_order.status == OrderStatus.CREATED


@pytest.mark.asyncio
async def test_create_pay_link_falls_back_to_site_url(async_client):
    await ensure_settings(site_url="https://movary.example.com")
    plan = await create_plan()
    user = await create_user()

    token = create_token({"sub": str(user.id)}, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await async_client.post(
        "/api/v1/orders", json={"plan_id": str(plan.id)}, headers=headers
    )
    assert create_resp.status_code == 200
    order = create_resp.json()["data"]["order"]

    pay_resp = await async_client.post(
        f"/api/v1/orders/{order['id']}/pay",
        json={"pay_type": "alipay"},
        headers=headers,
    )
    assert pay_resp.status_code == 200
    pay_url = pay_resp.json()["data"]["pay_url"]

    query = parse_qs(urlparse(pay_url).query)
    assert query["notify_url"] == ["https://movary.example.com/api/v1/pay/epay/notify"]
    assert query["return_url"] == ["https://movary.example.com/pay/return"]
