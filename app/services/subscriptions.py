from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSettings
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.user import User
from app.models.vod import VodRequest
from app.services.emby_accounts import (
    disable_orphaned_emby_accounts_for_user,
    reconcile_emby_accounts_for_user,
)
from app.services.telegram import create_telegram_notification


def is_subscription_active(subscription: Subscription, now: datetime | None = None) -> bool:
    current = now or datetime.utcnow()
    return (
        subscription.status == SubscriptionStatus.ACTIVE
        and subscription.start_at <= current
        and subscription.end_at > current
    )


async def get_active_subscription_for_user(
    db: AsyncSession,
    user_id,
    *,
    now: datetime | None = None,
) -> Subscription | None:
    subscriptions = await get_effective_subscriptions_for_user(db, user_id, now=now)
    return subscriptions[-1] if subscriptions else None


async def get_effective_subscriptions_for_user(
    db: AsyncSession,
    user_id,
    *,
    now: datetime | None = None,
) -> list[Subscription]:
    current = now or datetime.utcnow()
    stmt = (
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.start_at <= current,
            Subscription.end_at > current,
        )
        .order_by(Subscription.end_at.asc(), Subscription.created_at.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def get_effective_subscription_rows_for_user(
    db: AsyncSession,
    user_id,
    *,
    now: datetime | None = None,
) -> list[tuple[Subscription, Plan]]:
    current = now or datetime.utcnow()
    stmt = (
        select(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.start_at <= current,
            Subscription.end_at > current,
        )
        .order_by(Subscription.end_at.asc(), Subscription.created_at.asc())
    )
    return (await db.execute(stmt)).all()


async def get_vod_quota_subscription_for_user(
    db: AsyncSession,
    user_id,
    media_type: str,
    *,
    now: datetime | None = None,
) -> Subscription | None:
    active_rows = await get_effective_subscription_rows_for_user(db, user_id, now=now)
    if not active_rows:
        return None

    count_stmt = (
        select(
            VodRequest.subscription_id,
            func.count(VodRequest.id),
        )
        .where(
            VodRequest.subscription_id.in_([subscription.id for subscription, _ in active_rows]),
            VodRequest.quota_consumed.is_(True),
            VodRequest.media_type == media_type,
        )
        .group_by(VodRequest.subscription_id)
    )
    usage_rows = (await db.execute(count_stmt)).all()
    usage_map = {subscription_id: int(count or 0) for subscription_id, count in usage_rows}

    for subscription, plan in active_rows:
        limit = (
            int(plan.vod_movie_times or 0) if media_type == "MOVIE" else int(plan.vod_tv_times or 0)
        )
        used = usage_map.get(subscription.id, 0)
        if limit > used:
            return subscription
    return None


async def expire_stale_subscriptions(
    db: AsyncSession,
    subscriptions: Iterable[Subscription],
    *,
    now: datetime | None = None,
) -> int:
    current = now or datetime.utcnow()
    stale = [
        subscription
        for subscription in subscriptions
        if subscription.status == SubscriptionStatus.ACTIVE and subscription.end_at <= current
    ]
    if not stale:
        return 0

    affected_user_ids = {subscription.user_id for subscription in stale}
    plan_ids = {subscription.plan_id for subscription in stale}
    plan_map = {}
    if plan_ids:
        plans = (await db.execute(select(Plan).where(Plan.id.in_(plan_ids)))).scalars().all()
        plan_map = {plan.id: plan for plan in plans}
    for subscription in stale:
        plan = plan_map.get(subscription.plan_id)
        await create_telegram_notification(
            db,
            user_id=subscription.user_id,
            notification_type="subscription_expired",
            title="订阅已过期",
            content=f"您的{plan.name if plan else '订阅'}已过期",
            reference_id=str(subscription.id),
        )
        subscription.status = SubscriptionStatus.EXPIRED
        db.add(subscription)

    for user_id in affected_user_ids:
        await sync_user_subscription_entitlements(db, user_id, now=current)

    await db.commit()
    return len(stale)


async def expire_due_subscriptions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    current = now or datetime.utcnow()
    stale_stmt = select(Subscription).where(
        Subscription.status == SubscriptionStatus.ACTIVE,
        Subscription.end_at <= current,
    )
    stale = (await db.execute(stale_stmt)).scalars().all()
    if not stale:
        return 0, 0
    affected_user_ids = {subscription.user_id for subscription in stale}
    expired = await expire_stale_subscriptions(db, stale, now=current)
    return expired, len(affected_user_ids)


async def _get_subscription_retention_days(db: AsyncSession) -> int:
    settings = await db.scalar(select(SystemSettings))
    if settings and getattr(settings, "subscription_retention_days", None) is not None:
        return int(settings.subscription_retention_days or 0)
    return 30


async def delete_subscriptions_immediately(
    db: AsyncSession,
    subscriptions: Iterable[Subscription],
    *,
    now: datetime | None = None,
) -> int:
    current = now or datetime.utcnow()
    removable = [subscription for subscription in subscriptions if subscription is not None]
    if not removable:
        return 0

    affected_user_ids = {subscription.user_id for subscription in removable}
    for subscription in removable:
        await db.delete(subscription)

    await db.flush()

    for user_id in affected_user_ids:
        await sync_user_subscription_entitlements(
            db,
            user_id,
            now=current,
            delete_orphaned_emby_accounts=True,
        )

    return len(removable)


async def cleanup_expired_subscription_data(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    current = now or datetime.utcnow()
    retention_days = await _get_subscription_retention_days(db)
    if retention_days < 0:
        retention_days = 0

    cutoff = current - timedelta(days=retention_days)
    stale_stmt = select(Subscription).where(
        Subscription.status == SubscriptionStatus.EXPIRED,
        Subscription.end_at <= cutoff,
    )
    stale = (await db.execute(stale_stmt)).scalars().all()
    if not stale:
        return 0, 0

    affected_user_ids = {subscription.user_id for subscription in stale}
    for subscription in stale:
        await db.delete(subscription)

    await db.flush()

    for user_id in affected_user_ids:
        await sync_user_subscription_entitlements(
            db,
            user_id,
            now=current,
            delete_orphaned_emby_accounts=True,
        )

    await db.commit()
    return len(stale), len(affected_user_ids)


async def sync_user_subscription_entitlements(
    db: AsyncSession,
    user_or_id,
    *,
    now: datetime | None = None,
    delete_orphaned_emby_accounts: bool = False,
) -> Subscription | None:
    current = now or datetime.utcnow()

    if isinstance(user_or_id, User):
        user = user_or_id
    else:
        user = await db.get(User, user_or_id)
    if user is None:
        return None

    stale_stmt = select(Subscription).where(
        Subscription.user_id == user.id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        Subscription.end_at <= current,
    )
    stale = (await db.execute(stale_stmt)).scalars().all()
    for subscription in stale:
        subscription.status = SubscriptionStatus.EXPIRED
        db.add(subscription)

    active_rows = await get_effective_subscription_rows_for_user(db, user.id, now=current)
    if not active_rows:
        user.vod_movie_limit = 0
        user.vod_tv_limit = 0
        user.vod_movie_used = 0
        user.vod_tv_used = 0
        if delete_orphaned_emby_accounts:
            await reconcile_emby_accounts_for_user(db, user)
        else:
            await disable_orphaned_emby_accounts_for_user(db, user)
        db.add(user)
        return None

    subscription_ids = [subscription.id for subscription, _ in active_rows]
    usage_stmt = (
        select(
            VodRequest.subscription_id,
            VodRequest.media_type,
            func.count(VodRequest.id),
        )
        .where(
            VodRequest.subscription_id.in_(subscription_ids),
            VodRequest.quota_consumed.is_(True),
        )
        .group_by(VodRequest.subscription_id, VodRequest.media_type)
    )
    usage_rows = (await db.execute(usage_stmt)).all()
    movie_usage = 0
    tv_usage = 0
    usage_map = {
        (subscription_id, media_type): int(count or 0)
        for subscription_id, media_type, count in usage_rows
    }

    movie_limit = 0
    tv_limit = 0
    for subscription, plan in active_rows:
        movie_limit += int(plan.vod_movie_times or 0) if plan else 0
        tv_limit += int(plan.vod_tv_times or 0) if plan else 0
        movie_usage += usage_map.get((subscription.id, "MOVIE"), 0)
        tv_usage += usage_map.get((subscription.id, "TV"), 0)

    user.vod_movie_limit = movie_limit
    user.vod_tv_limit = tv_limit
    user.vod_movie_used = movie_usage
    user.vod_tv_used = tv_usage
    await disable_orphaned_emby_accounts_for_user(db, user)
    db.add(user)
    return active_rows[-1][0]
