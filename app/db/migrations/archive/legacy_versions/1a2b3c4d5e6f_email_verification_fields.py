"""add email verification fields

Revision ID: 1a2b3c4d5e6f
Revises: f9a1b2c3d4e5
Create Date: 2026-01-22 01:10:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "1a2b3c4d5e6f"
down_revision = "f9a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.add_column(
        "users", sa.Column("email_verification_token", sa.String(length=128), nullable=True)
    )
    op.add_column("users", sa.Column("email_verification_expires_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_users_email_verification_token", "users", ["email_verification_token"], unique=False
    )

    op.alter_column("users", "email_verified", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_users_email_verification_token", table_name="users")
    op.drop_column("users", "email_verification_expires_at")
    op.drop_column("users", "email_verification_token")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verified")
