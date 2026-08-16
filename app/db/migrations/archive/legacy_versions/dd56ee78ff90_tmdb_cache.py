"""add tmdb cache

Revision ID: dd56ee78ff90
Revises: cc45dd67ee89
Create Date: 2026-01-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "dd56ee78ff90"
down_revision = "cc45dd67ee89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tmdb_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("cache_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_tmdb_cache_cache_key", "tmdb_cache", ["cache_key"], unique=True)
    op.create_index("ix_tmdb_cache_expires_at", "tmdb_cache", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tmdb_cache_expires_at", table_name="tmdb_cache")
    op.drop_index("ix_tmdb_cache_cache_key", table_name="tmdb_cache")
    op.drop_table("tmdb_cache")
