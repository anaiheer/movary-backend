"""system_settings refund limits: per-month limit

Revision ID: 4f5a6b7c8d9e
Revises: 3e4f5a6b7c8d
Create Date: 2026-02-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4f5a6b7c8d9e"
down_revision = "3e4f5a6b7c8d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_settings",
        sa.Column(
            "refund_user_monthly_limit", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.add_column(
        "system_settings",
        sa.Column(
            "refund_user_monthly_window_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
    )


def downgrade():
    op.drop_column("system_settings", "refund_user_monthly_window_days")
    op.drop_column("system_settings", "refund_user_monthly_limit")
