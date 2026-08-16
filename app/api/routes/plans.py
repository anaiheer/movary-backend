from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.subscription import (
    Plan,
    PlanBillingCycle,
    PlanStatus,
    Subscription,
    SubscriptionStatus,
)
from app.schemas.plan import (
    PlanBillingCycleOut,
    PlanPurchasePreview,
    PlanResponse,
    SubscriptionResponse,
)
from app.services.plan_purchase import build_purchase_preview
from app.services.subscriptions import expire_stale_subscriptions


router = APIRouter(prefix="/plans", tags=["plans"])

PUBLIC_PLAN_CYCLES = {"TRIAL", "MONTHLY", "QUARTERLY", "SEMI_ANNUAL", "YEARLY", "LIFETIME"}


def _resolve_public_default_cycle(plan: Plan, cycles: list[PlanBillingCycleOut]) -> str:
    default_cycle = next((cycle.billing_cycle for cycle in cycles if cycle.is_default), None)
    if default_cycle:
        return default_cycle
    if plan.default_billing_cycle and plan.default_billing_cycle.value in PUBLIC_PLAN_CYCLES:
        return plan.default_billing_cycle.value
    return cycles[0].billing_cycle if cycles else "MONTHLY"


@router.get("", response_model=list[PlanResponse])
async def get_plans(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Plan)
        .where(Plan.status == PlanStatus.ON, Plan.is_visible.is_(True))
        .order_by(Plan.group_name.asc(), Plan.tier_level.asc(), Plan.created_at.asc())
    )
    result = await db.execute(stmt)
    plans = result.scalars().all()
    cycle_rows = (
        (
            await db.execute(
                select(PlanBillingCycle).order_by(
                    PlanBillingCycle.plan_id.asc(),
                    PlanBillingCycle.sort_order.asc(),
                    PlanBillingCycle.created_at.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    cycle_map: dict[str, list[PlanBillingCycleOut]] = {}
    for row in cycle_rows:
        if row.billing_cycle.value not in PUBLIC_PLAN_CYCLES:
            continue
        cycle_map.setdefault(str(row.plan_id), []).append(
            PlanBillingCycleOut(
                id=row.id,
                billing_cycle=row.billing_cycle.value,
                price=float(row.price or 0),
                duration_days=int(row.duration_days or 0),
                is_default=bool(row.is_default),
                sort_order=int(row.sort_order or 0),
            )
        )

    return [
        PlanResponse(
            id=plan.id,
            name=plan.name,
            group_key=plan.group_key,
            group_name=plan.group_name,
            tier_level=int(plan.tier_level or 1),
            description=plan.description,
            duration_days=int(plan.duration_days or 0),
            price=float(plan.price or 0),
            default_billing_cycle=_resolve_public_default_cycle(
                plan, cycle_map.get(str(plan.id), [])
            ),
            vod_times=int(plan.vod_times or 0),
            vod_movie_times=int(plan.vod_movie_times or 0),
            vod_tv_times=int(plan.vod_tv_times or 0),
            features=plan.features or {},
            trial_price=float(plan.trial_price) if plan.trial_price is not None else None,
            trial_days=plan.trial_days,
            monthly_price=float(plan.monthly_price) if plan.monthly_price is not None else None,
            quarterly_price=float(plan.quarterly_price)
            if plan.quarterly_price is not None
            else None,
            semi_annual_price=float(plan.semi_annual_price)
            if plan.semi_annual_price is not None
            else None,
            annual_price=float(plan.annual_price) if plan.annual_price is not None else None,
            lifetime_price=float(plan.lifetime_price) if plan.lifetime_price is not None else None,
            auto_renew_enabled=bool(plan.auto_renew_enabled),
            server_allocation_strategy=plan.server_allocation_strategy.value
            if plan.server_allocation_strategy
            else None,
            is_visible=bool(plan.is_visible),
            status=plan.status.value,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
            cycles=cycle_map.get(str(plan.id), []),
        )
        for plan in plans
    ]


@router.get("/me", response_model=list[SubscriptionResponse])
async def get_my_subscriptions(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.user_id == current_user["user_id"],
            Subscription.status != SubscriptionStatus.CANCELED,
        )
        .order_by(Subscription.end_at.desc(), Subscription.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    subscriptions = [subscription for subscription, _ in rows]
    await expire_stale_subscriptions(db, subscriptions)
    return [
        SubscriptionResponse(
            id=subscription.id,
            user_id=subscription.user_id,
            plan_id=subscription.plan_id,
            plan_name=plan.name if plan else None,
            group_key=plan.group_key if plan else None,
            group_name=plan.group_name if plan else None,
            tier_level=int(plan.tier_level or 1) if plan else None,
            status=subscription.status.value,
            billing_cycle=subscription.billing_cycle.value,
            start_at=subscription.start_at,
            end_at=subscription.end_at,
            vod_times_used=subscription.vod_times_used,
        )
        for subscription, plan in rows
    ]


@router.get("/{plan_id}/purchase-preview", response_model=PlanPurchasePreview)
async def get_purchase_preview(
    plan_id: UUID,
    billing_cycle: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.scalar(select(Plan).where(Plan.id == plan_id))
    if not plan or plan.status != PlanStatus.ON or not plan.is_visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅计划不存在或不可用")

    preview = await build_purchase_preview(
        db,
        user_id=current_user["user_id"],
        target_plan=plan,
        billing_cycle=billing_cycle,
    )
    return PlanPurchasePreview(**preview.as_payload())
