"""add telegram tables

Revision ID: d4e5f6a7b8c9
Revises: 2a4c6e8f0b1d
Create Date: 2026-04-01 20:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "d4e5f6a7b8c9"
down_revision = "2a4c6e8f0b1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "telegram_user_bindings" not in tables:
        op.create_table(
            "telegram_user_bindings",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("telegram_user_id", sa.String(length=64), nullable=False),
            sa.Column("telegram_username", sa.String(length=255), nullable=True),
            sa.Column("telegram_first_name", sa.String(length=255), nullable=True),
            sa.Column("telegram_last_name", sa.String(length=255), nullable=True),
            sa.Column("telegram_language_code", sa.String(length=10), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("bound_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.Column("unbound_at", sa.DateTime(), nullable=True),
            sa.Column(
                "last_interaction_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "uq_telegram_user_bindings_active_tg_user",
            "telegram_user_bindings",
            ["telegram_user_id"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
        )
        op.create_index(
            "uq_telegram_user_bindings_active_user",
            "telegram_user_bindings",
            ["user_id"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
        )
        op.create_index(
            "ix_telegram_user_bindings_user_id",
            "telegram_user_bindings",
            ["user_id"],
            unique=False,
        )

    if "telegram_notifications" not in tables:
        op.create_table(
            "telegram_notifications",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("type", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("reference_id", sa.String(length=64), nullable=True),
            sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_telegram_notifications_user_created_at",
            "telegram_notifications",
            ["user_id", "created_at"],
            unique=False,
        )
        op.create_index(
            "ix_telegram_notifications_user_type",
            "telegram_notifications",
            ["user_id", "type"],
            unique=False,
        )

    if "telegram_notification_preferences" not in tables:
        op.create_table(
            "telegram_notification_preferences",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column(
                "subscription_expiry",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("payment", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("vod", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("ticket", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "telegram_notification_preferences" in tables:
        op.drop_table("telegram_notification_preferences")
    if "telegram_notifications" in tables:
        op.drop_index("ix_telegram_notifications_user_type", table_name="telegram_notifications")
        op.drop_index(
            "ix_telegram_notifications_user_created_at",
            table_name="telegram_notifications",
        )
        op.drop_table("telegram_notifications")
    if "telegram_user_bindings" in tables:
        op.drop_index(
            "ix_telegram_user_bindings_user_id",
            table_name="telegram_user_bindings",
        )
        op.drop_index(
            "uq_telegram_user_bindings_active_user",
            table_name="telegram_user_bindings",
        )
        op.drop_index(
            "uq_telegram_user_bindings_active_tg_user",
            table_name="telegram_user_bindings",
        )
        op.drop_table("telegram_user_bindings")
