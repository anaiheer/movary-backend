"""migrate legacy zero-price markers to base-edition values

Revision ID: b7c8d9e0f1a2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-16 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f1a2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

# 免费版时代遗留的零价支付/分组标记统一迁移为基础版标记。
# 全新安装没有遗留数据，本迁移实际为空操作。


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    conn.execute(sa.text("UPDATE orders SET pay_provider = 'ZERO' WHERE pay_provider = 'FREE'"))
    conn.execute(
        sa.text(
            "UPDATE subscription_groups SET key = 'default', name = '默认分组' WHERE key = 'free-default'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE plans SET group_key = 'default', group_name = '默认分组' WHERE group_key = 'free-default'"
        )
    )

    if dialect == "postgresql":
        conn.execute(
            sa.text(
                "UPDATE orders SET pay_payload = jsonb_set(pay_payload::jsonb, '{pay_type}', '\"zero\"', false)::json "
                "WHERE pay_payload->>'pay_type' = 'free'"
            )
        )
    elif dialect == "sqlite":
        conn.execute(
            sa.text(
                "UPDATE orders SET pay_payload = json_set(pay_payload, '$.pay_type', 'zero') "
                "WHERE json_extract(pay_payload, '$.pay_type') = 'free'"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    conn.execute(sa.text("UPDATE orders SET pay_provider = 'FREE' WHERE pay_provider = 'ZERO'"))
    conn.execute(
        sa.text(
            "UPDATE subscription_groups SET key = 'free-default', name = '免费版默认分组' WHERE key = 'default'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE plans SET group_key = 'free-default', group_name = '免费版默认分组' WHERE group_key = 'default'"
        )
    )

    if dialect == "postgresql":
        conn.execute(
            sa.text(
                "UPDATE orders SET pay_payload = jsonb_set(pay_payload::jsonb, '{pay_type}', '\"free\"', false)::json "
                "WHERE pay_payload->>'pay_type' = 'zero'"
            )
        )
    elif dialect == "sqlite":
        conn.execute(
            sa.text(
                "UPDATE orders SET pay_payload = json_set(pay_payload, '$.pay_type', 'free') "
                "WHERE json_extract(pay_payload, '$.pay_type') = 'zero'"
            )
        )
