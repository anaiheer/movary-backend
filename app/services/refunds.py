from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import random
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import BalanceTransaction
from app.models.order import Order, OrderSettlementStatus, OrderStatus, OrderType, RefundStatus
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.models.vod import VodRequest
from app.services.epay import epay_refund, epay_refund_query
from app.services.order_chains import collect_related_source_orders, order_has_consuming_descendants
from app.services.subscriptions import (
    delete_subscriptions_immediately,
    sync_user_subscription_entitlements,
)


MONEY_QUANT = Decimal("0.01")
SPLIT_REFUND_ACTIONS = {"UPGRADE", "REPLACE_TRIAL"}

REFUND_DISABLED = "退款功能未启用"
ORDER_NOT_REFUNDABLE = "订单不可退款"
ORDER_NOT_PAID = "订单未支付"
REFUND_ALREADY_IN_PROGRESS = "退款处理中，请勿重复提交"
REFUND_WINDOW_EXPIRED = "退款时间窗口已过期"
INVALID_REFUND_TO = "无效的退款方式"
INVALID_REFUND_AMOUNT = "退款金额无效"
REFUND_AMOUNT_REQUIRED_POSITIVE = "退款金额必须大于 0"
REFUND_AMOUNT_EXCEEDS_ORDER = "退款金额不能超过订单可退金额"
PARTIAL_REFUND_NOT_SUPPORTED = "当前仅支持全额退款"
RECHARGE_REFUND_NOT_ALLOWED = "充值订单暂不支持退款，请联系管理员处理"
EPAY_REFUND_NOT_SUPPORTED = "暂不支持原路退款"
REFUND_REQUEST_NOT_FOUND = "没有待审核的退款申请"
REFUND_REJECT_REASON_REQUIRED = "请填写拒绝原因"
ORDER_VALUE_ALREADY_USED = "当前订单价值已用于后续升级，不能重复退款"


def get_refund_state(order: Order) -> dict:
    refund_state = (order.pay_payload or {}).get("refund")
    return refund_state if isinstance(refund_state, dict) else {}


def merge_refund_state(order: Order, **changes) -> dict:
    refund_state = {**get_refund_state(order), **changes}
    order.pay_payload = {**(order.pay_payload or {}), "refund": refund_state}
    return refund_state


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_QUANT)


def get_refund_amount_breakdown(order: Order) -> tuple[Decimal, Decimal, Decimal]:
    payload = order.pay_payload or {}
    action = str(payload.get("purchase_action") or "").upper()
    payable_amount = _money(
        payload.get("payable_amount") if "payable_amount" in payload else order.amount
    )
    credit_amount = _money(payload.get("credit_amount"))

    if action in SPLIT_REFUND_ACTIONS and credit_amount > Decimal("0.00"):
        total_amount = payable_amount + credit_amount
        return total_amount, payable_amount, credit_amount

    return payable_amount, payable_amount, Decimal("0.00")


def parse_refund_money(raw: str | None, order: Order) -> str:
    full_amount, _, _ = get_refund_amount_breakdown(order)
    if raw is None:
        return f"{full_amount:.2f}"
    try:
        money = Decimal(str(raw)).quantize(MONEY_QUANT)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_REFUND_AMOUNT
        ) from exc
    if money <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=REFUND_AMOUNT_REQUIRED_POSITIVE,
        )
    if money > full_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=REFUND_AMOUNT_EXCEEDS_ORDER,
        )
    if money != full_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=PARTIAL_REFUND_NOT_SUPPORTED,
        )
    return f"{money:.2f}"


async def _find_subscription_for_order(order: Order, db: AsyncSession) -> Subscription | None:
    subscription: Subscription | None = None
    if getattr(order, "subscription_id", None):
        subscription = (
            await db.execute(select(Subscription).where(Subscription.id == order.subscription_id))
        ).scalar()
    if not subscription and order.plan_id:
        subscription = (
            await db.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == order.user_id,
                    Subscription.plan_id == order.plan_id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
        ).scalar()
    return subscription


async def cancel_subscription_for_order(order: Order, db: AsyncSession) -> None:
    subscription = await _find_subscription_for_order(order, db)
    if not subscription:
        return
    await delete_subscriptions_immediately(db, [subscription])


async def _load_source_orders_for_subscription(
    db: AsyncSession, subscription_id: UUID
) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.subscription_id == subscription_id,
            Order.type == OrderType.PLAN,
            Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED, OrderStatus.REFUNDED]),
        )
        .order_by(Order.paid_at.asc(), Order.created_at.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def rollback_subscription_for_order(order: Order, db: AsyncSession) -> None:
    subscription = await _find_subscription_for_order(order, db)

    now = datetime.utcnow()
    if subscription:
        source_orders = await _load_source_orders_for_subscription(db, subscription.id)
        remaining_duration_days = 0
        for source_order in source_orders:
            if source_order.id == order.id or source_order.status == OrderStatus.REFUNDED:
                continue
            remaining_duration_days += max(
                int((source_order.pay_payload or {}).get("duration_days") or 0), 0
            )

        if remaining_duration_days > 0:
            subscription.end_at = subscription.start_at + timedelta(days=remaining_duration_days)
            if subscription.end_at <= now:
                subscription.status = SubscriptionStatus.CANCELED
            else:
                subscription.status = SubscriptionStatus.ACTIVE
            db.add(subscription)
            return

    await cancel_subscription_for_order(order, db)


async def resolve_subscription_id_for_order(order: Order, db: AsyncSession) -> UUID | None:
    if getattr(order, "subscription_id", None):
        return order.subscription_id
    payload = order.pay_payload or {}
    raw_subscription_id = payload.get("subscription_id")
    if raw_subscription_id:
        try:
            return UUID(str(raw_subscription_id))
        except Exception:
            pass
    if order.plan_id and order.paid_at:
        window_start = order.paid_at - timedelta(minutes=10)
        window_end = order.paid_at + timedelta(minutes=10)
        return await db.scalar(
            select(Subscription.id)
            .where(
                Subscription.user_id == order.user_id,
                Subscription.plan_id == order.plan_id,
                Subscription.created_at >= window_start,
                Subscription.created_at <= window_end,
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    return None


async def validate_refund_policies(
    order: Order,
    db: AsyncSession,
    settings_row: SystemSettings,
) -> None:
    if order.type == OrderType.RECHARGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=RECHARGE_REFUND_NOT_ALLOWED
        )

    if getattr(settings_row, "refund_forbid_if_vod_used", False):
        threshold = int(getattr(settings_row, "refund_vod_used_threshold", 0) or 0)
        subscription_id = await resolve_subscription_id_for_order(order, db)
        used = 0
        if subscription_id:
            used = int(
                await db.scalar(
                    select(func.count(VodRequest.id)).where(
                        VodRequest.subscription_id == subscription_id,
                        VodRequest.quota_consumed.is_(True),
                    )
                )
                or 0
            )
        if used > threshold:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不允许退款：点播已使用 {used} 次，大于阈值 {threshold}",
            )

    monthly_limit = int(getattr(settings_row, "refund_user_monthly_limit", 0) or 0)
    window_days = int(getattr(settings_row, "refund_user_monthly_window_days", 30) or 30)
    if monthly_limit > 0:
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        used_refunds = int(
            await db.scalar(
                select(func.count(Order.id)).where(
                    Order.user_id == order.user_id,
                    Order.refunded_at.isnot(None),
                    Order.refunded_at >= cutoff,
                )
            )
            or 0
        )
        if used_refunds >= monthly_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"退款次数已达上限：最近 {window_days} 天内已使用 {used_refunds}/{monthly_limit} 次",
            )

    refund_window_days = int(getattr(settings_row, "refund_window_days", 0) or 0)
    if refund_window_days > 0:
        if not order.paid_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_NOT_PAID)
        deadline = order.paid_at + timedelta(days=refund_window_days)
        if datetime.utcnow() > deadline:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=REFUND_WINDOW_EXPIRED,
            )


async def _order_value_consumed_by_later_upgrade(order: Order, db: AsyncSession) -> bool:
    return await order_has_consuming_descendants(db, order)


async def _settle_related_source_orders_as_refunded(
    order: Order,
    db: AsyncSession,
    *,
    reviewer_user_id: UUID | None,
    refunded_at: datetime,
) -> None:
    related_orders = await collect_related_source_orders(db, order)
    for related_order in related_orders:
        if related_order.refund_status == RefundStatus.REFUNDED:
            continue

        related_total_money, _, _ = get_refund_amount_breakdown(related_order)
        related_order.status = OrderStatus.REFUNDED
        related_order.refund_status = RefundStatus.REFUNDED
        related_order.settlement_status = OrderSettlementStatus.COVERED_BY_DESCENDANT_REFUND
        related_order.refunded_at = refunded_at
        related_order.refund_reviewed_at = refunded_at
        related_order.refund_reviewed_by = reviewer_user_id
        related_order.refund_reject_reason = None
        merge_refund_state(
            related_order,
            money=f"{related_total_money:.2f}",
            status=RefundStatus.REFUNDED.value,
            reviewed_at=refunded_at.isoformat(),
            reviewed_by=str(reviewer_user_id) if reviewer_user_id else None,
            covered_by_order_id=str(order.id),
            covered_by_order_no=order.order_no,
            note=f"covered by refund of {order.order_no}",
        )
        db.add(related_order)


async def ensure_order_refundable(order: Order, db: AsyncSession) -> None:
    if order.status not in {OrderStatus.PAID, OrderStatus.COMPLETED}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_NOT_REFUNDABLE)
    if order.settlement_status == OrderSettlementStatus.CONSUMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ORDER_VALUE_ALREADY_USED,
        )
    if order.refund_status == RefundStatus.REFUNDED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_NOT_REFUNDABLE)
    if await _order_value_consumed_by_later_upgrade(order, db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ORDER_VALUE_ALREADY_USED,
        )


def ensure_refund_request_allowed(order: Order) -> None:
    if order.refund_status not in {
        RefundStatus.NONE,
        RefundStatus.REJECTED,
        RefundStatus.FAILED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=REFUND_ALREADY_IN_PROGRESS,
        )


async def get_refund_eligibility(
    order: Order,
    db: AsyncSession,
    settings_row: SystemSettings,
) -> dict:
    methods: list[str] = []
    reason: str | None = None

    provider = str(order.pay_provider or "").upper()
    if provider == "EPAY":
        methods = ["original", "balance"]
    elif provider == "BALANCE":
        methods = ["balance"]

    if not getattr(settings_row, "refund_enabled", False):
        reason = REFUND_DISABLED
    elif order.type == OrderType.RECHARGE:
        reason = RECHARGE_REFUND_NOT_ALLOWED
    else:
        try:
            await ensure_order_refundable(order, db)
            ensure_refund_request_allowed(order)
            await validate_refund_policies(order, db, settings_row)
        except HTTPException as exc:
            reason = str(exc.detail)

    can_request = reason is None and bool(methods)
    if not can_request:
        methods = []
        if reason is None:
            reason = ORDER_NOT_REFUNDABLE

    return {
        "can_request": can_request,
        "available_methods": methods,
        "reason": reason,
    }


def request_refund(order: Order, refund_to: str, money: str) -> None:
    now = datetime.utcnow()
    _, provider_money, balance_credit_money = get_refund_amount_breakdown(order)
    order.refund_status = RefundStatus.PENDING
    order.refund_requested_at = now
    order.refund_reviewed_at = None
    order.refund_reviewed_by = None
    order.refund_reject_reason = None
    merge_refund_state(
        order,
        refund_to=refund_to,
        money=money,
        provider_money=f"{provider_money:.2f}",
        balance_credit_money=f"{balance_credit_money:.2f}",
        status=RefundStatus.PENDING.value,
        requested_at=now.isoformat(),
        reviewed_at=None,
        reviewed_by=None,
        reject_reason=None,
    )


def reject_refund(order: Order, reviewer_user_id: UUID, reason: str) -> None:
    clean_reason = reason.strip()
    if not clean_reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=REFUND_REJECT_REASON_REQUIRED
        )
    now = datetime.utcnow()
    order.refund_status = RefundStatus.REJECTED
    order.refund_reviewed_at = now
    order.refund_reviewed_by = reviewer_user_id
    order.refund_reject_reason = clean_reason
    merge_refund_state(
        order,
        status=RefundStatus.REJECTED.value,
        reviewed_at=now.isoformat(),
        reviewed_by=str(reviewer_user_id),
        reject_reason=clean_reason,
    )


async def refund_to_balance(
    order: Order,
    db: AsyncSession,
    *,
    operator_user_id: UUID,
    reviewer_user_id: UUID,
    money: str,
) -> None:
    user = (await db.execute(select(User).where(User.id == order.user_id))).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    total_amount, provider_money, balance_credit_money = get_refund_amount_breakdown(order)
    before_balance = float(user.balance or 0)
    delta = float(money)
    user.balance = before_balance + delta
    db.add(
        BalanceTransaction(
            user_id=user.id,
            operator_user_id=operator_user_id,
            delta=delta,
            before_balance=before_balance,
            after_balance=float(user.balance),
            reason="REFUND",
        )
    )
    db.add(user)

    await rollback_subscription_for_order(order, db)
    await sync_user_subscription_entitlements(db, user)

    now = datetime.utcnow()
    order.status = OrderStatus.REFUNDED
    order.refund_status = RefundStatus.REFUNDED
    order.settlement_status = OrderSettlementStatus.REFUNDED
    order.refunded_at = now
    order.refund_reviewed_at = now
    order.refund_reviewed_by = reviewer_user_id
    order.refund_reject_reason = None
    merge_refund_state(
        order,
        refund_to="balance",
        money=money,
        provider_money=f"{provider_money:.2f}",
        balance_credit_money=f"{balance_credit_money:.2f}",
        total_money=f"{total_amount:.2f}",
        status=RefundStatus.REFUNDED.value,
        reviewed_at=now.isoformat(),
        reviewed_by=str(reviewer_user_id),
        refunded_at=now.isoformat(),
        note="refunded to balance",
    )
    await _settle_related_source_orders_as_refunded(
        order,
        db,
        reviewer_user_id=reviewer_user_id,
        refunded_at=now,
    )


async def approve_original_refund(
    order: Order,
    settings_row: SystemSettings,
    *,
    reviewer_user_id: UUID,
    money: str,
) -> None:
    if order.pay_provider != "EPAY":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=EPAY_REFUND_NOT_SUPPORTED
        )
    if (
        not settings_row.epay_enabled
        or not settings_row.epay_gateway
        or not settings_row.epay_merchant_id
        or not settings_row.epay_key
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="易支付未配置")

    out_refund_no = (
        f"RF{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{random.randint(100000, 999999)}"
    )
    total_amount, provider_money, balance_credit_money = get_refund_amount_breakdown(order)
    provider_money_str = f"{provider_money:.2f}"
    now = datetime.utcnow()
    order.refund_status = RefundStatus.APPROVED
    order.refund_reviewed_at = now
    order.refund_reviewed_by = reviewer_user_id
    order.refund_reject_reason = None
    merge_refund_state(
        order,
        refund_to="original",
        money=money,
        provider_money=provider_money_str,
        balance_credit_money=f"{balance_credit_money:.2f}",
        total_money=f"{total_amount:.2f}",
        status=RefundStatus.APPROVED.value,
        reviewed_at=now.isoformat(),
        reviewed_by=str(reviewer_user_id),
        out_refund_no=out_refund_no,
    )

    try:
        refund_response = await epay_refund(
            settings_row.epay_gateway,
            settings_row.epay_merchant_id,
            settings_row.epay_key,
            out_trade_no=order.order_no,
            money=provider_money_str,
            out_refund_no=out_refund_no,
        )
    except Exception as exc:  # noqa: BLE001
        order.refund_status = RefundStatus.FAILED
        merge_refund_state(
            order,
            status=RefundStatus.FAILED.value,
            refund_error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"退款请求失败：{exc}"
        ) from exc

    order.refund_status = RefundStatus.PROCESSING
    merge_refund_state(
        order,
        status=RefundStatus.PROCESSING.value,
        refund_response=refund_response,
        refund_no=refund_response.get("refund_no") if isinstance(refund_response, dict) else None,
    )


async def query_original_refund(
    order: Order,
    settings_row: SystemSettings,
    db: AsyncSession,
) -> None:
    if (
        not settings_row.epay_enabled
        or not settings_row.epay_gateway
        or not settings_row.epay_merchant_id
        or not settings_row.epay_key
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="易支付未配置")

    refund_payload = get_refund_state(order)
    out_refund_no = refund_payload.get("out_refund_no")
    refund_no = refund_payload.get("refund_no")
    if not out_refund_no and not refund_no:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未找到退款记录")

    try:
        query_response = await epay_refund_query(
            settings_row.epay_gateway,
            settings_row.epay_merchant_id,
            settings_row.epay_key,
            out_refund_no=out_refund_no,
            refund_no=refund_no,
        )
    except Exception as exc:  # noqa: BLE001
        order.refund_status = RefundStatus.FAILED
        merge_refund_state(
            order,
            status=RefundStatus.FAILED.value,
            refund_query_error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"退款查询失败：{exc}"
        ) from exc

    merge_refund_state(order, refund_query=query_response)

    provider_status = query_response.get("status") if isinstance(query_response, dict) else None
    if str(provider_status) != "1":
        return

    user = (await db.execute(select(User).where(User.id == order.user_id))).scalar()
    _, provider_money, balance_credit_money = get_refund_amount_breakdown(order)
    if user and balance_credit_money > Decimal("0.00"):
        before_balance = float(user.balance or 0)
        after_balance = before_balance + float(balance_credit_money)
        user.balance = after_balance
        db.add(user)
        db.add(
            BalanceTransaction(
                user_id=user.id,
                operator_user_id=order.refund_reviewed_by or user.id,
                delta=float(balance_credit_money),
                before_balance=before_balance,
                after_balance=after_balance,
                reason="REFUND_CREDIT",
            )
        )

    await rollback_subscription_for_order(order, db)
    if user:
        await sync_user_subscription_entitlements(db, user)
    now = datetime.utcnow()
    order.status = OrderStatus.REFUNDED
    order.refund_status = RefundStatus.REFUNDED
    order.settlement_status = OrderSettlementStatus.REFUNDED
    order.refunded_at = now
    merge_refund_state(
        order,
        status=RefundStatus.REFUNDED.value,
        provider_money=f"{provider_money:.2f}",
        balance_credit_money=f"{balance_credit_money:.2f}",
        refunded_at=now.isoformat(),
        note=(
            f"provider refunded {provider_money:.2f}, balance credited {balance_credit_money:.2f}"
            if balance_credit_money > Decimal("0.00")
            else "provider refunded"
        ),
    )
    await _settle_related_source_orders_as_refunded(
        order,
        db,
        reviewer_user_id=order.refund_reviewed_by,
        refunded_at=now,
    )
