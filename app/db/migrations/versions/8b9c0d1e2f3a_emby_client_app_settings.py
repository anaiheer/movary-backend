"""add emby client app settings

Revision ID: 8b9c0d1e2f3a
Revises: c3d4e5f6a7b8
Create Date: 2026-04-01 23:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from app.services.client_apps import default_client_app_configs


revision = "8b9c0d1e2f3a"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "emby_client_apps" in columns:
        return

    op.add_column("system_settings", sa.Column("emby_client_apps", sa.JSON(), nullable=True))
    settings_table = sa.table(
        "system_settings",
        sa.column("id"),
        sa.column("emby_client_apps", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(settings_table.c.id, settings_table.c.emby_client_apps)
    ).fetchall()
    for row in rows:
        if row.emby_client_apps not in (None, []):
            continue
        bind.execute(
            settings_table.update()
            .where(settings_table.c.id == row.id)
            .values(emby_client_apps=default_client_app_configs())
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "emby_client_apps" not in columns:
        return

    op.drop_column("system_settings", "emby_client_apps")
