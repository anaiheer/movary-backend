"""add plan billing cycle table

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-03-18 22:30:00
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c4d5e6f7a8b9"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


SORT_ORDER = {
    "TRIAL": 10,
    "UNSET": 20,
    "MONTHLY": 30,
    "QUARTERLY": 40,
    "SEMI_ANNUAL": 50,
    "YEARLY": 60,
    "LIFETIME": 80,
}

BILLING_CYCLE_ENUM = postgresql.ENUM(
    "UNSET",
    "MONTHLY",
    "QUARTERLY",
    "YEARLY",
    "TRIAL",
    "SEMI_ANNUAL",
    "LIFETIME",
    name="billingcycle",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "plan_billing_cycles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_cycle", BILLING_CYCLE_ENUM, nullable=False),
        sa.Column("price", sa.Numeric(18, 2), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "billing_cycle", name="uq_plan_billing_cycle"),
    )
    op.create_index(
        op.f("ix_plan_billing_cycles_plan_id"), "plan_billing_cycles", ["plan_id"], unique=False
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, price, duration_days, default_billing_cycle,
                   trial_price, trial_days,
                   monthly_price, quarterly_price, semi_annual_price,
                   annual_price, lifetime_price
            FROM plans
            """
        )
    ).mappings()

    for row in rows:
        cycles: list[dict] = []

        def add_cycle(cycle: str, price, duration_days: int) -> None:
            if price is None:
                return
            cycles.append(
                {
                    "id": uuid.uuid4(),
                    "plan_id": row["id"],
                    "billing_cycle": cycle,
                    "price": price,
                    "duration_days": int(duration_days or 0),
                    "is_default": str(row["default_billing_cycle"] or "") == cycle,
                    "sort_order": SORT_ORDER.get(cycle, 999),
                }
            )

        add_cycle("UNSET", row["price"], row["duration_days"] or 0)
        add_cycle("TRIAL", row["trial_price"], row["trial_days"] or 0)
        add_cycle("MONTHLY", row["monthly_price"], 30)
        add_cycle("QUARTERLY", row["quarterly_price"], 90)
        add_cycle("SEMI_ANNUAL", row["semi_annual_price"], 180)
        add_cycle("YEARLY", row["annual_price"], 365)
        add_cycle("LIFETIME", row["lifetime_price"], 0)

        if cycles and not any(item["is_default"] for item in cycles):
            cycles[0]["is_default"] = True

        for cycle in cycles:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO plan_billing_cycles
                        (id, plan_id, billing_cycle, price, duration_days, is_default, sort_order)
                    VALUES
                        (:id, :plan_id, :billing_cycle, :price, :duration_days, :is_default, :sort_order)
                    """
                ),
                cycle,
            )

    op.alter_column("plan_billing_cycles", "duration_days", server_default=None)
    op.alter_column("plan_billing_cycles", "sort_order", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_plan_billing_cycles_plan_id"), table_name="plan_billing_cycles")
    op.drop_table("plan_billing_cycles")
