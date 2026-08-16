"""remove legacy UNSET plan billing cycles

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-03-20 15:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "plans"):
        return

    affected_selects: list[str] = []
    if _has_column(bind, "plans", "default_billing_cycle"):
        affected_selects.append(
            "SELECT id AS plan_id FROM plans WHERE default_billing_cycle::text = 'UNSET'"
        )
    if _has_table(bind, "plan_billing_cycles") and _has_column(
        bind, "plan_billing_cycles", "billing_cycle"
    ):
        affected_selects.append(
            "SELECT plan_id FROM plan_billing_cycles WHERE billing_cycle::text = 'UNSET'"
        )

    if not affected_selects:
        return

    op.execute(f"CREATE TEMP TABLE tmp_unset_plans AS {' UNION '.join(affected_selects)}")

    if _has_table(bind, "plan_billing_cycles") and _has_column(
        bind, "plan_billing_cycles", "billing_cycle"
    ):
        bind.execute(
            sa.text(
                """
                DELETE FROM plan_billing_cycles
                WHERE billing_cycle::text = 'UNSET'
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE plan_billing_cycles
                SET is_default = false
                WHERE plan_id IN (SELECT plan_id FROM tmp_unset_plans)
                """
            )
        )
        bind.execute(
            sa.text(
                """
                WITH ranked AS (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY plan_id
                            ORDER BY sort_order ASC, created_at ASC, id ASC
                        ) AS row_no
                    FROM plan_billing_cycles
                    WHERE plan_id IN (SELECT plan_id FROM tmp_unset_plans)
                )
                UPDATE plan_billing_cycles target
                SET is_default = true
                FROM ranked source
                WHERE target.id = source.id
                  AND source.row_no = 1
                """
            )
        )
        bind.execute(
            sa.text(
                """
                WITH chosen AS (
                    SELECT
                        plan_id,
                        billing_cycle,
                        price,
                        duration_days
                    FROM (
                        SELECT
                            plan_id,
                            billing_cycle,
                            price,
                            duration_days,
                            ROW_NUMBER() OVER (
                                PARTITION BY plan_id
                                ORDER BY sort_order ASC, created_at ASC, id ASC
                            ) AS row_no
                        FROM plan_billing_cycles
                        WHERE plan_id IN (SELECT plan_id FROM tmp_unset_plans)
                    ) ranked
                    WHERE row_no = 1
                )
                UPDATE plans target
                SET default_billing_cycle = chosen.billing_cycle,
                    price = chosen.price,
                    duration_days = chosen.duration_days
                FROM chosen
                WHERE target.id = chosen.plan_id
                """
            )
        )

    plan_updates = [
        "default_billing_cycle = 'MONTHLY'",
        "price = 0",
        "duration_days = 0",
    ]
    if _has_column(bind, "plans", "monthly_price"):
        plan_updates.append("monthly_price = NULL")
    if _has_column(bind, "plans", "quarterly_price"):
        plan_updates.append("quarterly_price = NULL")
    if _has_column(bind, "plans", "semi_annual_price"):
        plan_updates.append("semi_annual_price = NULL")
    if _has_column(bind, "plans", "annual_price"):
        plan_updates.append("annual_price = NULL")
    if _has_column(bind, "plans", "lifetime_price"):
        plan_updates.append("lifetime_price = NULL")
    if _has_column(bind, "plans", "trial_price"):
        plan_updates.append("trial_price = NULL")
    if _has_column(bind, "plans", "trial_days"):
        plan_updates.append("trial_days = NULL")
    if _has_column(bind, "plans", "status"):
        plan_updates.append("status = 'OFF'")
    if _has_column(bind, "plans", "is_visible"):
        plan_updates.append("is_visible = false")

    bind.execute(
        sa.text(
            f"""
            UPDATE plans
            SET {", ".join(plan_updates)}
            WHERE id IN (SELECT plan_id FROM tmp_unset_plans)
              AND NOT EXISTS (
                  SELECT 1
                  FROM plan_billing_cycles
                  WHERE plan_billing_cycles.plan_id = plans.id
              )
            """
        )
    )

    op.execute("DROP TABLE tmp_unset_plans")


def downgrade() -> None:
    pass
