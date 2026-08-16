from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.ticket import Ticket, TicketMessage, TicketPriority, TicketStatus
from app.models.user import User


async def _create_ticket_with_message() -> tuple[UUID, UUID, str, str]:
    async with AsyncSessionLocal() as session:
        username = f"ticket_user_{uuid4().hex[:8]}"
        email = f"ticket_user_{uuid4().hex[:8]}@example.com"
        user = User(
            username=username,
            email=email,
            password_hash=hash_password("Test123456"),
        )
        session.add(user)
        await session.flush()

        ticket = Ticket(
            user_id=user.id,
            subject=f"Ticket {uuid4().hex[:8]}",
            status=TicketStatus.OPEN,
            priority=TicketPriority.MEDIUM,
        )
        session.add(ticket)
        await session.flush()

        message = TicketMessage(
            ticket_id=ticket.id,
            sender_user_id=user.id,
            sender_role="USER",
            content="Initial ticket message",
        )
        session.add(message)
        await session.commit()

        return ticket.id, message.id, username, email


@pytest.mark.asyncio
async def test_admin_batch_delete_tickets_removes_messages(async_client, admin_token):
    ticket_id, message_id, _, _ = await _create_ticket_with_message()

    response = await async_client.post(
        "/api/v1/admin/tickets/batch-delete",
        json={"ids": [str(ticket_id)]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload == {
        "requested": 1,
        "deleted": 1,
        "missing": 0,
        "missing_ids": [],
        "failed_ids": [],
    }

    async with AsyncSessionLocal() as session:
        saved_ticket = await session.get(Ticket, ticket_id)
        saved_message = await session.get(TicketMessage, message_id)
        assert saved_ticket is None
        assert saved_message is None


@pytest.mark.asyncio
async def test_admin_batch_delete_tickets_reports_missing_ids(async_client, admin_token):
    ticket_id, _, _, _ = await _create_ticket_with_message()
    missing_id = str(uuid4())

    response = await async_client.post(
        "/api/v1/admin/tickets/batch-delete",
        json={"ids": [str(ticket_id), missing_id, str(ticket_id)]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["requested"] == 2
    assert payload["deleted"] == 1
    assert payload["missing"] == 1
    assert payload["missing_ids"] == [missing_id]
    assert payload["failed_ids"] == []

    async with AsyncSessionLocal() as session:
        remaining_tickets = (
            (await session.execute(select(Ticket.id).where(Ticket.id == ticket_id))).scalars().all()
        )
        remaining_messages = (
            (
                await session.execute(
                    select(TicketMessage.id).where(TicketMessage.ticket_id == ticket_id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining_tickets == []
        assert remaining_messages == []


@pytest.mark.asyncio
async def test_admin_ticket_list_includes_ticket_owner(async_client, admin_token):
    ticket_id, _, username, email = await _create_ticket_with_message()

    response = await async_client.get(
        "/api/v1/admin/tickets",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    ticket = next(item for item in items if item["id"] == str(ticket_id))
    assert ticket["user"] == {
        "id": ticket["user_id"],
        "username": username,
        "email": email,
    }
