"""add subscription groups and plan default billing cycle

Revision ID: 9a2b3c4d5e6f
Revises: 8d1f2a3b4c5d
Create Date: 2026-03-18 12:10:00
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "9a2b3c4d5e6f"
down_revision = "8d1f2a3b4c5d"
branch_labels = None
depends_on = None


billing_cycle_enum = sa.Enum(
    "TRIAL",
    "UNSET",
    "MONTHLY",
    "QUARTERLY",
    "SEMI_ANNUAL",
    "YEARLY",
    "LIFETIME",
    name="billingcycle",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "subscription_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("tier_count", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("key", name="uq_subscription_groups_key"),
        sa.UniqueConstraint("name", name="uq_subscription_groups_name"),
    )
    op.create_index("ix_subscription_groups_key", "subscription_groups", ["key"], unique=False)
    op.create_index("ix_subscription_groups_name", "subscription_groups", ["name"], unique=False)

    op.add_column(
        "plans",
        sa.Column(
            "default_billing_cycle",
            billing_cycle_enum,
            nullable=False,
            server_default="UNSET",
        ),
    )

    connection = op.get_bind()
    plan_rows = connection.execute(
        sa.text(
            """
            SELECT group_key, group_name, MAX(tier_level) AS max_tier
            FROM plans
            GROUP BY group_key, group_name
            """
        )
    ).fetchall()
    if plan_rows:
        op.bulk_insert(
            sa.table(
                "subscription_groups",
                sa.column("id", postgresql.UUID(as_uuid=True)),
                sa.column("key", sa.String),
                sa.column("name", sa.String),
                sa.column("description", sa.String),
                sa.column("tier_count", sa.Integer),
            ),
            [
                {
                    "id": uuid.uuid4(),
                    "key": row.group_key,
                    "name": row.group_name,
                    "description": None,
                    "tier_count": max(int(row.max_tier or 1), 1),
                }
                for row in plan_rows
            ],
        )

    connection.execute(
        sa.text(
            """
            UPDATE plans
            SET default_billing_cycle = (
                CASE
                    WHEN monthly_price IS NOT NULL AND duration_days = 30 THEN 'MONTHLY'
                    WHEN quarterly_price IS NOT NULL AND duration_days = 90 THEN 'QUARTERLY'
                    WHEN semi_annual_price IS NOT NULL AND duration_days = 180 THEN 'SEMI_ANNUAL'
                    WHEN annual_price IS NOT NULL AND duration_days IN (365, 366) THEN 'YEARLY'
                    WHEN lifetime_price IS NOT NULL AND duration_days = 0 THEN 'LIFETIME'
                    WHEN trial_price IS NOT NULL AND COALESCE(trial_days, 0) > 0 AND duration_days = trial_days THEN 'TRIAL'
                    ELSE 'UNSET'
                END
            )::billingcycle
            """
        )
    )

    op.alter_column("plans", "default_billing_cycle", server_default=None)
    op.alter_column("subscription_groups", "tier_count", server_default=None)
    op.alter_column("subscription_groups", "created_at", server_default=None)
    op.alter_column("subscription_groups", "updated_at", server_default=None)


def downgrade() -> None:
    op.drop_column("plans", "default_billing_cycle")
    op.drop_index("ix_subscription_groups_name", table_name="subscription_groups")
    op.drop_index("ix_subscription_groups_key", table_name="subscription_groups")
    op.drop_table("subscription_groups")
