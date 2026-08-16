"""admin v0.1 fields

Revision ID: 7f2fbd0a4c7a
Revises: 4c5c7d8a9b10
Create Date: 2026-01-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7f2fbd0a4c7a"
down_revision = "4c5c7d8a9b10"
branch_labels = None
depends_on = None


def upgrade():
    # Extend enum types
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'COMPLETED'")
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'TIMEOUT'")

    # Users extra fields
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)
    op.add_column(
        "users", sa.Column("balance", sa.Numeric(18, 2), nullable=False, server_default="0")
    )
    op.add_column(
        "users",
        sa.Column("expire_remind", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "users",
        sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("users", sa.Column("inviter_user_id", sa.UUID(), nullable=True))
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index("ix_users_inviter_user_id", "users", ["inviter_user_id"], unique=False)
    op.create_foreign_key("fk_users_inviter_user_id", "users", "users", ["inviter_user_id"], ["id"])

    # Orders link to plans
    op.add_column("orders", sa.Column("plan_id", sa.UUID(), nullable=True))
    op.create_index("ix_orders_plan_id", "orders", ["plan_id"], unique=False)
    op.create_foreign_key("fk_orders_plan_id", "orders", "plans", ["plan_id"], ["id"])

    # Subscriptions billing cycle
    op.execute("CREATE TYPE billingcycle AS ENUM ('UNSET', 'MONTHLY', 'QUARTERLY', 'YEARLY')")
    op.add_column(
        "subscriptions",
        sa.Column(
            "billing_cycle",
            sa.Enum("UNSET", "MONTHLY", "QUARTERLY", "YEARLY", name="billingcycle"),
            nullable=False,
            server_default="UNSET",
        ),
    )

    # Balance transactions
    op.create_table(
        "balance_transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("operator_user_id", sa.UUID(), nullable=False),
        sa.Column("delta", sa.Numeric(18, 2), nullable=False),
        sa.Column("before_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("after_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["operator_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_balance_transactions_user_id", "balance_transactions", ["user_id"], unique=False
    )
    op.create_index(
        "ix_balance_transactions_operator_user_id",
        "balance_transactions",
        ["operator_user_id"],
        unique=False,
    )

    # Invitations
    op.create_table(
        "invitations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("invitee_email", sa.String(length=255), nullable=False),
        sa.Column("inviter_user_id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=True),
        sa.Column("initial_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "ACCEPTED", "EXPIRED", name="invitationstatus"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["inviter_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invitations_token", "invitations", ["token"], unique=True)
    op.create_index(
        "ix_invitations_inviter_user_id", "invitations", ["inviter_user_id"], unique=False
    )


def downgrade():
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=False)
    op.drop_index("ix_invitations_inviter_user_id", table_name="invitations")
    op.drop_index("ix_invitations_token", table_name="invitations")
    op.drop_table("invitations")

    op.drop_index("ix_balance_transactions_operator_user_id", table_name="balance_transactions")
    op.drop_index("ix_balance_transactions_user_id", table_name="balance_transactions")
    op.drop_table("balance_transactions")

    op.drop_column("subscriptions", "billing_cycle")
    op.execute("DROP TYPE IF EXISTS billingcycle")

    op.drop_constraint("fk_orders_plan_id", "orders", type_="foreignkey")
    op.drop_index("ix_orders_plan_id", table_name="orders")
    op.drop_column("orders", "plan_id")

    op.drop_constraint("fk_users_inviter_user_id", "users", type_="foreignkey")
    op.drop_index("ix_users_inviter_user_id", table_name="users")
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "inviter_user_id")
    op.drop_column("users", "trial_used")
    op.drop_column("users", "expire_remind")
    op.drop_column("users", "balance")
