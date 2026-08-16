"""expand billing cycle enum and simplify generated group keys

Revision ID: b2c3d4e5f6a7
Revises: 9a2b3c4d5e6f
Create Date: 2026-03-18 16:25:00
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "9a2b3c4d5e6f"
branch_labels = None
depends_on = None


def _build_group_key(name: str | None, used: set[str]) -> str:
    base = (name or "").strip().lower()
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"[^\u4e00-\u9fff\w-]+", "", base, flags=re.UNICODE)
    base = re.sub(r"-{2,}", "-", base).strip("-_")
    if not base:
        base = "group"

    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def upgrade() -> None:
    connection = op.get_bind()

    with op.get_context().autocommit_block():
        connection.execute(sa.text("ALTER TYPE billingcycle ADD VALUE IF NOT EXISTS 'TRIAL'"))
        connection.execute(sa.text("ALTER TYPE billingcycle ADD VALUE IF NOT EXISTS 'SEMI_ANNUAL'"))
        connection.execute(sa.text("ALTER TYPE billingcycle ADD VALUE IF NOT EXISTS 'LIFETIME'"))

    groups = connection.execute(
        sa.text(
            """
            SELECT id, key, name
            FROM subscription_groups
            ORDER BY created_at ASC, name ASC
            """
        )
    ).mappings()

    used_keys = {
        str(row["key"])
        for row in groups
        if row["key"] and not str(row["key"]).startswith("legacy-")
    }

    groups = connection.execute(
        sa.text(
            """
            SELECT id, key, name
            FROM subscription_groups
            WHERE key LIKE 'legacy-%'
            ORDER BY created_at ASC, name ASC
            """
        )
    ).mappings()

    for row in groups:
        old_key = str(row["key"])
        new_key = _build_group_key(str(row["name"]), used_keys)
        connection.execute(
            sa.text("UPDATE subscription_groups SET key = :new_key WHERE id = :group_id"),
            {"new_key": new_key, "group_id": row["id"]},
        )
        connection.execute(
            sa.text("UPDATE plans SET group_key = :new_key WHERE group_key = :old_key"),
            {"new_key": new_key, "old_key": old_key},
        )


def downgrade() -> None:
    # Enum value removal is not reversible in PostgreSQL; keep downgrade as no-op.
    pass
