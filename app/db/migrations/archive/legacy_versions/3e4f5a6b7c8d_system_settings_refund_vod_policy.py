"""system_settings refund policy: VOD usage threshold

Revision ID: 3e4f5a6b7c8d
Revises: 2d3e4f5a6b7c
Create Date: 2026-02-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3e4f5a6b7c8d"
down_revision = "2d3e4f5a6b7c"
branch_labels = None
depends_on = None


def upgrade():
    # Rename to better reflect actual policy meaning.
    op.alter_column(
        "system_settings",
        "refund_forbid_if_sub_active",
        new_column_name="refund_forbid_if_vod_used",
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
    op.add_column(
        "system_settings",
        sa.Column(
            "refund_vod_used_threshold", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade():
    op.drop_column("system_settings", "refund_vod_used_threshold")
    op.alter_column(
        "system_settings",
        "refund_forbid_if_vod_used",
        new_column_name="refund_forbid_if_sub_active",
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
