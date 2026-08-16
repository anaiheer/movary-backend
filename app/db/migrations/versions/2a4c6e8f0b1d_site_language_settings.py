"""add site language settings

Revision ID: 2a4c6e8f0b1d
Revises: 1f2e3d4c5b6a
Create Date: 2026-04-02 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.services.site_languages import DEFAULT_SITE_LANGUAGE, default_enabled_site_languages


revision = "2a4c6e8f0b1d"
down_revision = "1f2e3d4c5b6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}

    if "enabled_languages" not in columns:
        op.add_column("system_settings", sa.Column("enabled_languages", sa.JSON(), nullable=True))
    if "default_language" not in columns:
        op.add_column(
            "system_settings",
            sa.Column("default_language", sa.String(length=16), nullable=True),
        )

    settings_table = sa.table(
        "system_settings",
        sa.column("id"),
        sa.column("enabled_languages", sa.JSON()),
        sa.column("default_language", sa.String(length=16)),
    )
    rows = bind.execute(
        sa.select(
            settings_table.c.id,
            settings_table.c.enabled_languages,
            settings_table.c.default_language,
        )
    ).fetchall()
    for row in rows:
        values: dict[str, object] = {}
        if row.enabled_languages in (None, []):
            values["enabled_languages"] = default_enabled_site_languages()
        if not row.default_language:
            values["default_language"] = DEFAULT_SITE_LANGUAGE
        if values:
            bind.execute(
                settings_table.update().where(settings_table.c.id == row.id).values(**values)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}

    if "default_language" in columns:
        op.drop_column("system_settings", "default_language")
    if "enabled_languages" in columns:
        op.drop_column("system_settings", "enabled_languages")
