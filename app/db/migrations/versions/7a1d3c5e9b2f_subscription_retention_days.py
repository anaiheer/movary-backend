"""add subscription retention days setting

Revision ID: 7a1d3c5e9b2f
Revises: 4f7b8d2c1a9e
Create Date: 2026-03-24 23:40:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "7a1d3c5e9b2f"
down_revision = "4f7b8d2c1a9e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "subscription_retention_days" in columns:
        return

    op.add_column(
        "system_settings",
        sa.Column("subscription_retention_days", sa.Integer(), nullable=False, server_default="30"),
    )
    op.alter_column("system_settings", "subscription_retention_days", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "subscription_retention_days" not in columns:
        return

    op.drop_column("system_settings", "subscription_retention_days")
