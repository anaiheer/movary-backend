"""add abnormal user status

Revision ID: c1a2b3c4d5e6
Revises: a1d2e3f4c5d6
Create Date: 2026-01-17
"""

from alembic import op


revision = "c1a2b3c4d5e6"
down_revision = "a1d2e3f4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE userstatus ADD VALUE IF NOT EXISTS 'ABNORMAL'")


def downgrade() -> None:
    pass
