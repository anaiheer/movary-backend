"""add tmdb settings

Revision ID: cc45dd67ee89
Revises: bb34cc56dd78
Create Date: 2026-01-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "cc45dd67ee89"
down_revision = "bb34cc56dd78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_settings", sa.Column("tmdb_base_url", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "system_settings", sa.Column("tmdb_api_key", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "system_settings", sa.Column("tmdb_proxy_url", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("system_settings", "tmdb_proxy_url")
    op.drop_column("system_settings", "tmdb_api_key")
    op.drop_column("system_settings", "tmdb_base_url")
