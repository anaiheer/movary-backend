"""emby server extended fields

Revision ID: 4c5c7d8a9b10
Revises: b7c60762f094
Create Date: 2026-01-14 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "4c5c7d8a9b10"
down_revision = "b7c60762f094"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("emby_servers", sa.Column("external_url", sa.String(length=255), nullable=True))
    op.add_column("emby_servers", sa.Column("backup_url", sa.String(length=255), nullable=True))
    op.add_column("emby_servers", sa.Column("webhook_url", sa.String(length=255), nullable=True))
    op.add_column("emby_servers", sa.Column("description", sa.String(length=1000), nullable=True))
    op.add_column(
        "emby_servers",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "emby_servers",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_emby_servers_priority", "emby_servers", ["priority"], unique=False)

    op.create_unique_constraint(
        "uq_emby_account_user_server",
        "emby_accounts",
        ["user_id", "emby_server_id"],
    )


def downgrade():
    op.drop_constraint("uq_emby_account_user_server", "emby_accounts", type_="unique")
    op.drop_index("ix_emby_servers_priority", table_name="emby_servers")
    op.drop_column("emby_servers", "is_default")
    op.drop_column("emby_servers", "priority")
    op.drop_column("emby_servers", "description")
    op.drop_column("emby_servers", "webhook_url")
    op.drop_column("emby_servers", "backup_url")
    op.drop_column("emby_servers", "external_url")
