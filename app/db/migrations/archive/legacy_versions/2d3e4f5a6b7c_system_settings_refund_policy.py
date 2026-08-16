"""system_settings refund policy fields

Revision ID: 2d3e4f5a6b7c
Revises: 1c2d3e4f5a6b
Create Date: 2026-02-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2d3e4f5a6b7c"
down_revision = "1c2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_settings",
        sa.Column("refund_window_days", sa.Integer(), nullable=False, server_default=sa.text("7")),
    )
    op.add_column(
        "system_settings",
        sa.Column(
            "refund_forbid_if_sub_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade():
    op.drop_column("system_settings", "refund_forbid_if_sub_active")
    op.drop_column("system_settings", "refund_window_days")
