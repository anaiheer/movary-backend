from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.ticket import Ticket, TicketMessage, TicketPriority, TicketStatus
from app.models.user import User
from app.schemas.ticket import (
    TicketCreate,
    TicketMessageCreate,
    TicketDetailOut,
    TicketMessageOut,
    TicketOut,
    TicketListResponse,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _normalize_priority(value: str) -> TicketPriority:
    try:
        return TicketPriority(value)
    except ValueError:
        return TicketPriority.MEDIUM


@router.get("", response_model=TicketListResponse)
async def list_my_tickets(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Ticket)
        .where(Ticket.user_id == current_user["user_id"])
        .order_by(Ticket.updated_at.desc())
    )
    result = await db.execute(stmt)
    items = result.scalars().all()
    return TicketListResponse(items=[TicketOut.model_validate(item) for item in items])


@router.post("", response_model=TicketDetailOut, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(user_stmt)).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    priority = _normalize_priority(payload.priority)
    ticket = Ticket(
        user_id=user.id,
        subject=payload.subject,
        priority=priority,
        status=TicketStatus.OPEN,
        last_reply_at=datetime.utcnow(),
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    message = TicketMessage(
        ticket_id=ticket.id,
        sender_user_id=user.id,
        sender_role="USER",
        content=payload.content,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    return TicketDetailOut(
        ticket=TicketOut.model_validate(ticket),
        messages=[TicketMessageOut.model_validate(message)],
    )


@router.get("/{ticket_id}", response_model=TicketDetailOut)
async def get_ticket_detail(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == current_user["user_id"])
    ticket = (await db.execute(stmt)).scalar()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")

    msg_stmt = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at.asc())
    )
    messages = (await db.execute(msg_stmt)).scalars().all()

    return TicketDetailOut(
        ticket=TicketOut.model_validate(ticket),
        messages=[TicketMessageOut.model_validate(m) for m in messages],
    )


@router.post("/{ticket_id}/messages", response_model=TicketDetailOut)
async def reply_ticket(
    ticket_id: UUID,
    payload: TicketMessageCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == current_user["user_id"])
    ticket = (await db.execute(stmt)).scalar()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    if ticket.status in {TicketStatus.RESOLVED, TicketStatus.CLOSED}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="工单已关闭")

    msg = TicketMessage(
        ticket_id=ticket.id,
        sender_user_id=current_user["user_id"],
        sender_role="USER",
        content=payload.content,
    )
    ticket.status = TicketStatus.OPEN
    ticket.last_reply_at = datetime.utcnow()
    db.add(ticket)
    db.add(msg)
    await db.commit()

    msg_stmt = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at.asc())
    )
    messages = (await db.execute(msg_stmt)).scalars().all()
    return TicketDetailOut(
        ticket=TicketOut.model_validate(ticket),
        messages=[TicketMessageOut.model_validate(m) for m in messages],
    )


@router.post("/{ticket_id}/close", response_model=TicketOut)
async def close_ticket(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == current_user["user_id"])
    ticket = (await db.execute(stmt)).scalar()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")

    ticket.status = TicketStatus.CLOSED
    ticket.updated_at = datetime.utcnow()
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return TicketOut.model_validate(ticket)
