from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.ticket import Ticket, TicketMessage, TicketStatus
from app.models.user import User, UserRole
from app.schemas.ticket import (
    AdminTicketStats,
    TicketDetailOut,
    TicketMessageCreate,
    TicketMessageOut,
    TicketOut,
    TicketUserSummary,
)
from app.services.telegram import create_telegram_notification

router = APIRouter(prefix="/admin/tickets", tags=["admin-tickets"])


class AdminTicketDeleteRequest(BaseModel):
    ids: list[UUID]


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def _serialize_ticket(ticket: Ticket, user: User | None = None) -> TicketOut:
    payload = TicketOut.model_validate(ticket)
    if not user:
        return payload
    return payload.model_copy(update={"user": TicketUserSummary.model_validate(user)})


async def _delete_tickets_by_ids(ticket_ids: list[UUID], db: AsyncSession) -> dict:
    requested_ids: list[UUID] = []
    seen_ids: set[UUID] = set()
    for ticket_id in ticket_ids:
        if ticket_id in seen_ids:
            continue
        seen_ids.add(ticket_id)
        requested_ids.append(ticket_id)

    if not requested_ids:
        return {
            "requested": 0,
            "deleted": 0,
            "missing": 0,
            "missing_ids": [],
            "failed_ids": [],
        }

    tickets = (
        (
            await db.execute(
                select(Ticket)
                .where(Ticket.id.in_(requested_ids))
                .order_by(Ticket.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    ticket_map = {ticket.id: ticket for ticket in tickets}
    missing_ids = [str(ticket_id) for ticket_id in requested_ids if ticket_id not in ticket_map]
    failed_ids: list[str] = []
    deleted_count = 0

    for ticket_id in requested_ids:
        ticket = ticket_map.get(ticket_id)
        if not ticket:
            continue
        try:
            async with db.begin_nested():
                await db.execute(delete(TicketMessage).where(TicketMessage.ticket_id == ticket_id))
                await db.flush()
                await db.delete(ticket)
                await db.flush()
            deleted_count += 1
        except Exception:
            failed_ids.append(str(ticket_id))

    await db.commit()
    return {
        "requested": len(requested_ids),
        "deleted": deleted_count,
        "missing": len(missing_ids),
        "missing_ids": missing_ids,
        "failed_ids": failed_ids,
    }


@router.get("", response_model=dict)
async def list_tickets(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    keyword: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    filters = []
    if keyword:
        like = f"%{keyword}%"
        filters.append(
            or_(
                Ticket.subject.ilike(like),
                User.username.ilike(like),
                User.email.ilike(like),
            )
        )
    if status_filter:
        try:
            filters.append(Ticket.status == TicketStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的状态")

    stmt = (
        select(Ticket, User)
        .join(User, User.id == Ticket.user_id)
        .order_by(Ticket.updated_at.desc())
    )
    if filters:
        stmt = stmt.where(*filters)

    count_stmt = select(func.count()).select_from(Ticket).join(User, User.id == Ticket.user_id)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = await db.scalar(count_stmt)

    stats_stmt = select(
        func.count().filter(Ticket.status == TicketStatus.OPEN).label("open_count"),
        func.count().filter(Ticket.status == TicketStatus.PENDING).label("pending_count"),
        func.count().filter(Ticket.status == TicketStatus.RESOLVED).label("resolved_count"),
        func.count().filter(Ticket.status == TicketStatus.CLOSED).label("closed_count"),
    ).select_from(Ticket)
    stats_row = (await db.execute(stats_stmt)).one()

    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    stats = AdminTicketStats(
        open_count=int(stats_row.open_count or 0),
        pending_count=int(stats_row.pending_count or 0),
        resolved_count=int(stats_row.resolved_count or 0),
        closed_count=int(stats_row.closed_count or 0),
    )

    return _response(
        {
            "items": [_serialize_ticket(ticket, user) for ticket, user in rows],
            "page": page,
            "page_size": page_size,
            "total": int(total or 0),
            "stats": stats.model_dump(),
        }
    )


@router.get("/{ticket_id}", response_model=dict)
async def get_ticket_detail(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    row = (
        await db.execute(
            select(Ticket, User).join(User, User.id == Ticket.user_id).where(Ticket.id == ticket_id)
        )
    ).first()
    ticket = row[0] if row else None
    ticket_user = row[1] if row else None
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")

    msg_stmt = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at.asc())
    )
    messages = (await db.execute(msg_stmt)).scalars().all()

    payload = TicketDetailOut(
        ticket=_serialize_ticket(ticket, ticket_user),
        messages=[TicketMessageOut.model_validate(m) for m in messages],
    )
    return _response(payload.model_dump(mode="json"))


@router.post("/{ticket_id}/messages", response_model=dict)
async def reply_ticket(
    ticket_id: UUID,
    payload: TicketMessageCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")

    msg = TicketMessage(
        ticket_id=ticket.id,
        sender_user_id=current_user["user_id"],
        sender_role="ADMIN",
        content=payload.content,
    )
    ticket.status = TicketStatus.PENDING
    ticket.last_reply_at = datetime.utcnow()
    db.add(ticket)
    db.add(msg)
    await create_telegram_notification(
        db,
        user_id=ticket.user_id,
        notification_type="ticket_reply",
        title="工单收到回复",
        content=f"工单#{ticket.id}有新回复",
        reference_id=str(ticket.id),
    )
    await db.commit()

    return _response({"id": str(ticket.id), "status": ticket.status.value})


@router.post("/{ticket_id}/status", response_model=dict)
async def update_ticket_status(
    ticket_id: UUID,
    status_value: str = Query(..., alias="status"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")

    try:
        ticket.status = TicketStatus(status_value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的状态")

    ticket.updated_at = datetime.utcnow()
    db.add(ticket)
    await db.commit()
    return _response({"id": str(ticket.id), "status": ticket.status.value})


@router.post("/batch-delete", response_model=dict)
async def batch_delete_tickets(
    payload: AdminTicketDeleteRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    result = await _delete_tickets_by_ids(payload.ids, db)
    return _response(result)
