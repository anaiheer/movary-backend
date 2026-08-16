"""add vod favorites

Revision ID: 4e8d2d7d3d4b
Revises: b7c60762f094
Create Date: 2026-01-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4e8d2d7d3d4b"
down_revision = "b7c60762f094"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vod_favorites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("poster_url", sa.String(length=500), nullable=True),
        sa.Column("backdrop_url", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "tmdb_id", "media_type", name="uq_vod_favorites_user_tmdb"),
    )
    op.create_index(
        op.f("ix_vod_favorites_created_at"), "vod_favorites", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_vod_favorites_tmdb_id"), "vod_favorites", ["tmdb_id"], unique=False)
    op.create_index(op.f("ix_vod_favorites_user_id"), "vod_favorites", ["user_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_vod_favorites_user_id"), table_name="vod_favorites")
    op.drop_index(op.f("ix_vod_favorites_tmdb_id"), table_name="vod_favorites")
    op.drop_index(op.f("ix_vod_favorites_created_at"), table_name="vod_favorites")
    op.drop_table("vod_favorites")
