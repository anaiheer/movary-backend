from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.license_status import build_license_overview
from app.core.pro_extensions import get_backend_extension_state
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.order import Order, OrderStatus
from app.models.subscription import Subscription, SubscriptionStatus, Plan
from app.models.user import User, UserRole
from app.models.vod import VodRequest
from app.schemas.admin import DashboardOverview
from app.services.order_summary import build_order_summary

router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


def _order_realized_amount(order: Order) -> float:
    if order.status not in {OrderStatus.PAID, OrderStatus.COMPLETED, OrderStatus.REFUNDED}:
        return 0.0

    summary = build_order_summary(order)
    payable_amount = float(summary.get("payable_amount") or 0)
    if str(getattr(order.refund_status, "value", order.refund_status) or "") == "REFUNDED":
        return 0.0
    return payable_amount


def _active_subscription_exists_for_user(now: datetime):
    return (
        select(Subscription.id)
        .where(
            Subscription.user_id == User.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > now,
        )
        .exists()
    )


def _add_months(dt: datetime, months: int) -> datetime:
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def _period_start(dt: datetime, granularity: str) -> datetime:
    if granularity == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if granularity == "quarter":
        month = ((dt.month - 1) // 3) * 3 + 1
        return dt.replace(month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if granularity == "half_year":
        month = 1 if dt.month <= 6 else 7
        return dt.replace(month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if granularity == "year":
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不支持的统计粒度")


def _period_label(dt: datetime, granularity: str) -> str:
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "month":
        return dt.strftime("%Y-%m")
    if granularity == "quarter":
        quarter = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{quarter}"
    if granularity == "half_year":
        half = "H1" if dt.month <= 6 else "H2"
        return f"{dt.year}-{half}"
    if granularity == "year":
        return dt.strftime("%Y")
    return "-"


def _build_periods(now: datetime, granularity: str, count: int) -> list[datetime]:
    cursor = _period_start(now, granularity)
    periods = []
    for _ in range(count):
        periods.append(cursor)
        if granularity == "day":
            cursor = cursor - timedelta(days=1)
        elif granularity == "month":
            cursor = _add_months(cursor, -1)
        elif granularity == "quarter":
            cursor = _add_months(cursor, -3)
        elif granularity == "half_year":
            cursor = _add_months(cursor, -6)
        elif granularity == "year":
            cursor = _add_months(cursor, -12)
    periods.reverse()
    return periods


@router.get("/overview")
async def dashboard_overview(
    granularity: str = Query("month"),
    months: int = Query(6, ge=1, le=24),
    periods: int | None = Query(None, ge=1, le=60),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    now = datetime.utcnow()
    period_count = periods or months
    period_list = _build_periods(now, granularity, period_count)
    start = period_list[0]

    total_users = await db.scalar(
        select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    )
    active_users = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.deleted_at.is_(None), _active_subscription_exists_for_user(now))
    )
    revenue_orders = (
        (
            await db.execute(
                select(Order).where(
                    Order.status.in_(
                        [OrderStatus.PAID, OrderStatus.COMPLETED, OrderStatus.REFUNDED]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    total_revenue = sum(_order_realized_amount(order) for order in revenue_orders)
    active_subscriptions = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.end_at > now)
    )
    total_vod_requests = await db.scalar(select(func.count()).select_from(VodRequest))
    expiring_days = 7
    expiring_soon = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at >= now,
            Subscription.end_at <= now + timedelta(days=expiring_days),
        )
    )

    period_labels = [_period_label(p, granularity) for p in period_list]
    growth_map = {label: 0 for label in period_labels}
    revenue_map = {label: 0 for label in period_labels}

    user_stmt = select(User.created_at).where(User.created_at >= start, User.deleted_at.is_(None))
    user_rows = (await db.execute(user_stmt)).all()
    for (created_at,) in user_rows:
        label = _period_label(_period_start(created_at, granularity), granularity)
        if label in growth_map:
            growth_map[label] += 1

    revenue_stmt = select(Order).where(
        Order.created_at >= start,
        Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED, OrderStatus.REFUNDED]),
    )
    revenue_rows = (await db.execute(revenue_stmt)).scalars().all()
    for order in revenue_rows:
        created_at = order.created_at
        label = _period_label(_period_start(created_at, granularity), granularity)
        if label in revenue_map:
            revenue_map[label] += _order_realized_amount(order)

    user_growth = [{"period": p, "value": int(growth_map.get(p, 0))} for p in period_labels]
    revenue_trend = [
        {"period": p, "value": f"{float(revenue_map.get(p, 0)):.2f}"} for p in period_labels
    ]

    recent_users_stmt = (
        select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc()).limit(5)
    )
    recent_users = (await db.execute(recent_users_stmt)).scalars().all()
    inviter_map: dict = {}
    inviter_ids = [user.inviter_user_id for user in recent_users if user.inviter_user_id]
    if inviter_ids:
        inviters_stmt = select(User).where(User.id.in_(inviter_ids))
        inviters = (await db.execute(inviters_stmt)).scalars().all()
        inviter_map = {inviter.id: inviter for inviter in inviters}

    recent_orders_stmt = (
        select(Order, User, Plan)
        .join(User, User.id == Order.user_id)
        .outerjoin(Plan, Plan.id == Order.plan_id)
        .order_by(Order.created_at.desc())
        .limit(5)
    )
    recent_orders_rows = (await db.execute(recent_orders_stmt)).all()

    from app.services.license_runtime import load_license_state

    license_info = build_license_overview(get_backend_extension_state(), load_license_state())

    overview = DashboardOverview(
        license=license_info,
        kpi={
            "total_users": int(total_users or 0),
            "active_users": int(active_users or 0),
            "total_revenue": f"{float(total_revenue or 0):.2f}",
            "active_subscriptions": int(active_subscriptions or 0),
            "total_vod_requests": int(total_vod_requests or 0),
            "expiring_soon": int(expiring_soon or 0),
            "expiring_days": expiring_days,
        },
        charts={"user_growth": user_growth, "revenue_trend": revenue_trend},
        recent_users=[
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "inviter": (
                    {
                        "id": str(inviter.id),
                        "username": inviter.username,
                    }
                    if (inviter := inviter_map.get(user.inviter_user_id))
                    else None
                ),
                "created_at": user.created_at,
            }
            for user in recent_users
        ],
        recent_orders=[
            {
                "id": order.id,
                "user": {"id": user.id, "username": user.username},
                "plan": {"id": plan.id, "name": plan.name} if plan else None,
                "amount": f"{float(order.amount or 0):.2f}",
                "status": order.status.value,
                "created_at": order.created_at,
            }
            for order, user, plan in recent_orders_rows
        ],
    )

    return _response(overview.model_dump(mode="json"))
