"""remove two-year billing cycle and biennial plan pricing

Revision ID: d6e7f8a9b0c1
Revises: c4d5e6f7a8b9
Create Date: 2026-03-20 11:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d6e7f8a9b0c1"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


UPGRADE_BILLING_CYCLE_VALUES = (
    "TRIAL",
    "UNSET",
    "MONTHLY",
    "QUARTERLY",
    "SEMI_ANNUAL",
    "YEARLY",
    "LIFETIME",
)

DOWNGRADE_BILLING_CYCLE_VALUES = (
    "TRIAL",
    "UNSET",
    "MONTHLY",
    "QUARTERLY",
    "SEMI_ANNUAL",
    "YEARLY",
    "TWO_YEAR",
    "LIFETIME",
)


def _has_table(bind, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def _enum_has_value(bind, enum_name: str, value: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1
                FROM pg_type t
                JOIN pg_enum e ON e.enumtypid = t.oid
                WHERE t.typname = :enum_name
                  AND e.enumlabel = :enum_value
                LIMIT 1
                """
            ),
            {"enum_name": enum_name, "enum_value": value},
        ).scalar()
    )


def _recreate_billing_cycle_enum(bind, values: tuple[str, ...]) -> None:
    if not bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'billingcycle' LIMIT 1")
    ).scalar():
        return

    op.execute("ALTER TYPE billingcycle RENAME TO billingcycle_old")
    postgresql.ENUM(*values, name="billingcycle").create(bind, checkfirst=False)

    if _has_column(bind, "plans", "default_billing_cycle"):
        op.execute("ALTER TABLE plans ALTER COLUMN default_billing_cycle DROP DEFAULT")
        op.execute(
            """
            ALTER TABLE plans
            ALTER COLUMN default_billing_cycle
            TYPE billingcycle
            USING default_billing_cycle::text::billingcycle
            """
        )

    if _has_column(bind, "plan_billing_cycles", "billing_cycle"):
        op.execute(
            """
            ALTER TABLE plan_billing_cycles
            ALTER COLUMN billing_cycle
            TYPE billingcycle
            USING billing_cycle::text::billingcycle
            """
        )

    if _has_column(bind, "subscriptions", "billing_cycle"):
        op.execute("ALTER TABLE subscriptions ALTER COLUMN billing_cycle DROP DEFAULT")
        op.execute(
            """
            ALTER TABLE subscriptions
            ALTER COLUMN billing_cycle
            TYPE billingcycle
            USING billing_cycle::text::billingcycle
            """
        )
        op.execute("ALTER TABLE subscriptions ALTER COLUMN billing_cycle SET DEFAULT 'UNSET'")

    op.execute("DROP TYPE billingcycle_old")


def upgrade() -> None:
    bind = op.get_bind()

    affected_selects: list[str] = []
    if _has_table(bind, "plans"):
        conditions: list[str] = []
        if _has_column(bind, "plans", "default_billing_cycle"):
            conditions.append("default_billing_cycle::text = 'TWO_YEAR'")
        if _has_column(bind, "plans", "biennial_price"):
            conditions.append("biennial_price IS NOT NULL")
        if conditions:
            affected_selects.append(
                f"SELECT id AS plan_id FROM plans WHERE {' OR '.join(conditions)}"
            )

    if _has_column(bind, "plan_billing_cycles", "billing_cycle"):
        affected_selects.append(
            "SELECT plan_id FROM plan_billing_cycles WHERE billing_cycle::text = 'TWO_YEAR'"
        )

    if affected_selects:
        op.execute(f"CREATE TEMP TABLE tmp_two_year_plans AS {' UNION '.join(affected_selects)}")

    if _has_column(bind, "subscriptions", "billing_cycle"):
        bind.execute(
            sa.text(
                """
                UPDATE subscriptions
                SET billing_cycle = 'UNSET'
                WHERE billing_cycle::text = 'TWO_YEAR'
                """
            )
        )

    if _has_column(bind, "plan_billing_cycles", "billing_cycle"):
        bind.execute(
            sa.text(
                """
                DELETE FROM plan_billing_cycles
                WHERE billing_cycle::text = 'TWO_YEAR'
                """
            )
        )

    if affected_selects and _has_table(bind, "plan_billing_cycles"):
        bind.execute(
            sa.text(
                """
                UPDATE plan_billing_cycles
                SET is_default = false
                WHERE plan_id IN (SELECT plan_id FROM tmp_two_year_plans)
                """
            )
        )
        bind.execute(
            sa.text(
                """
                WITH ranked AS (
                    SELECT
                        id,
                        plan_id,
                        billing_cycle,
                        price,
                        duration_days,
                        ROW_NUMBER() OVER (
                            PARTITION BY plan_id
                            ORDER BY sort_order ASC, created_at ASC, id ASC
                        ) AS row_no
                    FROM plan_billing_cycles
                    WHERE plan_id IN (SELECT plan_id FROM tmp_two_year_plans)
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
                        WHERE plan_id IN (SELECT plan_id FROM tmp_two_year_plans)
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

    if affected_selects and _has_table(bind, "plans"):
        orphan_updates = [
            "default_billing_cycle = 'UNSET'",
            "price = 0",
            "duration_days = 0",
        ]
        if _has_column(bind, "plans", "status"):
            orphan_updates.append("status = 'OFF'")
        bind.execute(
            sa.text(
                f"""
                UPDATE plans
                SET {", ".join(orphan_updates)}
                WHERE id IN (SELECT plan_id FROM tmp_two_year_plans)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM plan_billing_cycles
                      WHERE plan_billing_cycles.plan_id = plans.id
                  )
                """
            )
        )
        op.execute("DROP TABLE tmp_two_year_plans")

    if _has_column(bind, "plans", "biennial_price"):
        bind.execute(
            sa.text("UPDATE plans SET biennial_price = NULL WHERE biennial_price IS NOT NULL")
        )
        op.drop_column("plans", "biennial_price")

    if _enum_has_value(bind, "billingcycle", "TWO_YEAR"):
        _recreate_billing_cycle_enum(bind, UPGRADE_BILLING_CYCLE_VALUES)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "plans") and not _has_column(bind, "plans", "biennial_price"):
        op.add_column("plans", sa.Column("biennial_price", sa.Numeric(18, 2), nullable=True))

    if not _enum_has_value(bind, "billingcycle", "TWO_YEAR"):
        _recreate_billing_cycle_enum(bind, DOWNGRADE_BILLING_CYCLE_VALUES)
