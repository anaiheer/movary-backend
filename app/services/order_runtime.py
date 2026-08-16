from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus
from app.models.system_settings import SystemSettings
from app.services.epay import epay_close


ORDER_TIMEOUT_MINUTES = 30


def order_timeout_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.utcnow()) - timedelta(minutes=ORDER_TIMEOUT_MINUTES)


def is_order_expired(order: Order, now: datetime | None = None) -> bool:
    return (
        order.status == OrderStatus.CREATED
        and order.created_at is not None
        and order.created_at <= order_timeout_cutoff(now)
    )


async def try_close_remote_order(order: Order, settings_row: SystemSettings) -> None:
    if order.pay_provider != "EPAY":
        return
    if not settings_row.epay_enabled or not settings_row.epay_gateway:
        return
    if not settings_row.epay_merchant_id or not settings_row.epay_key:
        return

    try:
        response = await epay_close(
            settings_row.epay_gateway,
            settings_row.epay_merchant_id,
            settings_row.epay_key,
            out_trade_no=order.order_no,
        )
        order.pay_payload = {**(order.pay_payload or {}), "close_response": response}
    except Exception as exc:
        order.pay_payload = {**(order.pay_payload or {}), "close_error": str(exc)}


async def finalize_expired_order(
    order: Order,
    db: AsyncSession,
    settings_row: SystemSettings,
    *,
    now: datetime | None = None,
) -> bool:
    if not is_order_expired(order, now):
        return False

    await try_close_remote_order(order, settings_row)
    order.status = OrderStatus.TIMEOUT
    db.add(order)
    await db.flush()
    return True


async def finalize_stale_created_orders(
    db: AsyncSession,
    settings_row: SystemSettings,
    *,
    user_id: UUID | None = None,
    order_id: UUID | None = None,
    now: datetime | None = None,
) -> int:
    stmt = select(Order).where(
        Order.status == OrderStatus.CREATED,
        Order.created_at <= order_timeout_cutoff(now),
    )
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    if order_id is not None:
        stmt = stmt.where(Order.id == order_id)

    orders = (await db.execute(stmt)).scalars().all()
    updated = 0
    for order in orders:
        if await finalize_expired_order(order, db, settings_row, now=now):
            updated += 1

    if updated:
        await db.commit()

    return updated
