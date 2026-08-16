"""add tmdb warmup interval

Revision ID: ff78aa12bb34
Revises: ee67ff90aa12
Create Date: 2026-01-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "ff78aa12bb34"
down_revision = "ee67ff90aa12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_settings", sa.Column("tmdb_warmup_interval_seconds", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("system_settings", "tmdb_warmup_interval_seconds")
