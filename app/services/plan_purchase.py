from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus, OrderType, RefundStatus
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanBillingCycle,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import User


MONEY_QUANT = Decimal("0.01")
PLAN_BILLING_CYCLES = {
    BillingCycle.TRIAL,
    BillingCycle.MONTHLY,
    BillingCycle.QUARTERLY,
    BillingCycle.SEMI_ANNUAL,
    BillingCycle.YEARLY,
    BillingCycle.LIFETIME,
}


@dataclass
class PurchasePreview:
    allowed: bool
    action: str
    message: str
    button_label: str
    billing_cycle: BillingCycle
    duration_days: int
    base_price: Decimal
    credit_amount: Decimal
    payable_amount: Decimal
    carry_balance_amount: Decimal
    target_plan: Plan
    current_subscription: Subscription | None = None
    current_plan: Plan | None = None
    renewal_anchor_subscription: Subscription | None = None
    source_subscription_ids: list[str] | None = None

    def as_payload(self) -> dict:
        current = None
        if self.current_subscription and self.current_plan:
            current = {
                "id": self.current_subscription.id,
                "plan_id": self.current_subscription.plan_id,
                "plan_name": self.current_plan.name,
                "group_name": self.current_plan.group_name,
                "tier_level": int(self.current_plan.tier_level or 1),
                "billing_cycle": self.current_subscription.billing_cycle.value,
                "start_at": self.current_subscription.start_at,
                "end_at": self.current_subscription.end_at,
            }
        return {
            "allowed": self.allowed,
            "action": self.action,
            "message": self.message,
            "billing_cycle": self.billing_cycle.value,
            "duration_days": self.duration_days,
            "base_price": float(self.base_price),
            "credit_amount": float(self.credit_amount),
            "payable_amount": float(self.payable_amount),
            "carry_balance_amount": float(self.carry_balance_amount),
            "button_label": self.button_label,
            "target_plan": {
                "id": self.target_plan.id,
                "name": self.target_plan.name,
                "group_key": self.target_plan.group_key,
                "group_name": self.target_plan.group_name,
                "tier_level": int(self.target_plan.tier_level or 1),
            },
            "current_subscription": current,
            "source_subscription_ids": self.source_subscription_ids or [],
        }


def _quantize_money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _add_months(anchor: datetime, months: int) -> datetime:
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def get_cycle_end_at(
    anchor: datetime,
    cycle: BillingCycle,
    *,
    trial_days: int = 0,
    custom_days: int = 0,
) -> datetime:
    if cycle == BillingCycle.MONTHLY:
        return _add_months(anchor, 1)
    if cycle == BillingCycle.QUARTERLY:
        return _add_months(anchor, 3)
    if cycle == BillingCycle.SEMI_ANNUAL:
        return _add_months(anchor, 6)
    if cycle == BillingCycle.YEARLY:
        return _add_months(anchor, 12)
    if cycle == BillingCycle.LIFETIME:
        return anchor + timedelta(days=36500)
    if cycle == BillingCycle.TRIAL:
        return anchor + timedelta(days=max(int(trial_days or 0), 0))
    return anchor + timedelta(days=max(int(custom_days or 0), 0))


def get_cycle_duration_days(
    anchor: datetime,
    cycle: BillingCycle,
    *,
    trial_days: int = 0,
    custom_days: int = 0,
) -> int:
    if cycle == BillingCycle.LIFETIME:
        return 0
    end_at = get_cycle_end_at(
        anchor,
        cycle,
        trial_days=trial_days,
        custom_days=custom_days,
    )
    return max(int((end_at - anchor).total_seconds() // 86400), 0)


async def resolve_billing(
    db: AsyncSession,
    plan: Plan,
    billing_cycle: str | None,
    *,
    anchor_time: datetime | None = None,
) -> tuple[BillingCycle, int, Decimal]:
    anchor = anchor_time or datetime.utcnow()
    cycle_rows = (
        (
            await db.execute(
                select(PlanBillingCycle)
                .where(PlanBillingCycle.plan_id == plan.id)
                .order_by(PlanBillingCycle.sort_order.asc(), PlanBillingCycle.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    cycle_rows = [row for row in cycle_rows if row.billing_cycle in PLAN_BILLING_CYCLES]
    if not cycle_rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前套餐缺少付款周期配置，请联系管理员重新配置",
        )

    if billing_cycle:
        raw = str(billing_cycle).strip().upper()
        try:
            target_cycle = BillingCycle(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的付款周期",
            ) from exc
        selected_row = next((row for row in cycle_rows if row.billing_cycle == target_cycle), None)
    else:
        selected_row = next((row for row in cycle_rows if row.is_default), None) or cycle_rows[0]

    if not selected_row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该套餐未配置所选付款周期",
        )

    cycle = selected_row.billing_cycle
    duration_days = int(selected_row.duration_days or 0)
    if cycle == BillingCycle.TRIAL and duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="试用周期必须配置大于 0 的试用天数",
        )
    if cycle == BillingCycle.UNSET and duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="自定义周期必须配置大于 0 的有效天数",
        )
    if cycle != BillingCycle.TRIAL:
        duration_days = get_cycle_duration_days(anchor, cycle)

    return cycle, duration_days, _quantize_money(selected_row.price or 0)


async def _load_group_subscriptions(
    db: AsyncSession,
    *,
    user_id,
    group_key: str,
    now: datetime | None = None,
) -> list[tuple[Subscription, Plan]]:
    current = now or datetime.utcnow()
    stmt = (
        select(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > current,
            Plan.group_key == group_key,
        )
        .order_by(
            Subscription.start_at.asc(),
            Subscription.end_at.asc(),
            Subscription.created_at.asc(),
        )
    )
    return (await db.execute(stmt)).all()


async def _load_group_subscription_history(
    db: AsyncSession,
    *,
    user_id,
    group_key: str,
) -> list[tuple[Subscription, Plan]]:
    stmt = (
        select(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.user_id == user_id,
            Plan.group_key == group_key,
        )
        .order_by(
            Subscription.end_at.asc(),
            Subscription.created_at.asc(),
        )
    )
    return (await db.execute(stmt)).all()


async def _is_user_trial_blocked(
    db: AsyncSession,
    *,
    user_id,
) -> bool:
    stmt = select(User.trial_used).where(User.id == user_id)
    return bool(await db.scalar(stmt))


async def _load_source_orders_for_subscription(db: AsyncSession, subscription_id) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.subscription_id == subscription_id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED]),
        )
        .order_by(Order.paid_at.asc(), Order.created_at.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def _load_group_plan_orders(
    db: AsyncSession,
    *,
    user_id,
    group_key: str,
) -> list[tuple[Order, Plan]]:
    stmt = (
        select(Order, Plan)
        .join(Plan, Plan.id == Order.plan_id)
        .where(
            Order.user_id == user_id,
            Order.type == OrderType.PLAN,
            Plan.group_key == group_key,
        )
        .order_by(Order.paid_at.asc(), Order.created_at.asc())
    )
    return (await db.execute(stmt)).all()


def _order_still_holds_lifetime_lock(order: Order) -> bool:
    payload = order.pay_payload or {}
    billing_cycle = str(payload.get("billing_cycle") or "").upper()
    if billing_cycle != BillingCycle.LIFETIME.value:
        return False
    if order.status not in {OrderStatus.PAID, OrderStatus.COMPLETED}:
        return False
    if order.status == OrderStatus.REFUNDED:
        return False
    if order.refund_status == RefundStatus.REFUNDED:
        return False
    if order.refunded_at is not None:
        return False
    return True


def _successful_plan_order_cycle(order: Order) -> BillingCycle | None:
    if order.status not in {OrderStatus.PAID, OrderStatus.COMPLETED, OrderStatus.REFUNDED}:
        return None

    raw_cycle = str((order.pay_payload or {}).get("billing_cycle") or "").upper()
    if not raw_cycle:
        return None

    try:
        cycle = BillingCycle(raw_cycle)
    except ValueError:
        return None

    return cycle if cycle in PLAN_BILLING_CYCLES else None


async def compute_subscription_remaining_credit(
    db: AsyncSession,
    subscription: Subscription,
    plan: Plan,
    *,
    now: datetime | None = None,
) -> Decimal | None:
    current = now or datetime.utcnow()
    if subscription.end_at <= current:
        return Decimal("0.00")

    source_orders = await _load_source_orders_for_subscription(db, subscription.id)
    if source_orders:
        original_amount = Decimal("0.00")
        duration_days = 0
        for source_order in source_orders:
            payload = source_order.pay_payload or {}
            original_amount += _quantize_money(
                payload.get("base_amount") or source_order.amount or 0
            )
            duration_days += max(int(payload.get("duration_days") or 0), 0)
    else:
        cycle = subscription.billing_cycle.value if subscription.billing_cycle else None
        _, duration_days, original_amount = await resolve_billing(
            db, plan, cycle, anchor_time=subscription.start_at
        )

    if subscription.billing_cycle == BillingCycle.LIFETIME:
        return _quantize_money(original_amount)

    total_seconds = max((subscription.end_at - subscription.start_at).total_seconds(), 0)
    if total_seconds <= 0 and duration_days > 0:
        total_seconds = duration_days * 86400
    if total_seconds <= 0:
        return None

    remaining_seconds = max((subscription.end_at - current).total_seconds(), 0)
    credit = (original_amount * Decimal(str(remaining_seconds / total_seconds))).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_UP
    )
    if credit < Decimal("0.00"):
        return Decimal("0.00")
    if credit > original_amount:
        return original_amount
    return credit


async def _sum_remaining_credit(
    db: AsyncSession,
    rows: list[tuple[Subscription, Plan]],
    *,
    now: datetime,
) -> Decimal | None:
    total = Decimal("0.00")
    for subscription, plan in rows:
        credit = await compute_subscription_remaining_credit(db, subscription, plan, now=now)
        if credit is None:
            return None
        total += credit
    return _quantize_money(total)


async def build_purchase_preview(
    db: AsyncSession,
    *,
    user_id,
    target_plan: Plan,
    billing_cycle: str | None,
    now: datetime | None = None,
) -> PurchasePreview:
    current = now or datetime.utcnow()
    cycle, duration_days, base_price = await resolve_billing(
        db, target_plan, billing_cycle, anchor_time=current
    )
    group_order_rows = await _load_group_plan_orders(
        db, user_id=user_id, group_key=target_plan.group_key
    )
    group_history_rows = await _load_group_subscription_history(
        db, user_id=user_id, group_key=target_plan.group_key
    )
    group_rows = [
        (subscription, plan)
        for subscription, plan in group_history_rows
        if subscription.status == SubscriptionStatus.ACTIVE and subscription.end_at > current
    ]
    effective_rows = [
        (subscription, plan)
        for subscription, plan in group_rows
        if subscription.start_at <= current < subscription.end_at
    ]
    current_row = effective_rows[-1] if effective_rows else None
    latest_row = group_rows[-1] if group_rows else None
    source_subscription_ids = [str(subscription.id) for subscription, _ in group_rows]
    current_subscription = current_row[0] if current_row else None
    current_plan = current_row[1] if current_row else None
    current_tier = int(current_plan.tier_level or 1) if current_plan else 0
    target_tier = int(target_plan.tier_level or 1)
    latest_subscription = latest_row[0] if latest_row else None
    latest_plan = latest_row[1] if latest_row else None
    latest_tier = int(latest_plan.tier_level or 1) if latest_plan else 0
    can_upgrade_active_trial_to_trial = bool(
        current_subscription
        and current_subscription.billing_cycle == BillingCycle.TRIAL
        and cycle == BillingCycle.TRIAL
        and target_tier > current_tier
    )
    successful_group_order_cycles = []
    for order, _ in group_order_rows:
        order_cycle = _successful_plan_order_cycle(order)
        if order_cycle is not None:
            successful_group_order_cycles.append(order_cycle)
    historical_subscription_cycles = [
        subscription.billing_cycle
        for subscription, _ in group_history_rows
        if subscription.billing_cycle in PLAN_BILLING_CYCLES
    ]
    group_trial_used = any(
        order_cycle == BillingCycle.TRIAL for order_cycle in successful_group_order_cycles
    ) or any(cycle == BillingCycle.TRIAL for cycle in historical_subscription_cycles)
    group_formal_purchased = any(
        order_cycle != BillingCycle.TRIAL for order_cycle in successful_group_order_cycles
    ) or any(cycle != BillingCycle.TRIAL for cycle in historical_subscription_cycles)
    has_non_refunded_lifetime_order = any(
        _order_still_holds_lifetime_lock(order) for order, _ in group_order_rows
    )
    user_trial_blocked = await _is_user_trial_blocked(db, user_id=user_id)

    def build_preview(
        *,
        allowed: bool,
        action: str,
        message: str,
        button_label: str,
        credit_amount: Decimal = Decimal("0.00"),
        payable_amount: Decimal | None = None,
        carry_balance_amount: Decimal = Decimal("0.00"),
        current_subscription: Subscription | None = None,
        current_plan: Plan | None = None,
        renewal_anchor_subscription: Subscription | None = None,
        source_ids: list[str] | None = None,
    ) -> PurchasePreview:
        return PurchasePreview(
            allowed=allowed,
            action=action,
            message=message,
            button_label=button_label,
            billing_cycle=cycle,
            duration_days=duration_days,
            base_price=base_price,
            credit_amount=credit_amount,
            payable_amount=base_price if payable_amount is None else payable_amount,
            carry_balance_amount=carry_balance_amount,
            target_plan=target_plan,
            current_subscription=current_subscription,
            current_plan=current_plan,
            renewal_anchor_subscription=renewal_anchor_subscription,
            source_subscription_ids=source_ids,
        )

    if cycle == BillingCycle.TRIAL:
        if user_trial_blocked:
            return build_preview(
                allowed=False,
                action="BLOCKED",
                message="当前账号已被标记为禁止试用，无法购买试用套餐。",
                button_label="不可试用",
                current_subscription=current_subscription,
                current_plan=current_plan,
            )
        if group_formal_purchased:
            return build_preview(
                allowed=False,
                action="BLOCKED",
                message="当前计划组已购买过正式套餐，不能再购买试用套餐。",
                button_label="不可试用",
                current_subscription=current_subscription,
                current_plan=current_plan,
            )
        if group_trial_used and not can_upgrade_active_trial_to_trial:
            return build_preview(
                allowed=False,
                action="BLOCKED",
                message="当前计划组的试用套餐仅可购买一次，到期后不能再次购买。",
                button_label="不可试用",
                current_subscription=current_subscription,
                current_plan=current_plan,
            )

    if has_non_refunded_lifetime_order:
        can_upgrade_lifetime_to_higher_tier = bool(
            cycle == BillingCycle.LIFETIME
            and (
                (
                    current_subscription
                    and current_subscription.billing_cycle == BillingCycle.LIFETIME
                    and target_tier > current_tier
                )
                or (
                    latest_subscription
                    and latest_subscription.billing_cycle == BillingCycle.LIFETIME
                    and target_tier > latest_tier
                )
            )
        )
        if can_upgrade_lifetime_to_higher_tier:
            credit = await _sum_remaining_credit(db, group_rows, now=current)
            if credit is None:
                return build_preview(
                    allowed=False,
                    action="BLOCKED",
                    message="当前永久套餐无法自动折算升级金额，请联系管理员处理。",
                    button_label="不可升级",
                    current_subscription=current_subscription or latest_subscription,
                    current_plan=current_plan or latest_plan,
                )

            payable = max(base_price - credit, Decimal("0.00"))
            carry_balance = max(credit - base_price, Decimal("0.00"))
            message = (
                f"将按当前永久套餐剩余价值抵扣 {credit:.2f} 元后升级到更高等级永久套餐。"
                if payable > Decimal("0.00")
                else f"当前永久套餐剩余价值可覆盖升级金额，超出的 {carry_balance:.2f} 元将转入余额。"
            )
            return build_preview(
                allowed=True,
                action="UPGRADE",
                message=message,
                button_label="创建永久升级订单",
                credit_amount=credit,
                payable_amount=payable,
                carry_balance_amount=carry_balance,
                current_subscription=current_subscription or latest_subscription,
                current_plan=current_plan or latest_plan,
                source_ids=source_subscription_ids,
            )
        return build_preview(
            allowed=False,
            action="BLOCKED",
            message="当前计划组已购买永久套餐，退款成功前不能再次购买其他周期或永久周期。",
            button_label="不可购买",
            current_subscription=current_subscription,
            current_plan=current_plan,
        )

    if current_row:
        if target_tier < current_tier:
            return build_preview(
                allowed=False,
                action="BLOCKED",
                message="同分组内仅支持续费或升级，不能订阅更低等级套餐。",
                button_label="不可购买",
                current_subscription=current_subscription,
                current_plan=current_plan,
            )

        if target_tier == current_tier:
            if target_plan.id != current_plan.id:
                return build_preview(
                    allowed=False,
                    action="BLOCKED",
                    message="当前分组已存在同等级计划，请继续当前计划或升级到更高等级。",
                    button_label="不可购买",
                    current_subscription=current_subscription,
                    current_plan=current_plan,
                )

            if current_subscription.billing_cycle == BillingCycle.TRIAL:
                if cycle == BillingCycle.TRIAL:
                    return build_preview(
                        allowed=False,
                        action="BLOCKED",
                        message="试用套餐不可续费，可升级到更高等级或开通正式订阅。",
                        button_label="不可续费",
                        current_subscription=current_subscription,
                        current_plan=current_plan,
                    )

                credit = await _sum_remaining_credit(db, group_rows, now=current)
                if credit is None:
                    return build_preview(
                        allowed=False,
                        action="BLOCKED",
                        message="当前试用套餐无法自动折算，请联系管理员处理。",
                        button_label="不可购买",
                        current_subscription=current_subscription,
                        current_plan=current_plan,
                    )

                payable = max(base_price - credit, Decimal("0.00"))
                carry_balance = max(credit - base_price, Decimal("0.00"))
                return build_preview(
                    allowed=True,
                    action="REPLACE_TRIAL",
                    message="购买正式订阅后会覆盖当前试用套餐，并自动折算试用剩余价值。",
                    button_label="创建正式订阅订单",
                    credit_amount=credit,
                    payable_amount=payable,
                    carry_balance_amount=carry_balance,
                    current_subscription=current_subscription,
                    current_plan=current_plan,
                    source_ids=source_subscription_ids,
                )

            if (
                cycle == BillingCycle.LIFETIME
                and current_subscription.billing_cycle != BillingCycle.LIFETIME
            ):
                credit = await _sum_remaining_credit(db, group_rows, now=current)
                if credit is None:
                    return build_preview(
                        allowed=False,
                        action="BLOCKED",
                        message="当前订阅无法自动折算升级到永久周期的金额，请联系管理员处理。",
                        button_label="不可购买",
                        current_subscription=current_subscription,
                        current_plan=current_plan,
                    )

                payable = max(base_price - credit, Decimal("0.00"))
                carry_balance = max(credit - base_price, Decimal("0.00"))
                message = (
                    f"将按剩余价值抵扣 {credit:.2f} 元后切换为永久周期。"
                    if payable > Decimal("0.00")
                    else f"当前剩余价值可覆盖永久周期金额，超出的 {carry_balance:.2f} 元将转入余额。"
                )
                return build_preview(
                    allowed=True,
                    action="UPGRADE",
                    message=message,
                    button_label="创建永久套餐订单",
                    credit_amount=credit,
                    payable_amount=payable,
                    carry_balance_amount=carry_balance,
                    current_subscription=current_subscription,
                    current_plan=current_plan,
                    source_ids=source_subscription_ids,
                )

            latest_subscription = latest_row[0] if latest_row else current_subscription
            return build_preview(
                allowed=True,
                action="RENEW",
                message="当前购买会作为续期生效，在现有到期时间后继续增加时长。",
                button_label="创建续费订单",
                current_subscription=current_subscription,
                current_plan=current_plan,
                renewal_anchor_subscription=latest_subscription,
            )

        credit = await _sum_remaining_credit(db, group_rows, now=current)
        if credit is None:
            return build_preview(
                allowed=False,
                action="BLOCKED",
                message="当前订阅无法自动折算升级金额，请联系管理员处理。",
                button_label="不可升级",
                current_subscription=current_subscription,
                current_plan=current_plan,
            )

        payable = max(base_price - credit, Decimal("0.00"))
        carry_balance = max(credit - base_price, Decimal("0.00"))
        message = (
            f"将按剩余价值抵扣 {credit:.2f} 元后升级。"
            if payable > Decimal("0.00")
            else f"当前剩余价值可覆盖升级金额，超出的 {carry_balance:.2f} 元将转入余额。"
        )
        return build_preview(
            allowed=True,
            action="UPGRADE",
            message=message,
            button_label="创建升级订单",
            credit_amount=credit,
            payable_amount=payable,
            carry_balance_amount=carry_balance,
            current_subscription=current_subscription,
            current_plan=current_plan,
            source_ids=source_subscription_ids,
        )

    if latest_row:
        latest_subscription, latest_plan = latest_row
        latest_tier = int(latest_plan.tier_level or 1)
        target_tier = int(target_plan.tier_level or 1)

        if target_tier < latest_tier:
            return build_preview(
                allowed=False,
                action="BLOCKED",
                message="当前分组已存在更高等级的待生效订阅，不能购买更低等级套餐。",
                button_label="不可购买",
                current_subscription=latest_subscription,
                current_plan=latest_plan,
            )

        if target_tier == latest_tier and target_plan.id == latest_plan.id:
            if (
                latest_subscription.billing_cycle == BillingCycle.TRIAL
                and cycle == BillingCycle.TRIAL
            ):
                return build_preview(
                    allowed=False,
                    action="BLOCKED",
                    message="试用套餐不可续费，可升级到更高等级或开通正式订阅。",
                    button_label="不可续费",
                    current_subscription=latest_subscription,
                    current_plan=latest_plan,
                )
            return build_preview(
                allowed=True,
                action="RENEW",
                message="同分组订阅已排队，本次购买会继续追加到当前续费链路后面。",
                button_label="创建续费订单",
                current_subscription=latest_subscription,
                current_plan=latest_plan,
                renewal_anchor_subscription=latest_subscription,
            )

        if target_tier > latest_tier:
            credit = await _sum_remaining_credit(db, group_rows, now=current)
            if credit is None:
                return build_preview(
                    allowed=False,
                    action="BLOCKED",
                    message="当前待生效订阅无法自动折算升级金额，请联系管理员处理。",
                    button_label="不可升级",
                    current_subscription=latest_subscription,
                    current_plan=latest_plan,
                )

            payable = max(base_price - credit, Decimal("0.00"))
            carry_balance = max(credit - base_price, Decimal("0.00"))
            return build_preview(
                allowed=True,
                action="UPGRADE",
                message="检测到同分组待生效订阅，升级时会统一折算后切换到更高等级。",
                button_label="创建升级订单",
                credit_amount=credit,
                payable_amount=payable,
                carry_balance_amount=carry_balance,
                current_subscription=latest_subscription,
                current_plan=latest_plan,
                source_ids=source_subscription_ids,
            )

        return build_preview(
            allowed=False,
            action="BLOCKED",
            message="当前分组已存在待生效订阅，请等待其生效后再调整套餐。",
            button_label="不可购买",
            current_subscription=latest_subscription,
            current_plan=latest_plan,
        )

    return build_preview(
        allowed=True,
        action="DIRECT_PURCHASE",
        message="这是一个新的服务分组，可与其他分组的订阅并存。",
        button_label="创建订单",
    )
