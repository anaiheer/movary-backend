import codecs
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.order import (
    Order,
    OrderSettlementStatus,
    OrderStatus,
    OrderType,
    OrderValueLink,
    PaymentTransaction,
    RefundStatus,
)
from app.models.system_settings import SystemSettings
from app.models.user import User


async def _create_order_with_transaction(
    *,
    status: OrderStatus = OrderStatus.PAID,
    refund_status: RefundStatus = RefundStatus.NONE,
) -> tuple[UUID, UUID]:
    async with AsyncSessionLocal() as session:
        user = User(
            username=f"order_user_{uuid4().hex[:8]}",
            email=f"order_user_{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=88,
            status=status,
            pay_provider="EPAY",
            refund_status=refund_status,
            pay_payload={"pay_type": "alipay"},
        )
        session.add(order)
        await session.flush()

        transaction = PaymentTransaction(
            order_id=order.id,
            provider_trade_no=f"TRADE{uuid4().hex[:10]}",
            status="PAID",
            raw_callback={"paid": True},
        )
        session.add(transaction)
        await session.commit()

        return order.id, transaction.id


async def _create_linked_orders() -> tuple[UUID, UUID, UUID]:
    async with AsyncSessionLocal() as session:
        user = User(
            username=f"linked_order_user_{uuid4().hex[:8]}",
            email=f"linked_order_user_{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        root_order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=66,
            status=OrderStatus.COMPLETED,
            pay_provider="BALANCE",
            pay_payload={"billing_cycle": "MONTHLY", "purchase_action": "DIRECT_PURCHASE"},
        )
        session.add(root_order)
        await session.flush()

        child_order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=99,
            status=OrderStatus.COMPLETED,
            pay_provider="BALANCE",
            order_chain_id=root_order.id,
            root_order_id=root_order.id,
            parent_order_id=root_order.id,
            pay_payload={"billing_cycle": "LIFETIME", "purchase_action": "UPGRADE"},
        )
        session.add(child_order)
        await session.flush()

        root_order.superseded_by_order_id = child_order.id
        session.add(root_order)
        await session.flush()

        link = OrderValueLink(
            order_chain_id=root_order.id,
            source_order_id=root_order.id,
            target_order_id=child_order.id,
            relation_type="UPGRADE_TO_LIFETIME",
        )
        session.add(link)
        await session.commit()

        return root_order.id, child_order.id, link.id


async def _ensure_refund_settings() -> None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemSettings))).scalar()
        if not row:
            row = SystemSettings()
        row.refund_enabled = True
        row.refund_window_days = 0
        row.refund_user_monthly_limit = 0
        row.refund_user_monthly_window_days = 30
        session.add(row)
        await session.commit()


@pytest.mark.asyncio
async def test_admin_batch_delete_orders_removes_transactions(async_client, admin_token):
    order_id, transaction_id = await _create_order_with_transaction()

    response = await async_client.post(
        "/api/v1/admin/orders/batch-delete",
        json={"ids": [str(order_id)]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload == {
        "requested": 1,
        "deleted": 1,
        "missing": 0,
        "missing_ids": [],
        "failed_ids": [],
    }

    async with AsyncSessionLocal() as session:
        saved_order = await session.get(Order, order_id)
        saved_tx = await session.get(PaymentTransaction, transaction_id)
        assert saved_order is None
        assert saved_tx is None


@pytest.mark.asyncio
async def test_admin_batch_delete_orders_reports_missing_ids(async_client, admin_token):
    order_id, _ = await _create_order_with_transaction()
    missing_id = str(uuid4())

    response = await async_client.post(
        "/api/v1/admin/orders/batch-delete",
        json={"ids": [str(order_id), missing_id, str(order_id)]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["requested"] == 2
    assert payload["deleted"] == 1
    assert payload["missing"] == 1
    assert payload["missing_ids"] == [missing_id]
    assert payload["failed_ids"] == []

    async with AsyncSessionLocal() as session:
        remaining_orders = (
            (await session.execute(select(Order.id).where(Order.id == order_id))).scalars().all()
        )
        remaining_transactions = (
            (
                await session.execute(
                    select(PaymentTransaction.id).where(PaymentTransaction.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining_orders == []
        assert remaining_transactions == []


@pytest.mark.asyncio
async def test_admin_batch_delete_orders_removes_chain_links(async_client, admin_token):
    root_order_id, child_order_id, link_id = await _create_linked_orders()

    response = await async_client.post(
        "/api/v1/admin/orders/batch-delete",
        json={"ids": [str(root_order_id)]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["deleted"] == 1
    assert payload["failed_ids"] == []

    async with AsyncSessionLocal() as session:
        deleted_root = await session.get(Order, root_order_id)
        remaining_child = await session.get(Order, child_order_id)
        remaining_link = await session.get(OrderValueLink, link_id)

        assert deleted_root is None
        assert remaining_link is None
        assert remaining_child is not None
        assert remaining_child.parent_order_id is None
        assert remaining_child.root_order_id == remaining_child.id
        assert remaining_child.order_chain_id == remaining_child.id


@pytest.mark.asyncio
async def test_admin_list_orders_filters_by_refund_status(async_client, admin_token):
    pending_order_id, _ = await _create_order_with_transaction(refund_status=RefundStatus.PENDING)
    await _create_order_with_transaction(refund_status=RefundStatus.REFUNDED)

    response = await async_client.get(
        "/api/v1/admin/orders",
        params={"refund_status": "PENDING"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(pending_order_id)
    assert items[0]["refund_status"] == "PENDING"
    assert items[0]["refund_status_label"] == "退款待审核"


@pytest.mark.asyncio
async def test_admin_list_orders_stats_are_not_affected_by_filters(async_client, admin_token):
    username = f"stats_filter_user_{uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=88,
            status=OrderStatus.PAID,
            pay_provider="EPAY",
            refund_status=RefundStatus.PENDING,
            pay_payload={"pay_type": "alipay"},
        )
        session.add(order)
        await session.commit()

    all_response = await async_client.get(
        "/api/v1/admin/orders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert all_response.status_code == 200, all_response.text
    all_stats = all_response.json()["data"]["stats"]

    response = await async_client.get(
        "/api/v1/admin/orders",
        params={"keyword": username},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    items = payload["items"]
    assert items
    assert all(item["user"]["username"] == username for item in items)

    stats = payload["stats"]
    assert stats == all_stats
    assert set(stats.keys()) == {
        "total_orders",
        "successful_orders",
        "pending_orders",
        "refund_orders",
    }
    assert stats["successful_orders"] >= 1
    assert stats["refund_orders"] >= 1


@pytest.mark.asyncio
async def test_admin_list_orders_successful_stats_exclude_refunded_orders(
    async_client, admin_token
):
    before_response = await async_client.get(
        "/api/v1/admin/orders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert before_response.status_code == 200, before_response.text
    before_stats = before_response.json()["data"]["stats"]

    await _create_order_with_transaction(status=OrderStatus.REFUNDED)
    await _create_order_with_transaction(status=OrderStatus.COMPLETED)

    response = await async_client.get(
        "/api/v1/admin/orders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    stats = response.json()["data"]["stats"]
    assert stats["successful_orders"] == before_stats["successful_orders"] + 1


@pytest.mark.asyncio
async def test_admin_completed_filter_excludes_orders_already_refunded(async_client, admin_token):
    username = f"completed_filter_user_{uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        completed_order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=88,
            status=OrderStatus.COMPLETED,
            pay_provider="EPAY",
            refund_status=RefundStatus.NONE,
            pay_payload={"pay_type": "alipay"},
        )
        refunded_order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.PLAN,
            amount=66,
            status=OrderStatus.COMPLETED,
            pay_provider="EPAY",
            refund_status=RefundStatus.REFUNDED,
            pay_payload={"pay_type": "alipay"},
        )
        session.add_all([completed_order, refunded_order])
        await session.commit()
        completed_order_id = completed_order.id
        refunded_order_id = refunded_order.id

    completed_response = await async_client.get(
        "/api/v1/admin/orders",
        params={"keyword": username, "status": "COMPLETED"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert completed_response.status_code == 200, completed_response.text
    completed_items = completed_response.json()["data"]["items"]
    assert [item["id"] for item in completed_items] == [str(completed_order_id)]

    refunded_response = await async_client.get(
        "/api/v1/admin/orders",
        params={"keyword": username, "status": "REFUNDED"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert refunded_response.status_code == 200, refunded_response.text
    refunded_items = refunded_response.json()["data"]["items"]
    assert [item["id"] for item in refunded_items] == [str(refunded_order_id)]


@pytest.mark.asyncio
async def test_admin_list_orders_filters_by_order_chain_id(async_client, admin_token):
    root_order_id, child_order_id, _ = await _create_linked_orders()

    response = await async_client.get(
        "/api/v1/admin/orders",
        params={"order_chain_id": str(root_order_id)},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert [item["id"] for item in items] == [str(child_order_id), str(root_order_id)]
    assert all(item["order_chain_id"] == str(root_order_id) for item in items)


@pytest.mark.asyncio
async def test_admin_export_orders_supports_selected_ids(async_client, admin_token):
    export_order_id, _ = await _create_order_with_transaction(refund_status=RefundStatus.PENDING)
    await _create_order_with_transaction(refund_status=RefundStatus.REFUNDED)

    response = await async_client.post(
        "/api/v1/admin/orders/export",
        json={"ids": [str(export_order_id)]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(codecs.BOM_UTF8)

    content = response.content.decode("utf-8-sig")
    assert "订单号,用户,套餐,订单类型,订单状态,退款状态,金额,支付方式,创建时间,支付时间" in content
    assert "退款待审核" in content
    assert "支付宝" in content


@pytest.mark.asyncio
async def test_admin_export_orders_supports_filters(async_client, admin_token):
    pending_order_id, _ = await _create_order_with_transaction(refund_status=RefundStatus.PENDING)
    refunded_order_id, _ = await _create_order_with_transaction(refund_status=RefundStatus.REFUNDED)

    response = await async_client.post(
        "/api/v1/admin/orders/export",
        json={"filters": {"refund_status": "PENDING"}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    content = response.content.decode("utf-8-sig")
    assert str(pending_order_id) not in content
    assert str(refunded_order_id) not in content
    assert "退款待审核" in content
    assert "退款已完成" not in content


@pytest.mark.asyncio
async def test_admin_can_initiate_balance_refund_without_pending_request(async_client, admin_token):
    await _ensure_refund_settings()
    order_id, _ = await _create_order_with_transaction(status=OrderStatus.COMPLETED)

    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        order.pay_provider = "BALANCE"
        order.pay_payload = {"pay_type": "balance"}
        session.add(order)
        await session.commit()

    response = await async_client.post(
        f"/api/v1/admin/orders/{order_id}/refund",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["refund_status"] == "REFUNDED"

    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        assert order.status == OrderStatus.REFUNDED
        assert order.refund_status == RefundStatus.REFUNDED


@pytest.mark.asyncio
async def test_admin_initiated_refund_still_respects_refund_rules(async_client, admin_token):
    await _ensure_refund_settings()

    async with AsyncSessionLocal() as session:
        user = User(
            username=f"recharge_user_{uuid4().hex[:8]}",
            email=f"recharge_user_{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        order = Order(
            user_id=user.id,
            plan_id=None,
            order_no=f"OD{uuid4().hex[:12]}",
            type=OrderType.RECHARGE,
            amount=66,
            status=OrderStatus.COMPLETED,
            pay_provider="BALANCE",
            refund_status=RefundStatus.NONE,
            pay_payload={"pay_type": "balance"},
        )
        session.add(order)
        await session.commit()
        order_id = order.id

    response = await async_client.post(
        f"/api/v1/admin/orders/{order_id}/refund",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "充值订单暂不支持退款，请联系管理员处理"


@pytest.mark.asyncio
async def test_admin_direct_refund_on_upgrade_node_covers_source_orders(async_client, admin_token):
    await _ensure_refund_settings()
    root_order_id, child_order_id, _ = await _create_linked_orders()

    response = await async_client.post(
        f"/api/v1/admin/orders/{child_order_id}/refund",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["status"] == "REFUNDED"
    assert payload["refund_status"] == "REFUNDED"

    async with AsyncSessionLocal() as session:
        root_order = await session.get(Order, root_order_id)
        child_order = await session.get(Order, child_order_id)

        assert child_order.status == OrderStatus.REFUNDED
        assert child_order.refund_status == RefundStatus.REFUNDED
        assert child_order.settlement_status == OrderSettlementStatus.REFUNDED

        assert root_order.status == OrderStatus.REFUNDED
        assert root_order.refund_status == RefundStatus.REFUNDED
        assert root_order.settlement_status == OrderSettlementStatus.COVERED_BY_DESCENDANT_REFUND
        assert root_order.refunded_at is not None
