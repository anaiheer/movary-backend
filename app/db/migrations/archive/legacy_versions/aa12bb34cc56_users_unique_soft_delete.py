"""users unique indexes for soft delete

Revision ID: aa12bb34cc56
Revises: 0f1a2b3c4d5e
Create Date: 2026-01-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "aa12bb34cc56"
down_revision = "0f1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_users_username_active",
        "users",
        ["username"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_username_active", table_name="users")
    op.drop_index("uq_users_email_active", table_name="users")
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
