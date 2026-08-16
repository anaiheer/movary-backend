"""add social account bindings

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-04-02 16:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "f1a2b3c4d5e6"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "social_account_bindings" not in tables:
        op.create_table(
            "social_account_bindings",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_user_id", sa.String(length=255), nullable=False),
            sa.Column("provider_email", sa.String(length=255), nullable=True),
            sa.Column("provider_name", sa.String(length=255), nullable=True),
            sa.Column("provider_avatar_url", sa.Text(), nullable=True),
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
            "uq_social_account_bindings_active_provider_user",
            "social_account_bindings",
            ["provider", "provider_user_id"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
        )
        op.create_index(
            "uq_social_account_bindings_active_user_provider",
            "social_account_bindings",
            ["user_id", "provider"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
        )
        op.create_index(
            "ix_social_account_bindings_user_id",
            "social_account_bindings",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_social_account_bindings_provider",
            "social_account_bindings",
            ["provider"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "social_account_bindings" in tables:
        op.drop_index(
            "ix_social_account_bindings_provider",
            table_name="social_account_bindings",
        )
        op.drop_index(
            "ix_social_account_bindings_user_id",
            table_name="social_account_bindings",
        )
        op.drop_index(
            "uq_social_account_bindings_active_user_provider",
            table_name="social_account_bindings",
        )
        op.drop_index(
            "uq_social_account_bindings_active_provider_user",
            table_name="social_account_bindings",
        )
        op.drop_table("social_account_bindings")
