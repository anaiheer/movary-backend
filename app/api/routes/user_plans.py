from __future__ import annotations

from datetime import datetime
import random
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.order import Order, OrderStatus, OrderType
from app.models.subscription import Plan, PlanStatus
from app.models.user import User
from app.schemas.subscription_config import (
    UserOrderCreate,
    UserPlanItem,
    UserPlanPreview,
)
from app.schemas.order import OrderResponse
from app.services.order_chains import assign_order_chain_for_creation
from app.services.order_summary import build_order_summary
from app.services.payments import handle_paid_order
from app.services.plan_purchase import build_purchase_preview, resolve_billing

router = APIRouter(prefix="/plans", tags=["user-plans"])

ALLOWED_BILLING_CYCLES = {"MONTHLY", "YEARLY"}


def _order_response(order: Order) -> OrderResponse:
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
        **summary,
    )


def _generate_order_no() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand = random.randint(100000, 999999)
    return f"OD{ts}{rand}"


async def _load_visible_plan(db: AsyncSession, plan_id: UUID | None = None) -> Plan | None:
    stmt = select(Plan).where(Plan.status == PlanStatus.ON, Plan.is_visible.is_(True))
    if plan_id:
        stmt = stmt.where(Plan.id == plan_id)
    plans = (await db.execute(stmt.order_by(Plan.created_at.asc()))).scalars().all()
    visible = [
        plan
        for plan in plans
        if plan.group_key == "default" and int(plan.tier_level or 1) == 1
    ]
    return visible[0] if visible else None


def _serialize_plan(plan: Plan) -> UserPlanItem:
    return UserPlanItem(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        monthly_price=float(plan.monthly_price or plan.price or 0),
        yearly_price=float(plan.annual_price) if plan.annual_price is not None else None,
        vod_movie_times=int(plan.vod_movie_times or 0),
        vod_tv_times=int(plan.vod_tv_times or 0),
        is_visible=bool(plan.is_visible),
    )


@router.get("", response_model=list[UserPlanItem])
async def list_user_plans(db: AsyncSession = Depends(get_db)):
    plan = await _load_visible_plan(db)
    return [_serialize_plan(plan)] if plan else []


@router.get("/{plan_id}/preview", response_model=UserPlanPreview)
async def preview_user_plan(
    plan_id: UUID,
    billing_cycle: str = Query("MONTHLY"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if billing_cycle not in ALLOWED_BILLING_CYCLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="基础版仅支持月付或年付"
        )

    plan = await _load_visible_plan(db, plan_id=plan_id)
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="基础版订阅计划不存在或不可用"
        )

    preview = await build_purchase_preview(
        db,
        user_id=current_user["user_id"],
        target_plan=plan,
        billing_cycle=billing_cycle,
    )
    duration_cycle, duration_days, _ = await resolve_billing(db, plan, billing_cycle)
    return UserPlanPreview(
        allowed=preview.allowed,
        action=preview.action,
        message=preview.message,
        billing_cycle=duration_cycle.value,
        duration_days=duration_days,
        price=float(preview.payable_amount),
        button_label=preview.button_label,
        plan=_serialize_plan(plan),
    )


@router.post("/orders")
async def create_user_order(
    payload: UserOrderCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.billing_cycle not in ALLOWED_BILLING_CYCLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="基础版仅支持月付或年付"
        )

    plan = await _load_visible_plan(db, plan_id=payload.plan_id)
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="基础版订阅计划不存在或不可用"
        )

    user = (await db.execute(select(User).where(User.id == current_user["user_id"]))).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    preview = await build_purchase_preview(
        db,
        user_id=current_user["user_id"],
        target_plan=plan,
        billing_cycle=payload.billing_cycle,
    )
    if not preview.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=preview.message)

    billing_cycle, duration_days, _ = await resolve_billing(db, plan, payload.billing_cycle)
    order = Order(
        user_id=current_user["user_id"],
        order_no=_generate_order_no(),
        type=OrderType.PLAN,
        plan_id=plan.id,
        amount=float(preview.payable_amount or 0),
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
        },
        paid_at=None,
    )
    await assign_order_chain_for_creation(db, order)
    db.add(order)
    await db.commit()
    await db.refresh(order)

    if Decimal(str(preview.payable_amount or 0)) <= 0:
        order.pay_provider = "ZERO"
        order.pay_payload = {**(order.pay_payload or {}), "pay_type": "zero"}
        db.add(order)
        await db.commit()
        await db.refresh(order)
        await handle_paid_order(order, None, {"note": "zero price"}, db)

    return {
        "success": True,
        "message": "order created",
        "data": {"order": _order_response(order)},
    }
