"""add system task logs and settings fields

Revision ID: 4e5f6a7b8c90
Revises: 3c5e7a9b0d12
Create Date: 2026-01-26 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "4e5f6a7b8c90"
down_revision = "3c5e7a9b0d12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_task_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_key", sa.String(length=64), nullable=False),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("run_at", sa.DateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["system_tasks.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_system_task_logs_task_id", "system_task_logs", ["task_id"])
    op.create_index("ix_system_task_logs_task_key", "system_task_logs", ["task_key"])
    op.create_index("ix_system_task_logs_run_at", "system_task_logs", ["run_at"])

    op.add_column("system_settings", sa.Column("site_name", sa.String(length=128), nullable=True))
    op.add_column("system_settings", sa.Column("site_url", sa.String(length=255), nullable=True))
    op.add_column(
        "system_settings",
        sa.Column("task_log_retention_days", sa.Integer(), nullable=False, server_default="30"),
    )


def downgrade() -> None:
    op.drop_column("system_settings", "task_log_retention_days")
    op.drop_column("system_settings", "site_url")
    op.drop_column("system_settings", "site_name")
    op.drop_index("ix_system_task_logs_run_at", table_name="system_task_logs")
    op.drop_index("ix_system_task_logs_task_key", table_name="system_task_logs")
    op.drop_index("ix_system_task_logs_task_id", table_name="system_task_logs")
    op.drop_table("system_task_logs")
