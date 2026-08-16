"""vod quota and settings

Revision ID: a1d2e3f4c5d6
Revises: 9b1e3f4d2c11
Create Date: 2026-01-15 13:35:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "a1d2e3f4c5d6"
down_revision = "9b1e3f4d2c11"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "plans", sa.Column("vod_movie_times", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "plans", sa.Column("vod_tv_times", sa.Integer(), server_default="0", nullable=False)
    )

    op.add_column(
        "users", sa.Column("vod_movie_limit", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "users", sa.Column("vod_tv_limit", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "users", sa.Column("vod_movie_used", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "users", sa.Column("vod_tv_used", sa.Integer(), server_default="0", nullable=False)
    )

    op.create_table(
        "vod_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("auto_approve", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("vod_settings")

    op.drop_column("users", "vod_tv_used")
    op.drop_column("users", "vod_movie_used")
    op.drop_column("users", "vod_tv_limit")
    op.drop_column("users", "vod_movie_limit")

    op.drop_column("plans", "vod_tv_times")
    op.drop_column("plans", "vod_movie_times")
