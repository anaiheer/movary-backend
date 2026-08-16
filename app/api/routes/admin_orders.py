import csv
from datetime import datetime, timedelta, timezone
import io
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.order import Order, OrderStatus, OrderValueLink, PaymentTransaction, RefundStatus
from app.models.subscription import Plan
from app.models.system_settings import SystemSettings
from app.models.user import User, UserRole
from app.schemas.admin import AdminOrderDetail, AdminOrderListItem, AdminOrdersResponse
from app.services.order_chains import (
    build_order_chain_snapshot_payload,
    load_order_chain_snapshot,
)
from app.services.order_runtime import finalize_stale_created_orders
from app.services.order_summary import build_order_summary
from app.services.payments import handle_paid_order
from app.services import refunds as refund_service


router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


ORDER_NOT_FOUND = "订单不存在"
INVALID_STATUS = "无效的状态"
INVALID_REFUND_STATUS = "无效的退款状态"
INVALID_ORDER_CHAIN_ID = "无效的订单链 ID"
REFUND_DISABLED = "退款功能未启用"
INVALID_REFUND_TO = "无效的退款方式"

REFUND_STATUS_LABELS = {
    RefundStatus.NONE.value: "未发起退款",
    RefundStatus.PENDING.value: "退款待审核",
    RefundStatus.APPROVED.value: "退款已批准",
    RefundStatus.PROCESSING.value: "退款处理中",
    RefundStatus.REJECTED.value: "退款已拒绝",
    RefundStatus.REFUNDED.value: "退款已完成",
    RefundStatus.FAILED.value: "退款处理失败",
}

ORDER_STATUS_LABELS = {
    OrderStatus.CREATED.value: "待支付",
    OrderStatus.PAID.value: "已支付",
    OrderStatus.COMPLETED.value: "已完成",
    OrderStatus.CANCELED.value: "已取消",
    OrderStatus.TIMEOUT.value: "超时",
    OrderStatus.REFUNDED.value: "已退款",
}

PAY_TYPE_LABELS = {
    "alipay": "支付宝",
    "wxpay": "微信支付",
    "balance": "余额支付",
}


class AdminOrderDeleteRequest(BaseModel):
    ids: list[UUID]


class AdminOrderStatusUpdateRequest(BaseModel):
    status: str


class AdminOrderRefundRequest(BaseModel):
    money: str | None = None
    refund_to: str | None = None  # "balance" | "original"


class AdminOrderRefundRejectRequest(BaseModel):
    reason: str


class AdminOrderExportFilters(BaseModel):
    keyword: str | None = None
    status: str | None = None
    refund_status: str | None = None
    order_chain_id: str | None = None
    start_at: str | None = None
    end_at: str | None = None


class AdminOrderExportRequest(BaseModel):
    ids: list[UUID] | None = None
    filters: AdminOrderExportFilters | None = None


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    user = (await db.execute(select(User).where(User.id == current_user["user_id"]))).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _delete_orders_by_ids(order_ids: list[UUID], db: AsyncSession) -> dict:
    requested_ids: list[UUID] = []
    seen_ids: set[UUID] = set()
    for order_id in order_ids:
        if order_id in seen_ids:
            continue
        seen_ids.add(order_id)
        requested_ids.append(order_id)

    if not requested_ids:
        return {
            "requested": 0,
            "deleted": 0,
            "missing": 0,
            "missing_ids": [],
            "failed_ids": [],
        }

    orders = (
        (
            await db.execute(
                select(Order).where(Order.id.in_(requested_ids)).order_by(Order.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    order_map = {order.id: order for order in orders}
    missing_ids = [str(order_id) for order_id in requested_ids if order_id not in order_map]
    failed_ids: list[str] = []
    deleted = 0

    for order_id in requested_ids:
        order = order_map.get(order_id)
        if not order:
            continue
        try:
            async with db.begin_nested():
                await db.execute(
                    delete(OrderValueLink).where(
                        or_(
                            OrderValueLink.source_order_id == order_id,
                            OrderValueLink.target_order_id == order_id,
                        )
                    )
                )
                await db.execute(
                    update(Order)
                    .where(Order.parent_order_id == order_id, Order.id != order_id)
                    .values(parent_order_id=None)
                )
                await db.execute(
                    update(Order)
                    .where(Order.superseded_by_order_id == order_id, Order.id != order_id)
                    .values(superseded_by_order_id=None)
                )
                await db.execute(
                    update(Order)
                    .where(Order.root_order_id == order_id, Order.id != order_id)
                    .values(root_order_id=Order.id)
                )
                await db.execute(
                    update(Order)
                    .where(Order.order_chain_id == order_id, Order.id != order_id)
                    .values(order_chain_id=Order.id)
                )
                await db.execute(
                    delete(PaymentTransaction).where(PaymentTransaction.order_id == order_id)
                )
                await db.flush()
                await db.delete(order)
                await db.flush()
            deleted += 1
        except Exception:
            failed_ids.append(str(order_id))

    await db.commit()
    return {
        "requested": len(requested_ids),
        "deleted": deleted,
        "missing": len(missing_ids),
        "missing_ids": missing_ids,
        "failed_ids": failed_ids,
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_order_chain_id(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_ORDER_CHAIN_ID,
        ) from exc


async def _get_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _refund_status_label(refund_status: str | None) -> str:
    if not refund_status:
        return REFUND_STATUS_LABELS[RefundStatus.NONE.value]
    return REFUND_STATUS_LABELS.get(str(refund_status), "退款状态未知")


def _order_status_label(order_status: str | None) -> str:
    if not order_status:
        return ""
    return ORDER_STATUS_LABELS.get(str(order_status), str(order_status))


def _pay_method_label(order: Order) -> str:
    payload = order.pay_payload or {}
    pay_type = str(payload.get("pay_type") or "").lower()
    if pay_type in PAY_TYPE_LABELS:
        return PAY_TYPE_LABELS[pay_type]
    if order.pay_provider == "BALANCE":
        return "余额支付"
    if order.pay_provider == "EPAY":
        return "易支付"
    return order.pay_provider or "-"


def _build_filters(
    *,
    keyword: str | None,
    status_filter: str | None,
    refund_status_filter: str | None,
    order_chain_id: str | None,
    plan_id: str | None,
    start_at: str | None,
    end_at: str | None,
) -> list:
    filters = []

    if keyword:
        like = f"%{keyword}%"
        keyword_filters = [
            Order.order_no.ilike(like),
            User.username.ilike(like),
            User.email.ilike(like),
        ]
        try:
            keyword_uuid = UUID(keyword)
            keyword_filters.append(Order.user_id == keyword_uuid)
        except ValueError:
            pass
        filters.append(or_(*keyword_filters))

    if status_filter:
        try:
            target_status = OrderStatus(status_filter)
            if target_status == OrderStatus.REFUNDED:
                filters.append(
                    or_(
                        Order.status == OrderStatus.REFUNDED,
                        Order.refund_status == RefundStatus.REFUNDED,
                    )
                )
            elif target_status in {OrderStatus.PAID, OrderStatus.COMPLETED}:
                filters.append(
                    and_(
                        Order.status == target_status,
                        ~Order.refund_status.in_(
                            [
                                RefundStatus.PENDING,
                                RefundStatus.APPROVED,
                                RefundStatus.PROCESSING,
                                RefundStatus.REJECTED,
                                RefundStatus.FAILED,
                                RefundStatus.REFUNDED,
                            ]
                        ),
                    )
                )
            else:
                filters.append(Order.status == target_status)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_STATUS
            ) from exc

    if refund_status_filter:
        try:
            filters.append(Order.refund_status == RefundStatus(refund_status_filter))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_REFUND_STATUS
            ) from exc

    parsed_order_chain_id = _parse_order_chain_id(order_chain_id)
    if parsed_order_chain_id:
        filters.append(Order.order_chain_id == parsed_order_chain_id)

    if plan_id:
        filters.append(Order.plan_id == plan_id)

    start_dt = _parse_dt(start_at)
    end_dt = _parse_dt(end_at)
    if start_dt:
        filters.append(Order.created_at >= start_dt)
    if end_dt:
        filters.append(Order.created_at <= end_dt)

    return filters


def _serialize_order_list_item(order: Order, user: User, plan: Plan | None) -> AdminOrderListItem:
    refund_status = order.refund_status.value if order.refund_status else None
    return AdminOrderListItem(
        id=order.id,
        order_chain_id=order.order_chain_id,
        root_order_id=order.root_order_id,
        parent_order_id=order.parent_order_id,
        superseded_by_order_id=order.superseded_by_order_id,
        order_no=order.order_no,
        user={"id": user.id, "username": user.username, "email": user.email},
        plan={"id": plan.id, "name": plan.name, "duration_days": plan.duration_days}
        if plan
        else None,
        amount=f"{float(order.amount or 0):.2f}",
        status=order.status.value,
        type=order.type.value,
        settlement_status=order.settlement_status.value if order.settlement_status else None,
        refunded_at=order.refunded_at,
        refund_status=refund_status,
        refund_status_label=_refund_status_label(refund_status),
        refund_requested_at=order.refund_requested_at,
        refund_reviewed_at=order.refund_reviewed_at,
        refund_reviewed_by=order.refund_reviewed_by,
        refund_reject_reason=order.refund_reject_reason,
        created_at=order.created_at,
        paid_at=order.paid_at,
        **build_order_summary(order),
    )


async def _build_order_detail(order_id: UUID, db: AsyncSession) -> AdminOrderDetail:
    row = (
        await db.execute(
            select(Order, User, Plan)
            .join(User, User.id == Order.user_id)
            .outerjoin(Plan, Plan.id == Order.plan_id)
            .where(Order.id == order_id)
        )
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)
    order, user, plan = row
    chain_orders, value_links = await load_order_chain_snapshot(db, order)
    return AdminOrderDetail(
        **_serialize_order_list_item(order, user, plan).model_dump(mode="python"),
        currency=order.currency,
        pay_provider=order.pay_provider,
        pay_payload=order.pay_payload,
        order_chain=build_order_chain_snapshot_payload(order, chain_orders, value_links),
    )


@router.get("", response_model=dict)
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    keyword: str | None = Query(None, description="order_no/user_id/username/email"),
    status_filter: str | None = Query(None, alias="status"),
    refund_status_filter: str | None = Query(None, alias="refund_status"),
    order_chain_id: str | None = Query(None, alias="order_chain_id"),
    plan_id: str | None = Query(None),
    start_at: str | None = Query(None),
    end_at: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row)

    filters = _build_filters(
        keyword=keyword,
        status_filter=status_filter,
        refund_status_filter=refund_status_filter,
        order_chain_id=order_chain_id,
        plan_id=plan_id,
        start_at=start_at,
        end_at=end_at,
    )

    base_stmt = (
        select(Order, User, Plan)
        .join(User, User.id == Order.user_id)
        .outerjoin(Plan, Plan.id == Order.plan_id)
    )
    if filters:
        base_stmt = base_stmt.where(*filters)
    base_stmt = base_stmt.order_by(Order.created_at.desc())

    count_base = select(Order.id).join(User, User.id == Order.user_id)
    if filters:
        count_base = count_base.where(*filters)
    total = await db.scalar(select(func.count()).select_from(count_base.subquery()))

    rows = (await db.execute(base_stmt.offset((page - 1) * page_size).limit(page_size))).all()
    items: list[AdminOrderListItem] = []
    for order, user, plan in rows:
        items.append(_serialize_order_list_item(order, user, plan))

    stats_stmt = select(
        func.count(Order.id).label("total_orders"),
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED]),
                            Order.refund_status != RefundStatus.REFUNDED,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("successful_orders"),
        func.coalesce(func.sum(case((Order.status == OrderStatus.CREATED, 1), else_=0)), 0).label(
            "pending_orders"
        ),
        func.coalesce(
            func.sum(
                case(
                    (
                        (Order.refund_status.is_not(None))
                        & (Order.refund_status != RefundStatus.NONE),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("refund_orders"),
    ).join(User, User.id == Order.user_id)
    stats_row = (await db.execute(stats_stmt)).one()

    payload = AdminOrdersResponse(
        items=items,
        pagination={"page": page, "page_size": page_size, "total": int(total or 0)},
        stats={
            "total_orders": int(stats_row.total_orders or 0),
            "successful_orders": int(stats_row.successful_orders or 0),
            "pending_orders": int(stats_row.pending_orders or 0),
            "refund_orders": int(stats_row.refund_orders or 0),
        },
    )
    return _response(payload.model_dump(mode="json"))


@router.post("/export")
async def export_orders(
    payload: AdminOrderExportRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row)

    stmt = (
        select(Order, User, Plan)
        .join(User, User.id == Order.user_id)
        .outerjoin(Plan, Plan.id == Order.plan_id)
    )
    requested_ids = list(dict.fromkeys(payload.ids or []))
    if requested_ids:
        stmt = stmt.where(Order.id.in_(requested_ids))
    else:
        filters = _build_filters(
            keyword=payload.filters.keyword if payload.filters else None,
            status_filter=payload.filters.status if payload.filters else None,
            refund_status_filter=payload.filters.refund_status if payload.filters else None,
            order_chain_id=payload.filters.order_chain_id if payload.filters else None,
            plan_id=None,
            start_at=payload.filters.start_at if payload.filters else None,
            end_at=payload.filters.end_at if payload.filters else None,
        )
        if filters:
            stmt = stmt.where(*filters)

    rows = (await db.execute(stmt.order_by(Order.created_at.desc()))).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "订单号",
            "用户",
            "套餐",
            "订单类型",
            "订单状态",
            "退款状态",
            "金额",
            "支付方式",
            "创建时间",
            "支付时间",
        ]
    )

    for order, user, plan in rows:
        item = _serialize_order_list_item(order, user, plan)
        user_label = user.username
        if user.email:
            user_label = f"{user.username} ({user.email})"
        writer.writerow(
            [
                item.order_no,
                user_label,
                plan.name if plan else "",
                item.purchase_action_label or item.type,
                _order_status_label(item.status),
                item.refund_status_label or _refund_status_label(item.refund_status),
                item.amount,
                _pay_method_label(order),
                order.created_at.strftime("%Y-%m-%d %H:%M:%S") if order.created_at else "",
                order.paid_at.strftime("%Y-%m-%d %H:%M:%S") if order.paid_at else "",
            ]
        )

    filename = f"orders_export_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{order_id}", response_model=dict)
async def get_order_detail(
    order_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    settings_row = await _get_settings(db)
    await finalize_stale_created_orders(db, settings_row, order_id=order_id)
    payload = await _build_order_detail(order_id, db)
    return _response(payload.model_dump(mode="json"))


@router.patch("/{order_id}/status", response_model=dict)
async def update_order_status(
    order_id: UUID,
    payload: AdminOrderStatusUpdateRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)

    try:
        target_status = OrderStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_STATUS) from exc

    if target_status == OrderStatus.REFUNDED:
        settings_row = await _get_settings(db)
        if not settings_row.refund_enabled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=REFUND_DISABLED)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请使用退款接口将订单标记为已退款",
        )

    if target_status in {OrderStatus.PAID, OrderStatus.COMPLETED}:
        if order.status not in {OrderStatus.PAID, OrderStatus.COMPLETED}:
            if not order.pay_provider:
                order.pay_provider = "ADMIN"
            await handle_paid_order(
                order, "ADMIN", {"source": "admin", "status": target_status.value}, db
            )
    else:
        order.status = target_status
        order.paid_at = None
        db.add(order)
        await db.commit()
        await db.refresh(order)

    detail = await _build_order_detail(order_id, db)
    return _response(detail.model_dump(mode="json"), "status updated")


@router.post("/{order_id}/refund", response_model=dict)
async def refund_order(
    order_id: UUID,
    payload: AdminOrderRefundRequest = AdminOrderRefundRequest(),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    settings_row = await _get_settings(db)
    if not settings_row.refund_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=REFUND_DISABLED)

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)

    admin_user = await _ensure_admin(current_user, db)
    if order.status == OrderStatus.REFUNDED and order.refund_status == RefundStatus.REFUNDED:
        detail = await _build_order_detail(order_id, db)
        return _response(detail.model_dump(mode="json"), "already refunded")

    await refund_service.ensure_order_refundable(order, db)
    refund_state = refund_service.get_refund_state(order)
    refund_money = refund_service.parse_refund_money(payload.money, order)
    default_refund_to = "original" if order.pay_provider == "EPAY" else "balance"
    refund_to = (payload.refund_to or refund_state.get("refund_to") or default_refund_to).lower()
    if refund_to not in {"original", "balance"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_REFUND_TO)
    if refund_to == "original" and order.pay_provider != "EPAY":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=refund_service.EPAY_REFUND_NOT_SUPPORTED,
        )

    await refund_service.validate_refund_policies(order, db, settings_row)
    if order.refund_status != RefundStatus.PENDING:
        refund_service.ensure_refund_request_allowed(order)
        refund_service.request_refund(order, refund_to, refund_money)

    if refund_to == "balance":
        await refund_service.refund_to_balance(
            order,
            db,
            operator_user_id=admin_user.id,
            reviewer_user_id=admin_user.id,
            money=refund_money,
        )
    else:
        await refund_service.approve_original_refund(
            order,
            settings_row,
            reviewer_user_id=admin_user.id,
            money=refund_money,
        )

    db.add(order)
    await db.commit()
    detail = await _build_order_detail(order_id, db)
    return _response(detail.model_dump(mode="json"), "refund approved")


@router.post("/{order_id}/refund/reject", response_model=dict)
async def reject_refund(
    order_id: UUID,
    payload: AdminOrderRefundRejectRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    admin_user = await _ensure_admin(current_user, db)

    settings_row = await _get_settings(db)
    if not settings_row.refund_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=REFUND_DISABLED)

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)
    if order.refund_status != RefundStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=refund_service.REFUND_REQUEST_NOT_FOUND,
        )

    refund_service.reject_refund(order, admin_user.id, payload.reason)
    db.add(order)
    await db.commit()
    detail = await _build_order_detail(order_id, db)
    return _response(detail.model_dump(mode="json"), "refund rejected")


@router.post("/{order_id}/refund/query", response_model=dict)
async def refund_query(
    order_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORDER_NOT_FOUND)

    settings_row = await _get_settings(db)
    if not settings_row.refund_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=REFUND_DISABLED)

    if order.refund_status != RefundStatus.PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=refund_service.REFUND_REQUEST_NOT_FOUND,
        )

    await refund_service.query_original_refund(order, settings_row, db)
    db.add(order)
    await db.commit()
    detail = await _build_order_detail(order_id, db)
    return _response(detail.model_dump(mode="json"), "refund queried")


@router.post("/cleanup", response_model=dict)
async def cleanup_orders(
    days: int = Query(0, ge=0, le=365),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = select(Order).where(Order.status.in_([OrderStatus.TIMEOUT, OrderStatus.CANCELED]))
    if days > 0:
        cutoff = datetime.utcnow() - timedelta(days=days)
        stmt = stmt.where(Order.created_at < cutoff)
    order_ids = [(order.id) for order in (await db.execute(stmt)).scalars().all()]
    result = await _delete_orders_by_ids(order_ids, db)
    result["days"] = days
    return _response(result)


@router.post("/batch-delete", response_model=dict)
async def batch_delete_orders(
    payload: AdminOrderDeleteRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    result = await _delete_orders_by_ids(payload.ids, db)
    return _response(result)
