from datetime import datetime
from decimal import Decimal
import random
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.public_urls import get_epay_notify_url, get_epay_return_url
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.balance import BalanceTransaction
from app.models.order import Order, OrderStatus, OrderType
from app.models.subscription import Plan, PlanStatus
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.schemas.order import OrderCreate, OrderDetailResponse, OrderPayRequest, OrderResponse
from app.schemas.plan import PlanResponse
from app.services.epay import build_pay_url
from app.services.order_presenter import build_refund_eligibility
from app.services.order_runtime import finalize_stale_created_orders, try_close_remote_order
from app.services.order_chains import (
    assign_order_chain_for_creation,
    build_order_chain_snapshot_payload,
    load_order_chain_snapshot,
)
from app.services.order_summary import build_order_summary
from app.services.payments import handle_paid_order
from app.services.plan_purchase import build_purchase_preview, resolve_billing
from app.services import refunds as refund_service


router = APIRouter(prefix="/orders", tags=["orders"])


PLAN_NOT_FOUND = "订阅计划不存在或不可用"
USER_NOT_FOUND = "用户不存在"
ORDER_NOT_FOUND = "订单不存在"
ORDER_NOT_PAYABLE = "订单不可支付"
BALANCE_NOT_ENOUGH = "余额不足"
UNSUPPORTED_PAY_TYPE = "暂不支持该支付方式"
EPAY_NOT_CONFIGURED = "易支付未配置"
RECHARGE_AMOUNT_INVALID = "充值金额必须大于 0"
REFUND_DISABLED = "退款功能未启用"
ORDER_NOT_REFUNDABLE = "订单不可退款"
ORDER_NOT_PAID = "订单未支付"
REFUND_ALREADY_IN_PROGRESS = "退款处理中，请勿重复提交"
REFUND_WINDOW_EXPIRED = "退款时间窗口已过期"
INVALID_REFUND_TO = "无效的退款方式"
BALANCE_RECHARGE_NOT_ALLOWED = "充值订单不支持余额支付"
RECHARGE_REFUND_NOT_ALLOWED = "充值订单暂不支持退款，请联系管理员处理"
EPAY_REFUND_NOT_SUPPORTED = "暂不支持原路退款"


class OrderRefundRequest(BaseModel):
    refund_to: str | None = None  # "balance" | "original"


class RechargeOrderCreate(BaseModel):
    amount: Decimal


def _generate_order_no() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand = random.randint(100000, 999999)
    return f"OD{ts}{rand}"


def _serialize_order(order: Order, *, refund_eligibility=None) -> OrderResponse:
    summary = build_order_summary(order)
    return OrderResponse(
        id=order.id,
        order_chain_id=order.order_chain_id,
        root_order_id=order.root_order_id,
        parent_order_id=order.parent_order_id,
        superseded_by_order_id=order.superseded_by_order_id,
        order_no=order.order_no,
        type=order.type.value if hasattr(order.type, "value") else str(order.type),
        plan_id=order.plan_id,
        amount=float(order.amount or 0),
        currency=order.currency,
        status=order.status.value if hasattr(order.status, "value") else str(order.status),
        settlement_status=order.settlement_status.value
        if hasattr(order.settlement_status, "value")
        else str(order.settlement_status),
        user_id=order.user_id,
        paid_at=order.paid_at,
        refunded_at=order.refunded_at,
        refund_status=order.refund_status.value
        if hasattr(order.refund_status, "value")
        else str(order.refund_status),
        refund_requested_at=order.refund_requested_at,
        refund_reviewed_at=order.refund_reviewed_at,
        refund_reviewed_by=order.refund_reviewed_by,
        refund_reject_reason=order.refund_reject_reason,
        created_at=order.created_at,
        refund_eligibility=refund_eligibility,
        **summary,
    )


async def _get_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("", response_model=list[OrderResponse])
async def get_orders(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户订单列表。"""
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row, user_id=current_user["user_id"])
    result = await db.execute(
        select(Order)
        .where(Order.user_id == current_user["user_id"])
        .order_by(Order.created_at.desc())
    )
    items: list[OrderResponse] = []
    for order in result.scalars().all():
        refund_eligibility = await build_refund_eligibility(order, db, settings_row)
        items.append(_serialize_order(order, refund_eligibility=refund_eligibility))
    return items


@router.post("", response_model=dict)
async def create_order(
    payload: OrderCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建订阅订单。"""
    plan = (
        await db.execute(
            select(Plan).where(
                Plan.id == payload.plan_id,
                Plan.status == PlanStatus.ON,
                Plan.is_visible.is_(True),
            )
        )
    ).scalar()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PLAN_NOT_FOUND)

    user = (await db.execute(select(User).where(User.id == current_user["user_id"]))).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND)

    preview = await build_purchase_preview(
        db,
        user_id=current_user["user_id"],
        target_plan=plan,
        billing_cycle=payload.billing_cycle,
    )
    if not preview.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=preview.message)

    billing_cycle, duration_days, _ = await resolve_billing(db, plan, payload.billing_cycle)
    price = preview.payable_amount

    order = Order(
        user_id=current_user["user_id"],
        order_no=_generate_order_no(),
        type=OrderType.PLAN,
        plan_id=plan.id,
        amount=float(price or 0),
        currency="CNY",
        status=OrderStatus.CREATED,
        pay_provider=None,
        pay_payload={
            "plan_name": plan.name,
            "billing_cycle": billing_cycle.value,
            "duration_days": duration_days,
            "purchase_action": preview.action,
            "purchase_message": preview.message,
            "base_amount": float(preview.base_price),
            "credit_amount": float(preview.credit_amount),
            "payable_amount": float(preview.payable_amount),
            "carry_balance_amount": float(preview.carry_balance_amount),
            "source_billing_cycle": (
                preview.current_subscription.billing_cycle.value
                if preview.current_subscription
                else None
            ),
            "source_subscription_id": str(preview.current_subscription.id)
            if preview.current_subscription
            else None,
            "source_subscription_ids": preview.source_subscription_ids or [],
            "renewal_of_subscription_id": str(preview.renewal_anchor_subscription.id)
            if preview.renewal_anchor_subscription
            else None,
        },
        paid_at=None,
    )
    await assign_order_chain_for_creation(
        db,
        order,
        current_subscription_id=preview.current_subscription.id
        if preview.current_subscription
        else None,
        renewal_anchor_subscription_id=(
            preview.renewal_anchor_subscription.id if preview.renewal_anchor_subscription else None
        ),
        source_subscription_ids=[
            UUID(str(source_id)) for source_id in (preview.source_subscription_ids or [])
        ],
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    if price <= 0:
        order.pay_provider = "ZERO"
        order.pay_payload = {**(order.pay_payload or {}), "pay_type": "zero"}
        db.add(order)
        await db.commit()
        await db.refresh(order)
        await handle_paid_order(order, None, {"note": "zero price"}, db)
        return {
            "success": True,
            "message": "order paid",
            "data": {"order": _serialize_order(order)},
        }

    return {
        "success": True,
        "message": "order created",
        "data": {"order": _serialize_order(order)},
    }


@router.post("/recharge", response_model=dict)
async def create_recharge_order(
    payload: RechargeOrderCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    amount = Decimal(str(payload.amount or 0))
    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=RECHARGE_AMOUNT_INVALID,
        )

    order = Order(
        user_id=current_user["user_id"],
        order_no=_generate_order_no(),
        type=OrderType.RECHARGE,
        amount=float(amount),
        currency="CNY",
        status=OrderStatus.CREATED,
        pay_provider=None,
        pay_payload={"recharge_amount": float(amount)},
        paid_at=None,
    )
    await assign_order_chain_for_creation(db, order)
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {
        "success": True,
        "message": "recharge order created",
        "data": {"order": _serialize_order(order)},
    }


@router.get("/{order_id}", response_model=OrderDetailResponse)
async def get_order_detail(
    order_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row, order_id=order_id)
    order = (
        await db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == current_user["user_id"])
        )
    ).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)

    plan = None
    if order.plan_id:
        plan = (await db.execute(select(Plan).where(Plan.id == order.plan_id))).scalar()
    chain_orders, value_links = await load_order_chain_snapshot(db, order)
    refund_eligibility = await build_refund_eligibility(order, db, settings_row)

    return OrderDetailResponse(
        order=_serialize_order(order, refund_eligibility=refund_eligibility),
        plan=PlanResponse.model_validate(plan) if plan else None,
        pay_provider=order.pay_provider,
        pay_payload=order.pay_payload,
        order_chain=build_order_chain_snapshot_payload(order, chain_orders, value_links),
    )


@router.post("/{order_id}/pay")
async def create_pay_link(
    order_id: UUID,
    payload: OrderPayRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row, order_id=order_id)
    order = (
        await db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == current_user["user_id"])
        )
    ).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)
    if order.status != OrderStatus.CREATED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_NOT_PAYABLE)

    pay_type = payload.pay_type or "alipay"
    if pay_type == "balance":
        if order.type == OrderType.RECHARGE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=BALANCE_RECHARGE_NOT_ALLOWED,
            )

        user = (await db.execute(select(User).where(User.id == current_user["user_id"]))).scalar()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND)

        price = Decimal(str(order.amount or 0))
        balance = Decimal(str(user.balance or 0))
        if balance < price:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=BALANCE_NOT_ENOUGH)

        order.pay_payload = {**(order.pay_payload or {}), "pay_type": "balance"}
        order.pay_provider = "BALANCE"
        db.add(order)

        before_balance = balance
        after_balance = balance - price
        user.balance = after_balance
        db.add(
            BalanceTransaction(
                user_id=user.id,
                operator_user_id=user.id,
                delta=-price,
                before_balance=before_balance,
                after_balance=after_balance,
                reason="PLAN_PURCHASE",
            )
        )
        db.add(user)

        try:
            await handle_paid_order(order, None, {"note": "paid by balance"}, db, commit=False)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        await db.refresh(order)
        return {
            "success": True,
            "message": "paid by balance",
            "data": {"order": _serialize_order(order)},
        }

    if pay_type not in {"alipay", "wxpay"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=UNSUPPORTED_PAY_TYPE)

    if not settings_row.epay_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=UNSUPPORTED_PAY_TYPE)
    if (
        not settings_row.epay_gateway
        or not settings_row.epay_merchant_id
        or not settings_row.epay_key
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=EPAY_NOT_CONFIGURED)

    order.pay_payload = {**(order.pay_payload or {}), "pay_type": pay_type}
    order.pay_provider = "EPAY"
    db.add(order)
    await db.commit()
    await db.refresh(order)

    plan_name = (order.pay_payload or {}).get("plan_name") or "Plan"
    pay_params = {
        "pid": settings_row.epay_merchant_id,
        "type": pay_type,
        "out_trade_no": order.order_no,
        "notify_url": get_epay_notify_url(settings_row),
        "return_url": get_epay_return_url(settings_row),
        "name": plan_name,
        "money": f"{float(order.amount or 0):.2f}",
    }
    pay_url = build_pay_url(settings_row.epay_gateway, pay_params, settings_row.epay_key)

    return {
        "success": True,
        "message": "pay url",
        "data": {"order": _serialize_order(order), "pay_url": pay_url},
    }


@router.post("/{order_id}/close")
async def close_order(
    order_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row, order_id=order_id)
    order = (
        await db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == current_user["user_id"])
        )
    ).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)
    if order.status != OrderStatus.CREATED:
        return {"success": True, "message": "ignored", "data": {"order": _serialize_order(order)}}
    await try_close_remote_order(order, settings_row)

    order.status = OrderStatus.TIMEOUT
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {"success": True, "message": "timeout", "data": {"order": _serialize_order(order)}}


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row, order_id=order_id)
    order = (
        await db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == current_user["user_id"])
        )
    ).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)
    if order.status != OrderStatus.CREATED:
        return {"success": True, "message": "ignored", "data": {"order": _serialize_order(order)}}
    await try_close_remote_order(order, settings_row)

    order.status = OrderStatus.CANCELED
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {"success": True, "message": "canceled", "data": {"order": _serialize_order(order)}}


@router.post("/{order_id}/refund")
async def refund_my_order(
    order_id: UUID,
    payload: OrderRefundRequest = OrderRefundRequest(),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = (
        await db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == current_user["user_id"])
        )
    ).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)

    settings_row = await _get_settings(db)
    if not getattr(settings_row, "refund_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=refund_service.REFUND_DISABLED
        )

    await refund_service.ensure_order_refundable(order, db)
    refund_service.ensure_refund_request_allowed(order)
    await refund_service.validate_refund_policies(order, db, settings_row)

    refund_to = (payload.refund_to or "original").lower()
    if refund_to not in {"original", "balance"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=refund_service.INVALID_REFUND_TO
        )
    if refund_to == "original" and order.pay_provider != "EPAY":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=refund_service.EPAY_REFUND_NOT_SUPPORTED,
        )

    refund_money = refund_service.parse_refund_money(None, order)
    refund_service.request_refund(order, refund_to, refund_money)
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {
        "success": True,
        "message": "refund requested",
        "data": {"order": _serialize_order(order)},
    }
