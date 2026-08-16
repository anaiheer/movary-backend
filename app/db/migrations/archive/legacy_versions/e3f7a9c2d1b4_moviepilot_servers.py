"""moviepilot servers table

Revision ID: e3f7a9c2d1b4
Revises: d2e4f6a8b9c0
Create Date: 2026-01-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e3f7a9c2d1b4"
down_revision = "d2e4f6a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moviepilot_servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("api_token", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="OFFLINE"),
        sa.Column("latency", sa.Integer(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_moviepilot_servers_name", "moviepilot_servers", ["name"])


def downgrade() -> None:
    op.drop_index("ix_moviepilot_servers_name", table_name="moviepilot_servers")
    op.drop_table("moviepilot_servers")
