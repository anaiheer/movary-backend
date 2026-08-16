"""add tmdb cache settings

Revision ID: ee67ff90aa12
Revises: dd56ee78ff90
Create Date: 2026-01-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "ee67ff90aa12"
down_revision = "dd56ee78ff90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_settings", sa.Column("tmdb_cache_search_ttl", sa.Integer(), nullable=True)
    )
    op.add_column(
        "system_settings", sa.Column("tmdb_cache_discover_ttl", sa.Integer(), nullable=True)
    )
    op.add_column(
        "system_settings", sa.Column("tmdb_cache_genres_ttl", sa.Integer(), nullable=True)
    )
    op.add_column(
        "system_settings", sa.Column("tmdb_cache_companies_ttl", sa.Integer(), nullable=True)
    )
    op.add_column(
        "system_settings",
        sa.Column(
            "tmdb_warmup_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "system_settings", sa.Column("tmdb_warmup_categories", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "system_settings",
        sa.Column(
            "tmdb_warmup_include_genres",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "system_settings",
        sa.Column(
            "tmdb_warmup_include_companies",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    op.alter_column("system_settings", "tmdb_warmup_enabled", server_default=None)
    op.alter_column("system_settings", "tmdb_warmup_include_genres", server_default=None)
    op.alter_column("system_settings", "tmdb_warmup_include_companies", server_default=None)


def downgrade() -> None:
    op.drop_column("system_settings", "tmdb_warmup_include_companies")
    op.drop_column("system_settings", "tmdb_warmup_include_genres")
    op.drop_column("system_settings", "tmdb_warmup_categories")
    op.drop_column("system_settings", "tmdb_warmup_enabled")
    op.drop_column("system_settings", "tmdb_cache_companies_ttl")
    op.drop_column("system_settings", "tmdb_cache_genres_ttl")
    op.drop_column("system_settings", "tmdb_cache_discover_ttl")
    op.drop_column("system_settings", "tmdb_cache_search_ttl")
