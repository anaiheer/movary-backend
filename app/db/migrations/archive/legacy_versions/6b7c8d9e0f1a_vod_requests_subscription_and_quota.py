"""vod_requests: subscription_id + quota_consumed

Revision ID: 6b7c8d9e0f1a
Revises: 5a6b7c8d9e0f
Create Date: 2026-02-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "6b7c8d9e0f1a"
down_revision = "5a6b7c8d9e0f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vod_requests", sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "vod_requests",
        sa.Column("quota_consumed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        op.f("ix_vod_requests_subscription_id"), "vod_requests", ["subscription_id"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_vod_requests_subscription_id"), table_name="vod_requests")
    op.drop_column("vod_requests", "quota_consumed")
    op.drop_column("vod_requests", "subscription_id")
