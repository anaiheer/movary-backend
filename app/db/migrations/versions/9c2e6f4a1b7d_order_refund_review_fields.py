"""orders refund review fields

Revision ID: 9c2e6f4a1b7d
Revises: e7f8a9b0c1d2
Create Date: 2026-03-23 10:40:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "9c2e6f4a1b7d"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


refund_status_enum = sa.Enum(
    "NONE",
    "PENDING",
    "APPROVED",
    "REJECTED",
    "PROCESSING",
    "REFUNDED",
    "FAILED",
    name="refundstatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    refund_status_enum.create(bind, checkfirst=True)
    op.add_column(
        "orders",
        sa.Column(
            "refund_status",
            refund_status_enum,
            nullable=False,
            server_default="NONE",
        ),
    )
    op.add_column("orders", sa.Column("refund_requested_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("refund_reviewed_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("refund_reviewed_by", sa.UUID(), nullable=True))
    op.add_column(
        "orders", sa.Column("refund_reject_reason", sa.String(length=1000), nullable=True)
    )
    op.create_index(op.f("ix_orders_refund_status"), "orders", ["refund_status"], unique=False)
    op.create_index(
        op.f("ix_orders_refund_reviewed_by"),
        "orders",
        ["refund_reviewed_by"],
        unique=False,
    )
    op.alter_column("orders", "refund_status", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_orders_refund_reviewed_by"), table_name="orders")
    op.drop_index(op.f("ix_orders_refund_status"), table_name="orders")
    op.drop_column("orders", "refund_reject_reason")
    op.drop_column("orders", "refund_reviewed_by")
    op.drop_column("orders", "refund_reviewed_at")
    op.drop_column("orders", "refund_requested_at")
    op.drop_column("orders", "refund_status")
    refund_status_enum.drop(op.get_bind(), checkfirst=True)
