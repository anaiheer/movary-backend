from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.emby import EmbyServer
from app.models.invitation import Invitation
from app.models.order import Order
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanBillingCycle,
    PlanServerAllocationStrategy,
    PlanServerAssignment,
    PlanStatus,
    Subscription,
    SubscriptionStatus,
    SubscriptionGroup,
)
from app.models.user import User, UserRole
from app.schemas.plan import (
    AdminPlanCreate,
    AdminPlanOut,
    AdminPlanUpdate,
    PlanBillingCycleIn,
    PlanBillingCycleOut,
    PlanServerAssignmentOut,
    SubscriptionGroupCreate,
    SubscriptionGroupOut,
    SubscriptionGroupUpdate,
)

router = APIRouter(prefix="/admin/plans", tags=["admin-plans"])


CYCLE_DAYS: dict[BillingCycle, int] = {
    BillingCycle.TRIAL: 0,
    BillingCycle.MONTHLY: 30,
    BillingCycle.QUARTERLY: 90,
    BillingCycle.SEMI_ANNUAL: 180,
    BillingCycle.YEARLY: 365,
    BillingCycle.LIFETIME: 0,
}

CYCLE_SORT_ORDER: dict[BillingCycle, int] = {
    BillingCycle.TRIAL: 10,
    BillingCycle.MONTHLY: 30,
    BillingCycle.QUARTERLY: 40,
    BillingCycle.SEMI_ANNUAL: 50,
    BillingCycle.YEARLY: 60,
    BillingCycle.LIFETIME: 80,
}

PLAN_BILLING_CYCLES = {
    BillingCycle.TRIAL,
    BillingCycle.MONTHLY,
    BillingCycle.QUARTERLY,
    BillingCycle.SEMI_ANNUAL,
    BillingCycle.YEARLY,
    BillingCycle.LIFETIME,
}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _load_assignments(db: AsyncSession, plan_id: str) -> list[PlanServerAssignmentOut]:
    stmt = (
        select(PlanServerAssignment, EmbyServer)
        .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
        .where(PlanServerAssignment.plan_id == plan_id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        PlanServerAssignmentOut(
            server_id=row[0].server_id,
            template_emby_user_id=row[0].template_emby_user_id,
            template_emby_username=row[0].template_emby_username,
            server_name=row[1].name,
        )
        for row in rows
    ]


async def _load_cycles(db: AsyncSession, plan_id) -> list[PlanBillingCycleOut]:
    rows = (
        (
            await db.execute(
                select(PlanBillingCycle)
                .where(PlanBillingCycle.plan_id == plan_id)
                .order_by(PlanBillingCycle.sort_order.asc(), PlanBillingCycle.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        PlanBillingCycleOut(
            id=row.id,
            billing_cycle=row.billing_cycle.value,
            price=float(row.price or 0),
            duration_days=int(row.duration_days or 0),
            is_default=bool(row.is_default),
            sort_order=int(row.sort_order or 0),
        )
        for row in rows
        if row.billing_cycle in PLAN_BILLING_CYCLES
    ]


def _normalize_cycles(cycles: list[PlanBillingCycleIn] | None) -> list[PlanBillingCycleIn]:
    if not cycles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="至少需要配置一个付款周期"
        )

    normalized: list[PlanBillingCycleIn] = []
    seen: set[BillingCycle] = set()
    default_count = 0

    for item in cycles:
        cycle = _parse_plan_cycle(item.billing_cycle)
        if cycle not in PLAN_BILLING_CYCLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="默认周期必须从试用、月付、季付、半年付、年付、永久这些可售周期中选择",
            )
        if cycle in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="同一个计划下不能重复配置相同付款周期",
            )
        seen.add(cycle)
        duration_days = int(item.duration_days or 0)
        if cycle == BillingCycle.TRIAL and duration_days <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="试用周期必须设置大于 0 的试用天数"
            )
        if cycle != BillingCycle.TRIAL:
            duration_days = CYCLE_DAYS[cycle]
        if float(item.price) < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="付款周期价格不能小于 0"
            )

        is_default = bool(item.is_default)
        if is_default:
            default_count += 1

        normalized.append(
            PlanBillingCycleIn(
                billing_cycle=cycle.value,
                price=float(item.price),
                duration_days=duration_days,
                is_default=is_default,
                sort_order=item.sort_order
                if item.sort_order is not None
                else CYCLE_SORT_ORDER[cycle],
            )
        )

    if default_count != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="必须且只能设置一个默认付款周期"
        )
    return sorted(normalized, key=lambda item: (int(item.sort_order or 0), item.billing_cycle))


def _sync_plan_with_cycles(plan: Plan, cycles: list[PlanBillingCycleIn]) -> None:
    plan.default_billing_cycle = BillingCycle.MONTHLY
    plan.price = 0
    plan.duration_days = 0
    plan.trial_price = None
    plan.trial_days = None
    plan.monthly_price = None
    plan.quarterly_price = None
    plan.semi_annual_price = None
    plan.annual_price = None
    plan.lifetime_price = None

    for item in cycles:
        cycle = BillingCycle(item.billing_cycle)
        if cycle == BillingCycle.TRIAL:
            plan.trial_price = item.price
            plan.trial_days = item.duration_days
        elif cycle == BillingCycle.MONTHLY:
            plan.monthly_price = item.price
        elif cycle == BillingCycle.QUARTERLY:
            plan.quarterly_price = item.price
        elif cycle == BillingCycle.SEMI_ANNUAL:
            plan.semi_annual_price = item.price
        elif cycle == BillingCycle.YEARLY:
            plan.annual_price = item.price
        elif cycle == BillingCycle.LIFETIME:
            plan.lifetime_price = item.price

        if item.is_default:
            plan.default_billing_cycle = cycle
            plan.price = item.price
            plan.duration_days = item.duration_days


async def _apply_billing_cycles(
    db: AsyncSession,
    *,
    plan: Plan,
    cycles: list[PlanBillingCycleIn],
) -> None:
    await db.execute(PlanBillingCycle.__table__.delete().where(PlanBillingCycle.plan_id == plan.id))
    for item in cycles:
        cycle = BillingCycle(item.billing_cycle)
        db.add(
            PlanBillingCycle(
                plan_id=plan.id,
                billing_cycle=cycle,
                price=item.price,
                duration_days=item.duration_days,
                is_default=bool(item.is_default),
                sort_order=int(item.sort_order or CYCLE_SORT_ORDER[cycle]),
            )
        )


async def _load_cycle_inputs(db: AsyncSession, plan_id) -> list[PlanBillingCycleIn]:
    rows = (
        (
            await db.execute(
                select(PlanBillingCycle)
                .where(PlanBillingCycle.plan_id == plan_id)
                .order_by(PlanBillingCycle.sort_order.asc(), PlanBillingCycle.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        PlanBillingCycleIn(
            billing_cycle=row.billing_cycle.value,
            price=float(row.price or 0),
            duration_days=int(row.duration_days or 0),
            is_default=bool(row.is_default),
            sort_order=int(row.sort_order or 0),
        )
        for row in rows
        if row.billing_cycle in PLAN_BILLING_CYCLES
    ]


async def _ensure_group_slot_available(
    db: AsyncSession,
    *,
    group_key: str,
    tier_level: int,
    exclude_plan_id: str | None = None,
) -> None:
    stmt = select(Plan).where(Plan.group_key == group_key, Plan.tier_level == tier_level)
    if exclude_plan_id:
        stmt = stmt.where(Plan.id != exclude_plan_id)
    if await db.scalar(stmt):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该分组下已有相同订阅等级，请更换等级或调整分组设置",
        )


async def _apply_assignments(
    db: AsyncSession, plan_id: str, assignments: list[PlanServerAssignmentOut]
) -> None:
    await db.execute(
        PlanServerAssignment.__table__.delete().where(PlanServerAssignment.plan_id == plan_id)
    )
    for item in assignments:
        server_stmt = select(EmbyServer).where(EmbyServer.id == item.server_id)
        if not (await db.execute(server_stmt)).scalar():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="服务器不存在")
        db.add(
            PlanServerAssignment(
                plan_id=plan_id,
                server_id=item.server_id,
                template_emby_user_id=item.template_emby_user_id,
                template_emby_username=item.template_emby_username,
            )
        )


async def _plan_reference_counts(db: AsyncSession, plan_id: str) -> dict[str, int]:
    return {
        "订阅": int(
            await db.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.plan_id == plan_id)
            )
            or 0
        ),
        "订单": int(
            await db.scalar(select(func.count()).select_from(Order).where(Order.plan_id == plan_id))
            or 0
        ),
        "邀请码": int(
            await db.scalar(
                select(func.count()).select_from(Invitation).where(Invitation.plan_id == plan_id)
            )
            or 0
        ),
    }


def _parse_cycle(value: str | None, fallback: BillingCycle = BillingCycle.UNSET) -> BillingCycle:
    if not value:
        return fallback
    raw = str(value).strip().upper()
    try:
        return BillingCycle(raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="默认周期无效") from exc


def _parse_plan_cycle(value: str | None, fallback: BillingCycle | None = None) -> BillingCycle:
    cycle = _parse_cycle(value, fallback or BillingCycle.UNSET)
    if cycle not in PLAN_BILLING_CYCLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="默认周期必须从试用、月付、季付、半年付、年付、永久这些可售周期中选择",
        )
    return cycle


def _resolve_plan_default_cycle(plan: Plan | None) -> str:
    if plan and plan.default_billing_cycle in PLAN_BILLING_CYCLES:
        return plan.default_billing_cycle.value
    return BillingCycle.MONTHLY.value


async def _plan_delete_reference_counts(db: AsyncSession, plan_id: str) -> dict[str, int]:
    active_subscriptions = int(
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.plan_id == plan_id, Subscription.status == "ACTIVE")
        )
        or 0
    )
    total_subscriptions = int(
        await db.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.plan_id == plan_id)
        )
        or 0
    )
    return {
        "生效中的订阅": active_subscriptions,
        "历史订阅": max(total_subscriptions - active_subscriptions, 0),
        "订单": int(
            await db.scalar(select(func.count()).select_from(Order).where(Order.plan_id == plan_id))
            or 0
        ),
        "邀请码": int(
            await db.scalar(
                select(func.count()).select_from(Invitation).where(Invitation.plan_id == plan_id)
            )
            or 0
        ),
    }


def _trimmed(value: str | None, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name}不能为空")
    return cleaned


async def _get_group_by_id(db: AsyncSession, group_id: UUID | None) -> SubscriptionGroup:
    if not group_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请选择订阅分组")
    group = await db.get(SubscriptionGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅分组不存在")
    return group


async def _load_group_lookup(db: AsyncSession) -> dict[str, SubscriptionGroup]:
    groups = (await db.execute(select(SubscriptionGroup))).scalars().all()
    return {group.key: group for group in groups}


async def _plan_count_by_group(db: AsyncSession) -> dict[str, list[int]]:
    rows = (await db.execute(select(Plan.group_key, Plan.tier_level))).all()
    result: dict[str, list[int]] = {}
    for group_key, tier_level in rows:
        result.setdefault(group_key, []).append(int(tier_level or 1))
    return result


@router.get("/groups")
async def list_groups(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    groups = (
        (await db.execute(select(SubscriptionGroup).order_by(SubscriptionGroup.name.asc())))
        .scalars()
        .all()
    )
    grouped_tiers = await _plan_count_by_group(db)
    items = [
        SubscriptionGroupOut(
            id=group.id,
            key=group.key,
            name=group.name,
            description=group.description,
            tier_count=int(group.tier_count or 1),
            plan_count=len(grouped_tiers.get(group.key, [])),
            used_tiers=sorted(grouped_tiers.get(group.key, [])),
            created_at=group.created_at,
            updated_at=group.updated_at,
        )
        for group in groups
    ]
    return _response({"items": [item.model_dump(mode="json") for item in items]})


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: SubscriptionGroupCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    key = _trimmed(payload.key, "分组标识")
    name = _trimmed(payload.name, "分组名称")

    if await db.scalar(select(SubscriptionGroup).where(SubscriptionGroup.key == key)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="分组标识已存在")
    if await db.scalar(select(SubscriptionGroup).where(SubscriptionGroup.name == name)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="分组名称已存在")

    group = SubscriptionGroup(
        key=key,
        name=name,
        description=(payload.description or "").strip() or None,
        tier_count=int(payload.tier_count or 1),
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return _response({"id": str(group.id)})


@router.put("/groups/{group_id}")
async def update_group(
    group_id: UUID,
    payload: SubscriptionGroupUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    group = await db.get(SubscriptionGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅分组不存在")

    old_key = group.key
    old_name = group.name
    next_key = _trimmed(payload.key, "分组标识") if payload.key is not None else group.key
    next_name = _trimmed(payload.name, "分组名称") if payload.name is not None else group.name
    next_tier_count = (
        int(payload.tier_count) if payload.tier_count is not None else int(group.tier_count or 1)
    )

    if next_key != old_key and await db.scalar(
        select(SubscriptionGroup).where(
            SubscriptionGroup.key == next_key, SubscriptionGroup.id != group.id
        )
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="分组标识已存在")
    if next_name != old_name and await db.scalar(
        select(SubscriptionGroup).where(
            SubscriptionGroup.name == next_name, SubscriptionGroup.id != group.id
        )
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="分组名称已存在")

    existing_tiers = (
        (await db.execute(select(Plan.tier_level).where(Plan.group_key == old_key))).scalars().all()
    )
    max_used_tier = max([int(item or 1) for item in existing_tiers], default=0)
    if next_tier_count < max_used_tier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"当前分组已使用到 Lv.{max_used_tier}，不能把可用等级缩小到 {next_tier_count}",
        )

    group.key = next_key
    group.name = next_name
    if payload.description is not None:
        group.description = (payload.description or "").strip() or None
    group.tier_count = next_tier_count
    db.add(group)

    if next_key != old_key or next_name != old_name:
        rows = (await db.execute(select(Plan).where(Plan.group_key == old_key))).scalars().all()
        for plan in rows:
            plan.group_key = next_key
            plan.group_name = next_name
            db.add(plan)

    await db.commit()
    return _response({"id": str(group.id)})


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    group = await db.get(SubscriptionGroup, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅分组不存在")
    plan_count = int(
        await db.scalar(select(func.count()).select_from(Plan).where(Plan.group_key == group.key))
        or 0
    )
    if plan_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"当前分组下还有 {plan_count} 个订阅计划，不能删除，请先清空该分组",
        )
    await db.delete(group)
    await db.commit()
    return _response({"id": str(group.id)})


@router.get("")
async def list_plans(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    group_lookup = await _load_group_lookup(db)
    stmt = select(Plan).order_by(
        Plan.group_name.asc(), Plan.tier_level.asc(), Plan.created_at.asc()
    )
    plans = (await db.execute(stmt)).scalars().all()

    items = []
    for plan in plans:
        assignments = await _load_assignments(db, plan.id)
        cycles = await _load_cycles(db, plan.id)
        group = group_lookup.get(plan.group_key)
        items.append(
            AdminPlanOut(
                id=plan.id,
                name=plan.name,
                group_id=group.id if group else None,
                group_key=plan.group_key,
                group_name=plan.group_name,
                tier_level=int(plan.tier_level or 1),
                description=plan.description,
                duration_days=int(plan.duration_days or 0),
                price=float(plan.price or 0),
                default_billing_cycle=_resolve_plan_default_cycle(plan),
                vod_times=plan.vod_times,
                vod_movie_times=plan.vod_movie_times,
                vod_tv_times=plan.vod_tv_times,
                features=plan.features,
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
                lifetime_price=float(plan.lifetime_price)
                if plan.lifetime_price is not None
                else None,
                auto_renew_enabled=plan.auto_renew_enabled,
                server_allocation_strategy=plan.server_allocation_strategy.value
                if plan.server_allocation_strategy
                else PlanServerAllocationStrategy.ALL.value,
                is_visible=plan.is_visible,
                status=plan.status.value,
                created_at=plan.created_at,
                cycles=cycles,
                server_assignments=assignments,
            )
        )

    return _response({"items": [item.model_dump(mode="json") for item in items]})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: AdminPlanCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    group = await _get_group_by_id(db, payload.group_id)
    if int(payload.tier_level or 1) < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="订阅等级必须大于 0")
    if int(payload.tier_level or 1) > int(group.tier_count or 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="所选等级超过分组可用范围"
        )
    if await db.scalar(select(Plan).where(Plan.name == payload.name)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="订阅计划名称已存在")

    await _ensure_group_slot_available(
        db,
        group_key=group.key,
        tier_level=int(payload.tier_level or 1),
    )

    try:
        status_value = PlanStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="状态无效") from exc

    try:
        strategy_value = PlanServerAllocationStrategy(payload.server_allocation_strategy)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="服务器分配策略无效"
        ) from exc

    cycles = _normalize_cycles(payload.cycles)

    plan = Plan(
        name=payload.name.strip(),
        group_key=group.key,
        group_name=group.name,
        tier_level=int(payload.tier_level or 1),
        description=payload.description,
        duration_days=0,
        price=0,
        default_billing_cycle=BillingCycle.MONTHLY,
        vod_times=payload.vod_times,
        vod_movie_times=payload.vod_movie_times,
        vod_tv_times=payload.vod_tv_times,
        features=payload.features or {},
        auto_renew_enabled=payload.auto_renew_enabled,
        server_allocation_strategy=strategy_value,
        is_visible=payload.is_visible,
        status=status_value,
    )
    _sync_plan_with_cycles(plan, cycles)
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    await _apply_billing_cycles(db, plan=plan, cycles=cycles)
    await _apply_assignments(db, plan.id, payload.server_assignments)
    await db.commit()
    return _response({"id": str(plan.id)})


@router.put("/{plan_id}")
async def update_plan(
    plan_id: str,
    payload: AdminPlanUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    plan = await db.scalar(select(Plan).where(Plan.id == plan_id))
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅计划不存在")

    group = await _get_group_by_id(db, payload.group_id) if payload.group_id else None
    next_group_key = group.key if group else plan.group_key
    next_group_name = group.name if group else plan.group_name
    next_tier = int(
        payload.tier_level if payload.tier_level is not None else (plan.tier_level or 1)
    )
    target_group = group or await db.scalar(
        select(SubscriptionGroup).where(SubscriptionGroup.key == plan.group_key)
    )
    if target_group and next_tier > int(target_group.tier_count or 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="所选等级超过分组可用范围"
        )

    await _ensure_group_slot_available(
        db,
        group_key=next_group_key,
        tier_level=next_tier,
        exclude_plan_id=plan_id,
    )

    cycle_inputs = (
        payload.cycles if payload.cycles is not None else await _load_cycle_inputs(db, plan.id)
    )
    if payload.cycles is None and not cycle_inputs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前套餐缺少付款周期配置，请重新编辑并补齐可售周期",
        )
    cycles = _normalize_cycles(cycle_inputs)

    if payload.name is not None:
        cleaned_name = payload.name.strip()
        if not cleaned_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="订阅名称不能为空")
        plan.name = cleaned_name
    plan.group_key = next_group_key
    plan.group_name = next_group_name
    plan.tier_level = next_tier
    if payload.description is not None:
        plan.description = payload.description
    if payload.vod_times is not None:
        plan.vod_times = payload.vod_times
    if payload.vod_movie_times is not None:
        plan.vod_movie_times = payload.vod_movie_times
    if payload.vod_tv_times is not None:
        plan.vod_tv_times = payload.vod_tv_times
    if payload.features is not None:
        plan.features = payload.features
    if payload.auto_renew_enabled is not None:
        plan.auto_renew_enabled = payload.auto_renew_enabled
    if payload.server_allocation_strategy is not None:
        try:
            plan.server_allocation_strategy = PlanServerAllocationStrategy(
                payload.server_allocation_strategy
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="服务器分配策略无效"
            ) from exc
    if payload.is_visible is not None:
        plan.is_visible = payload.is_visible
    if payload.status is not None:
        try:
            plan.status = PlanStatus(payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="状态无效") from exc

    if payload.server_assignments is not None:
        await _apply_assignments(db, plan.id, payload.server_assignments)

    _sync_plan_with_cycles(plan, cycles)
    await _apply_billing_cycles(db, plan=plan, cycles=cycles)
    db.add(plan)
    await db.commit()
    return _response({"id": str(plan.id)})


@router.delete("/{plan_id}")
async def delete_plan(
    plan_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅计划不存在")

    now = datetime.utcnow()
    effective_active_subscriptions = int(
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.plan_id == plan.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.start_at <= now,
                Subscription.end_at > now,
            )
        )
        or 0
    )
    if effective_active_subscriptions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"当前仍有 {effective_active_subscriptions} 个生效中的订阅正在使用该计划，不能删除",
        )

    await db.execute(
        Subscription.__table__.delete().where(
            Subscription.plan_id == plan.id,
            ~(
                (Subscription.status == SubscriptionStatus.ACTIVE)
                & (Subscription.start_at <= now)
                & (Subscription.end_at > now)
            ),
        )
    )
    await db.execute(update(Order).where(Order.plan_id == plan.id).values(plan_id=None))
    await db.execute(update(Invitation).where(Invitation.plan_id == plan.id).values(plan_id=None))

    await db.execute(PlanBillingCycle.__table__.delete().where(PlanBillingCycle.plan_id == plan.id))
    await db.execute(
        PlanServerAssignment.__table__.delete().where(PlanServerAssignment.plan_id == plan.id)
    )
    await db.delete(plan)
    await db.commit()
    return _response({"id": str(plan.id)})
