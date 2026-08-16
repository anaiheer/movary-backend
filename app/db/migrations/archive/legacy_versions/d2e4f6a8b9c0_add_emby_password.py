"""add emby password column

Revision ID: d2e4f6a8b9c0
Revises: c1a2b3c4d5e6
Create Date: 2026-01-18
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e4f6a8b9c0"
down_revision = "c1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("emby_accounts", sa.Column("emby_password", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("emby_accounts", "emby_password")
