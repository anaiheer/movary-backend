"""plan admin fields

Revision ID: 9b1e3f4d2c11
Revises: 7f2fbd0a4c7a
Create Date: 2026-01-16 00:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "9b1e3f4d2c11"
down_revision = "7f2fbd0a4c7a"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TYPE planserverallocationstrategy AS ENUM ('ALL', 'LEAST_LOAD')")

    op.alter_column(
        "plans", "price", existing_type=sa.Float(), type_=sa.Numeric(18, 2), nullable=False
    )
    op.add_column("plans", sa.Column("trial_price", sa.Numeric(18, 2), nullable=True))
    op.add_column("plans", sa.Column("trial_days", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("monthly_price", sa.Numeric(18, 2), nullable=True))
    op.add_column("plans", sa.Column("quarterly_price", sa.Numeric(18, 2), nullable=True))
    op.add_column("plans", sa.Column("semi_annual_price", sa.Numeric(18, 2), nullable=True))
    op.add_column("plans", sa.Column("annual_price", sa.Numeric(18, 2), nullable=True))
    op.add_column("plans", sa.Column("lifetime_price", sa.Numeric(18, 2), nullable=True))
    op.add_column(
        "plans",
        sa.Column(
            "auto_renew_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "server_allocation_strategy",
            sa.Enum("ALL", "LEAST_LOAD", name="planserverallocationstrategy"),
            nullable=False,
            server_default="ALL",
        ),
    )
    op.add_column(
        "plans",
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    op.create_table(
        "plan_server_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("server_id", sa.UUID(), nullable=False),
        sa.Column("template_emby_user_id", sa.String(length=255), nullable=False),
        sa.Column("template_emby_username", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["emby_servers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "server_id", name="uq_plan_server"),
    )
    op.create_index(
        "ix_plan_server_assignments_plan_id", "plan_server_assignments", ["plan_id"], unique=False
    )
    op.create_index(
        "ix_plan_server_assignments_server_id",
        "plan_server_assignments",
        ["server_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_plan_server_assignments_server_id", table_name="plan_server_assignments")
    op.drop_index("ix_plan_server_assignments_plan_id", table_name="plan_server_assignments")
    op.drop_table("plan_server_assignments")

    op.drop_column("plans", "is_visible")
    op.drop_column("plans", "server_allocation_strategy")
    op.drop_column("plans", "auto_renew_enabled")
    op.drop_column("plans", "lifetime_price")
    op.drop_column("plans", "annual_price")
    op.drop_column("plans", "semi_annual_price")
    op.drop_column("plans", "quarterly_price")
    op.drop_column("plans", "monthly_price")
    op.drop_column("plans", "trial_days")
    op.drop_column("plans", "trial_price")
    op.alter_column(
        "plans", "price", existing_type=sa.Numeric(18, 2), type_=sa.Float(), nullable=False
    )

    op.execute("DROP TYPE IF EXISTS planserverallocationstrategy")
