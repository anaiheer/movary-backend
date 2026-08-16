"""add user avatar url

Revision ID: f4a1b2c3d4e5
Revises: e3f7a9c2d1b4
Create Date: 2026-01-20
"""

from alembic import op
import sqlalchemy as sa


revision = "f4a1b2c3d4e5"
down_revision = "e3f7a9c2d1b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_url")
