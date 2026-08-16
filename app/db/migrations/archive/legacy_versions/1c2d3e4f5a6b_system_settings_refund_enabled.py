"""system_settings refund_enabled

Revision ID: 1c2d3e4f5a6b
Revises: 9a1b2c3d4e5f
Create Date: 2026-02-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "1c2d3e4f5a6b"
down_revision = "9a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_settings",
        sa.Column("refund_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade():
    op.drop_column("system_settings", "refund_enabled")
