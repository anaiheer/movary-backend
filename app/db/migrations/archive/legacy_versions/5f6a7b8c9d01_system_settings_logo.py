"""add site logo to system settings

Revision ID: 5f6a7b8c9d01
Revises: 4e5f6a7b8c90
Create Date: 2026-01-26 11:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "5f6a7b8c9d01"
down_revision = "4e5f6a7b8c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_settings", sa.Column("site_logo_url", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("system_settings", "site_logo_url")
