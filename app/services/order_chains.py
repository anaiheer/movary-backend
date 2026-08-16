from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import (
    Order,
    OrderSettlementStatus,
    OrderStatus,
    OrderType,
    OrderValueLink,
)
from app.models.subscription import BillingCycle
from app.services.order_summary import build_order_summary


SUCCESSFUL_CHAIN_ORDER_STATUSES = {
    OrderStatus.PAID,
    OrderStatus.COMPLETED,
    OrderStatus.REFUNDED,
}
VALUE_CONSUMING_ACTIONS = {"UPGRADE", "REPLACE_TRIAL"}


def _parse_uuid(raw_value) -> UUID | None:
    if not raw_value:
        return None
    try:
        return UUID(str(raw_value))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_uuid_list(raw_values) -> list[UUID]:
    values = raw_values or []
    parsed: list[UUID] = []
    seen: set[UUID] = set()
    for raw in values:
        value = _parse_uuid(raw)
        if value and value not in seen:
            seen.add(value)
            parsed.append(value)
    return parsed


def extract_source_subscription_ids(order: Order) -> list[UUID]:
    payload = order.pay_payload or {}
    raw_source_ids = payload.get("source_subscription_ids") or []
    if not raw_source_ids and payload.get("source_subscription_id"):
        raw_source_ids = [payload.get("source_subscription_id")]
    return _parse_uuid_list(raw_source_ids)


async def order_has_consuming_descendants(db: AsyncSession, order: Order) -> bool:
    link_exists = (
        (
            await db.execute(
                select(OrderValueLink.id).where(OrderValueLink.source_order_id == order.id).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if link_exists:
        return True

    subscription_id = getattr(order, "subscription_id", None)
    if not subscription_id:
        return False

    stmt = select(Order).where(
        Order.user_id == order.user_id,
        Order.type == OrderType.PLAN,
        Order.id != order.id,
        Order.status.in_(list(SUCCESSFUL_CHAIN_ORDER_STATUSES)),
    )
    candidate_orders = (await db.execute(stmt)).scalars().all()
    target_subscription_id = str(subscription_id)
    for candidate in candidate_orders:
        candidate_payload = candidate.pay_payload or {}
        action = str(candidate_payload.get("purchase_action") or "").upper()
        if action not in VALUE_CONSUMING_ACTIONS:
            continue
        if target_subscription_id in {
            str(source_id) for source_id in extract_source_subscription_ids(candidate)
        }:
            return True
    return False


async def collect_related_source_orders(db: AsyncSession, order: Order) -> list[Order]:
    seen_order_ids = {order.id}
    pending_target_ids = [order.id]
    related_orders: list[Order] = []

    while pending_target_ids:
        current_target_ids = list({target_id for target_id in pending_target_ids if target_id})
        pending_target_ids = []
        if not current_target_ids:
            break

        linked_orders = (
            (
                await db.execute(
                    select(Order)
                    .join(OrderValueLink, OrderValueLink.source_order_id == Order.id)
                    .where(OrderValueLink.target_order_id.in_(current_target_ids))
                )
            )
            .scalars()
            .all()
        )
        for linked_order in linked_orders:
            if linked_order.id in seen_order_ids:
                continue
            seen_order_ids.add(linked_order.id)
            related_orders.append(linked_order)
            pending_target_ids.append(linked_order.id)

    if related_orders:
        return related_orders

    payload = order.pay_payload or {}
    purchase_action = str(payload.get("purchase_action") or "").upper()
    if purchase_action not in VALUE_CONSUMING_ACTIONS:
        return []

    pending_source_ids = {str(source_id) for source_id in extract_source_subscription_ids(order)}
    seen_source_ids: set[str] = set()

    while pending_source_ids:
        current_source_ids = {
            source_id
            for source_id in pending_source_ids
            if source_id and source_id not in seen_source_ids
        }
        pending_source_ids = set()
        if not current_source_ids:
            break
        seen_source_ids.update(current_source_ids)

        parsed_source_ids = _parse_uuid_list(current_source_ids)
        if not parsed_source_ids:
            continue

        candidate_orders = (
            (
                await db.execute(
                    select(Order).where(
                        Order.type == OrderType.PLAN,
                        Order.subscription_id.in_(parsed_source_ids),
                        Order.status.in_(list(SUCCESSFUL_CHAIN_ORDER_STATUSES)),
                    )
                )
            )
            .scalars()
            .all()
        )
        for candidate in candidate_orders:
            if candidate.id in seen_order_ids:
                continue
            seen_order_ids.add(candidate.id)
            related_orders.append(candidate)
            pending_source_ids.update(
                {str(source_id) for source_id in extract_source_subscription_ids(candidate)}
            )

    return related_orders


async def load_order_chain_snapshot(
    db: AsyncSession,
    order: Order,
) -> tuple[list[Order], list[OrderValueLink]]:
    chain_id = order.order_chain_id or order.id
    root_order_id = order.root_order_id or order.id

    orders = (
        (
            await db.execute(
                select(Order)
                .where(Order.order_chain_id == chain_id)
                .order_by(
                    case((Order.id == root_order_id, 0), else_=1),
                    Order.created_at.asc(),
                    Order.paid_at.asc().nullsfirst(),
                    Order.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    value_links = (
        (
            await db.execute(
                select(OrderValueLink)
                .where(OrderValueLink.order_chain_id == chain_id)
                .order_by(OrderValueLink.created_at.asc(), OrderValueLink.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return orders, value_links


def build_order_chain_snapshot_payload(
    order: Order,
    chain_orders: list[Order],
    value_links: list[OrderValueLink],
) -> dict:
    return {
        "chain_id": order.order_chain_id or order.id,
        "root_order_id": order.root_order_id or order.id,
        "current_order_id": order.id,
        "orders": [
            {
                "id": chain_order.id,
                "order_no": chain_order.order_no,
                "type": chain_order.type.value
                if hasattr(chain_order.type, "value")
                else str(chain_order.type),
                "order_chain_id": chain_order.order_chain_id or chain_order.id,
                "root_order_id": chain_order.root_order_id or chain_order.id,
                "parent_order_id": chain_order.parent_order_id,
                "superseded_by_order_id": chain_order.superseded_by_order_id,
                "status": chain_order.status.value
                if hasattr(chain_order.status, "value")
                else str(chain_order.status),
                "settlement_status": chain_order.settlement_status.value
                if hasattr(chain_order.settlement_status, "value")
                else str(chain_order.settlement_status),
                "refund_status": chain_order.refund_status.value
                if hasattr(chain_order.refund_status, "value")
                else str(chain_order.refund_status),
                "amount": float(chain_order.amount or 0),
                "created_at": chain_order.created_at,
                "paid_at": chain_order.paid_at,
                "refunded_at": chain_order.refunded_at,
                "is_current": chain_order.id == order.id,
                **build_order_summary(chain_order),
            }
            for chain_order in chain_orders
        ],
        "value_links": [
            {
                "id": value_link.id,
                "source_order_id": value_link.source_order_id,
                "target_order_id": value_link.target_order_id,
                "relation_type": value_link.relation_type,
                "consumed_amount": float(value_link.consumed_amount)
                if value_link.consumed_amount is not None
                else None,
                "consumed_days": value_link.consumed_days,
                "created_at": value_link.created_at,
            }
            for value_link in value_links
        ],
    }


async def _find_latest_anchor_order(
    db: AsyncSession,
    *,
    user_id,
    subscription_ids: list[UUID],
) -> Order | None:
    if not subscription_ids:
        return None

    stmt = (
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.type == OrderType.PLAN,
            Order.subscription_id.in_(subscription_ids),
            Order.status.in_(list(SUCCESSFUL_CHAIN_ORDER_STATUSES)),
        )
        .order_by(Order.paid_at.desc(), Order.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


def _chain_source_candidates(
    *,
    purchase_action: str,
    current_subscription_id: UUID | None,
    renewal_anchor_subscription_id: UUID | None,
    source_subscription_ids: list[UUID],
) -> list[UUID]:
    if purchase_action == "RENEW":
        return [
            subscription_id
            for subscription_id in [renewal_anchor_subscription_id]
            if subscription_id
        ]

    if purchase_action in VALUE_CONSUMING_ACTIONS:
        combined = list(source_subscription_ids)
        if current_subscription_id:
            combined.append(current_subscription_id)
        return _parse_uuid_list(combined)

    return []


async def assign_order_chain_for_creation(
    db: AsyncSession,
    order: Order,
    *,
    current_subscription_id: UUID | None = None,
    renewal_anchor_subscription_id: UUID | None = None,
    source_subscription_ids: list[UUID] | None = None,
) -> None:
    if not order.id:
        order.id = uuid4()

    source_ids = source_subscription_ids or []
    payload = order.pay_payload or {}
    purchase_action = str(payload.get("purchase_action") or "DIRECT_PURCHASE").upper()

    if order.type != OrderType.PLAN:
        order.order_chain_id = order.id
        order.root_order_id = order.id
        order.parent_order_id = None
        order.settlement_status = OrderSettlementStatus.OPEN
        return

    chain_source_ids = _chain_source_candidates(
        purchase_action=purchase_action,
        current_subscription_id=current_subscription_id,
        renewal_anchor_subscription_id=renewal_anchor_subscription_id,
        source_subscription_ids=source_ids,
    )
    parent_order = await _find_latest_anchor_order(
        db,
        user_id=order.user_id,
        subscription_ids=chain_source_ids,
    )

    if parent_order:
        order.order_chain_id = parent_order.order_chain_id or parent_order.id
        order.root_order_id = parent_order.root_order_id or parent_order.id
        order.parent_order_id = parent_order.id
    else:
        order.order_chain_id = order.id
        order.root_order_id = order.id
        order.parent_order_id = None

    order.settlement_status = OrderSettlementStatus.OPEN


async def _load_source_orders_for_consumption(
    db: AsyncSession,
    *,
    order: Order,
    source_subscription_ids: list[UUID],
) -> list[Order]:
    if not source_subscription_ids:
        return []

    stmt = (
        select(Order)
        .where(
            Order.user_id == order.user_id,
            Order.type == OrderType.PLAN,
            Order.subscription_id.in_(source_subscription_ids),
            Order.id != order.id,
            Order.status.in_(list(SUCCESSFUL_CHAIN_ORDER_STATUSES)),
        )
        .order_by(Order.paid_at.asc(), Order.created_at.asc())
    )
    return (await db.execute(stmt)).scalars().all()


def _relation_type_for_order(order: Order, source_orders: list[Order]) -> str:
    payload = order.pay_payload or {}
    purchase_action = str(payload.get("purchase_action") or "").upper()
    if purchase_action == "REPLACE_TRIAL":
        return "REPLACE_TRIAL"

    target_cycle = str(payload.get("billing_cycle") or "").upper()
    source_cycles = {
        str((source_order.pay_payload or {}).get("billing_cycle") or "").upper()
        for source_order in source_orders
    }
    if purchase_action == "UPGRADE" and target_cycle == BillingCycle.LIFETIME.value:
        if source_cycles and all(cycle == BillingCycle.LIFETIME.value for cycle in source_cycles):
            return "UPGRADE_LIFETIME_TIER"
        return "UPGRADE_TO_LIFETIME"

    return purchase_action or "UPGRADE"


async def apply_successful_order_chain_updates(db: AsyncSession, order: Order) -> None:
    payload = order.pay_payload or {}
    purchase_action = str(payload.get("purchase_action") or "").upper()

    if order.type != OrderType.PLAN:
        if order.settlement_status != OrderSettlementStatus.REFUNDED:
            order.settlement_status = OrderSettlementStatus.OPEN
        db.add(order)
        return

    if purchase_action not in VALUE_CONSUMING_ACTIONS:
        if order.settlement_status != OrderSettlementStatus.REFUNDED:
            order.settlement_status = OrderSettlementStatus.OPEN
        db.add(order)
        return

    source_orders = await _load_source_orders_for_consumption(
        db,
        order=order,
        source_subscription_ids=extract_source_subscription_ids(order),
    )
    relation_type = _relation_type_for_order(order, source_orders)
    credit_amount = payload.get("credit_amount")
    consumed_amount = None
    if len(source_orders) == 1 and credit_amount is not None:
        consumed_amount = Decimal(str(credit_amount))

    for source_order in source_orders:
        source_order.superseded_by_order_id = order.id
        if source_order.settlement_status != OrderSettlementStatus.REFUNDED:
            source_order.settlement_status = OrderSettlementStatus.CONSUMED
        db.add(source_order)

        existing_link = (
            (
                await db.execute(
                    select(OrderValueLink).where(
                        OrderValueLink.source_order_id == source_order.id,
                        OrderValueLink.target_order_id == order.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_link:
            continue

        db.add(
            OrderValueLink(
                order_chain_id=order.order_chain_id or order.id,
                source_order_id=source_order.id,
                target_order_id=order.id,
                relation_type=relation_type,
                consumed_amount=consumed_amount,
                consumed_days=None,
            )
        )

    if order.settlement_status != OrderSettlementStatus.REFUNDED:
        order.settlement_status = OrderSettlementStatus.OPEN
    db.add(order)
