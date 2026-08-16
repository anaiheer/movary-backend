"""add social auth settings

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-01 21:40:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.services.social_auth import default_social_auth_providers


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "social_auth_providers" not in columns:
        op.add_column(
            "system_settings", sa.Column("social_auth_providers", sa.JSON(), nullable=True)
        )

    settings_table = sa.table(
        "system_settings",
        sa.column("id"),
        sa.column("social_auth_providers", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(settings_table.c.id, settings_table.c.social_auth_providers)
    ).fetchall()
    for row in rows:
        if row.social_auth_providers not in (None, {}):
            continue
        bind.execute(
            settings_table.update()
            .where(settings_table.c.id == row.id)
            .values(social_auth_providers=default_social_auth_providers())
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "social_auth_providers" in columns:
        op.drop_column("system_settings", "social_auth_providers")
