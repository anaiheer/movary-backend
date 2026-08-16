from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pro_extensions import get_backend_extension_state
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanBillingCycle,
    PlanServerAssignment,
    PlanServerAllocationStrategy,
    PlanStatus,
    SubscriptionGroup,
)
from app.models.user import User, UserRole
from app.schemas.subscription_config import (
    SubscriptionConfigData,
    SubscriptionConfigSummary,
    SubscriptionPlanOut,
    SubscriptionPlanUpsert,
)

router = APIRouter(prefix="/admin/subscription-config", tags=["admin-subscription-config"])

DEFAULT_GROUP_KEY = "default"
DEFAULT_GROUP_NAME = "默认分组"


def _response(data: SubscriptionConfigData | dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def _load_groups(db: AsyncSession) -> list[SubscriptionGroup]:
    stmt = select(SubscriptionGroup).order_by(SubscriptionGroup.created_at.asc())
    return (await db.execute(stmt)).scalars().all()


async def _load_plans(db: AsyncSession) -> list[Plan]:
    stmt = select(Plan).order_by(Plan.created_at.asc())
    return (await db.execute(stmt)).scalars().all()


async def _load_cycles(db: AsyncSession, plan_id) -> list[PlanBillingCycle]:
    stmt = (
        select(PlanBillingCycle)
        .where(PlanBillingCycle.plan_id == plan_id)
        .order_by(PlanBillingCycle.sort_order.asc(), PlanBillingCycle.created_at.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def _assignment_count(db: AsyncSession) -> int:
    value = await db.scalar(select(func.count(PlanServerAssignment.id)))
    return int(value or 0)


def _serialize_plan(plan: Plan | None) -> SubscriptionPlanOut | None:
    if not plan:
        return None
    return SubscriptionPlanOut(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        monthly_price=float(plan.monthly_price or plan.price or 0),
        yearly_price=float(plan.annual_price) if plan.annual_price is not None else None,
        vod_movie_times=int(plan.vod_movie_times or 0),
        vod_tv_times=int(plan.vod_tv_times or 0),
        is_visible=bool(plan.is_visible),
        created_at=plan.created_at,
    )


async def _build_summary(
    db: AsyncSession, groups: list[SubscriptionGroup], plans: list[Plan]
) -> SubscriptionConfigSummary:
    notices: list[str] = []
    extra_plan_count = max(len(plans) - 1, 0)
    locked = False

    if len(groups) > 1:
        notices.append("检测到多个订阅分组，基础版仅支持单一默认计划。")
        locked = True
    if any(group.key != DEFAULT_GROUP_KEY or int(group.tier_count or 1) != 1 for group in groups):
        notices.append("检测到高级订阅分组配置，该能力属于专业版。")
        locked = True
    if len(plans) > 1:
        notices.append("检测到多个订阅计划，基础版仅支持一个公开计划。")
        locked = True
    if any(plan.group_key != DEFAULT_GROUP_KEY or int(plan.tier_level or 1) != 1 for plan in plans):
        notices.append("检测到分组等级或升级链路配置，该能力属于专业版。")
        locked = True
    if any(
        plan.trial_price is not None
        or plan.quarterly_price is not None
        or plan.semi_annual_price is not None
        or plan.lifetime_price is not None
        for plan in plans
    ):
        notices.append("检测到高级订阅周期配置，基础版轻量配置已锁定。")
        locked = True

    for plan in plans:
        cycles = await _load_cycles(db, plan.id)
        if any(
            cycle.billing_cycle not in {BillingCycle.MONTHLY, BillingCycle.YEARLY}
            for cycle in cycles
        ):
            notices.append("检测到月付/年付以外的周期配置，该能力属于专业版。")
            locked = True
            break

    if await _assignment_count(db) > 0:
        notices.append("检测到订阅计划服务器绑定配置，该能力将迁移到专业版。")
        locked = True

    summary = SubscriptionConfigSummary(
        plan_count=len(plans),
        visible_plan_count=sum(1 for plan in plans if plan.is_visible),
        group_count=len(groups),
        extra_plan_count=extra_plan_count,
        locked=locked,
        pro_data_detected=locked,
        notices=notices,
    )
    extension_state = get_backend_extension_state()
    summary.pro_subscription_extension_loaded = any(
        "advanced-subscriptions" in loaded.get("route_groups", [])
        for loaded in extension_state["loaded"]
    )
    summary.pro_subscription_admin_path = (
        "/admin/subscriptions" if summary.pro_subscription_extension_loaded else None
    )
    return summary


async def _load_config_data(db: AsyncSession) -> SubscriptionConfigData:
    groups = await _load_groups(db)
    plans = await _load_plans(db)
    summary = await _build_summary(db, groups, plans)
    return SubscriptionConfigData(
        plan=_serialize_plan(plans[0] if plans else None), summary=summary
    )


async def _ensure_default_group(db: AsyncSession) -> SubscriptionGroup:
    stmt = select(SubscriptionGroup).where(SubscriptionGroup.key == DEFAULT_GROUP_KEY)
    group = (await db.execute(stmt)).scalar()
    if group:
        group.name = DEFAULT_GROUP_NAME
        group.tier_count = 1
        db.add(group)
        await db.flush()
        return group

    group = SubscriptionGroup(key=DEFAULT_GROUP_KEY, name=DEFAULT_GROUP_NAME, tier_count=1)
    db.add(group)
    await db.flush()
    return group


async def _replace_cycles(
    db: AsyncSession, plan: Plan, *, monthly_price: float, yearly_price: float | None
) -> None:
    await db.execute(PlanBillingCycle.__table__.delete().where(PlanBillingCycle.plan_id == plan.id))
    db.add(
        PlanBillingCycle(
            plan_id=plan.id,
            billing_cycle=BillingCycle.MONTHLY,
            price=monthly_price,
            duration_days=30,
            is_default=True,
            sort_order=30,
        )
    )
    if yearly_price is not None:
        db.add(
            PlanBillingCycle(
                plan_id=plan.id,
                billing_cycle=BillingCycle.YEARLY,
                price=yearly_price,
                duration_days=365,
                is_default=False,
                sort_order=60,
            )
        )


@router.get("")
async def get_subscription_config(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    return _response(await _load_config_data(db))


@router.put("/plan")
async def upsert_subscription_plan(
    payload: SubscriptionPlanUpsert,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    groups = await _load_groups(db)
    plans = await _load_plans(db)
    summary = await _build_summary(db, groups, plans)
    if summary.locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="检测到专业版订阅数据，基础版轻量配置暂不可直接覆盖。",
        )

    await _ensure_default_group(db)
    plan = plans[0] if plans else Plan()
    plan.name = payload.name
    plan.group_key = DEFAULT_GROUP_KEY
    plan.group_name = DEFAULT_GROUP_NAME
    plan.tier_level = 1
    plan.description = payload.description
    plan.duration_days = 30
    plan.price = payload.monthly_price
    plan.default_billing_cycle = BillingCycle.MONTHLY
    plan.monthly_price = payload.monthly_price
    plan.annual_price = payload.yearly_price
    plan.trial_price = None
    plan.trial_days = None
    plan.quarterly_price = None
    plan.semi_annual_price = None
    plan.lifetime_price = None
    plan.auto_renew_enabled = False
    plan.server_allocation_strategy = PlanServerAllocationStrategy.ALL
    plan.vod_movie_times = payload.vod_movie_times
    plan.vod_tv_times = payload.vod_tv_times
    plan.vod_times = payload.vod_movie_times + payload.vod_tv_times
    plan.features = {}
    plan.is_visible = payload.is_visible
    plan.status = PlanStatus.ON
    db.add(plan)
    await db.flush()
    await _replace_cycles(
        db, plan, monthly_price=payload.monthly_price, yearly_price=payload.yearly_price
    )
    await db.commit()
    return _response(await _load_config_data(db), "基础版订阅计划已保存")


@router.delete("/plan", status_code=status.HTTP_200_OK)
async def delete_subscription_plan(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    groups = await _load_groups(db)
    plans = await _load_plans(db)
    summary = await _build_summary(db, groups, plans)
    if summary.locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="检测到专业版订阅数据，请先在专业版环境中完成清理或迁移。",
        )
    if not plans:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到基础版订阅计划")

    await db.execute(
        PlanBillingCycle.__table__.delete().where(PlanBillingCycle.plan_id == plans[0].id)
    )
    await db.delete(plans[0])
    await db.commit()
    return _response(await _load_config_data(db), "基础版订阅计划已删除")
