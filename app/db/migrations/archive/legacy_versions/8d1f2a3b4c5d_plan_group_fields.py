"""add plan group fields

Revision ID: 8d1f2a3b4c5d
Revises: 7c8d9e0f1a2
Create Date: 2026-03-17 20:35:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8d1f2a3b4c5d"
down_revision = "7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("group_key", sa.String(length=64), nullable=True))
    op.add_column("plans", sa.Column("group_name", sa.String(length=255), nullable=True))
    op.add_column(
        "plans",
        sa.Column("tier_level", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )

    op.execute(
        """
        UPDATE plans
        SET
            group_key = CONCAT('legacy-', REPLACE(id::text, '-', '')),
            group_name = COALESCE(NULLIF(name, ''), CONCAT('Plan ', id::text))
        WHERE group_key IS NULL OR group_name IS NULL
        """
    )

    op.alter_column("plans", "group_key", nullable=False)
    op.alter_column("plans", "group_name", nullable=False)
    op.create_index("ix_plans_group_key", "plans", ["group_key"], unique=False)
    op.create_index("ix_plans_group_name", "plans", ["group_name"], unique=False)
    op.create_unique_constraint("uq_plan_group_tier", "plans", ["group_key", "tier_level"])
    op.alter_column("plans", "tier_level", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_plan_group_tier", "plans", type_="unique")
    op.drop_index("ix_plans_group_name", table_name="plans")
    op.drop_index("ix_plans_group_key", table_name="plans")
    op.drop_column("plans", "tier_level")
    op.drop_column("plans", "group_name")
    op.drop_column("plans", "group_key")
