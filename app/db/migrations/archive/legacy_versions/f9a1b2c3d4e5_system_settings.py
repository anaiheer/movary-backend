"""system settings

Revision ID: f9a1b2c3d4e5
Revises: 0f1a2b3c4d5e
Create Date: 2026-01-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f9a1b2c3d4e5"
down_revision = "0f1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("default_theme", sa.String(length=16), nullable=False, server_default="dark"),
        sa.Column(
            "email_verification_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("smtp_host", sa.String(length=255), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_user", sa.String(length=255), nullable=True),
        sa.Column("smtp_password", sa.String(length=255), nullable=True),
        sa.Column("smtp_from", sa.String(length=255), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("smtp_use_ssl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("epay_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("epay_merchant_id", sa.String(length=64), nullable=True),
        sa.Column("epay_key", sa.String(length=255), nullable=True),
        sa.Column("epay_gateway", sa.String(length=255), nullable=True),
        sa.Column("epay_notify_url", sa.String(length=255), nullable=True),
        sa.Column("epay_return_url", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
