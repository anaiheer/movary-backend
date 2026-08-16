"""add email templates setting

Revision ID: 1f2e3d4c5b6a
Revises: 8b9c0d1e2f3a
Create Date: 2026-04-01 19:30:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "1f2e3d4c5b6a"
down_revision = "8b9c0d1e2f3a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "email_templates" in columns:
        return

    op.add_column("system_settings", sa.Column("email_templates", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    if "email_templates" not in columns:
        return

    op.drop_column("system_settings", "email_templates")
