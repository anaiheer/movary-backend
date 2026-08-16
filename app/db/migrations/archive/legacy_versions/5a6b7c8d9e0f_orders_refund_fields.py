"""orders: refunded_at + subscription_id

Revision ID: 5a6b7c8d9e0f
Revises: 4f5a6b7c8d9e
Create Date: 2026-02-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "5a6b7c8d9e0f"
down_revision = "4f5a6b7c8d9e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("refunded_at", sa.DateTime(), nullable=True))
    op.add_column(
        "orders", sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_index(op.f("ix_orders_subscription_id"), "orders", ["subscription_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_orders_subscription_id"), table_name="orders")
    op.drop_column("orders", "subscription_id")
    op.drop_column("orders", "refunded_at")
