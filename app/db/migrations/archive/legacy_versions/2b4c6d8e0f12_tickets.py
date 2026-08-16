"""add tickets

Revision ID: 2b4c6d8e0f12
Revises: 0f1a2b3c4d5e
Create Date: 2026-01-25 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2b4c6d8e0f12"
down_revision = "0f1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ticket_status = sa.Enum("OPEN", "PENDING", "RESOLVED", "CLOSED", name="ticketstatus")
    ticket_priority = sa.Enum("LOW", "MEDIUM", "HIGH", name="ticketpriority")
    ticket_status.create(op.get_bind(), checkfirst=True)
    ticket_priority.create(op.get_bind(), checkfirst=True)

    ticket_status_type = postgresql.ENUM(
        "OPEN", "PENDING", "RESOLVED", "CLOSED", name="ticketstatus", create_type=False
    )
    ticket_priority_type = postgresql.ENUM(
        "LOW", "MEDIUM", "HIGH", name="ticketpriority", create_type=False
    )

    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("status", ticket_status_type, nullable=False),
        sa.Column("priority", ticket_priority_type, nullable=False),
        sa.Column("last_reply_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tickets_user"),
    )
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"], unique=False)
    op.create_index("ix_tickets_status", "tickets", ["status"], unique=False)
    op.create_index("ix_tickets_priority", "tickets", ["priority"], unique=False)

    op.create_table(
        "ticket_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_role", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name="fk_ticket_messages_ticket"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"], name="fk_ticket_messages_user"),
    )
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"], unique=False)
    op.create_index(
        "ix_ticket_messages_sender_user_id", "ticket_messages", ["sender_user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_messages_sender_user_id", table_name="ticket_messages")
    op.drop_index("ix_ticket_messages_ticket_id", table_name="ticket_messages")
    op.drop_table("ticket_messages")

    op.drop_index("ix_tickets_priority", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_index("ix_tickets_user_id", table_name="tickets")
    op.drop_table("tickets")

    ticket_status = sa.Enum("OPEN", "PENDING", "RESOLVED", "CLOSED", name="ticketstatus")
    ticket_priority = sa.Enum("LOW", "MEDIUM", "HIGH", name="ticketpriority")
    ticket_status.drop(op.get_bind(), checkfirst=True)
    ticket_priority.drop(op.get_bind(), checkfirst=True)
