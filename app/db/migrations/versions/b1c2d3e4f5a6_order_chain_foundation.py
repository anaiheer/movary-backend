"""add order chain foundation

Revision ID: b1c2d3e4f5a6
Revises: 7a1d3c5e9b2f
Create Date: 2026-03-25 02:20:00.000000
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "7a1d3c5e9b2f"
branch_labels = None
depends_on = None


ORDER_SETTLEMENT_STATUS = postgresql.ENUM(
    "OPEN",
    "CONSUMED",
    "COVERED_BY_DESCENDANT_REFUND",
    "REFUNDED",
    name="ordersettlementstatus",
)
CHAIN_SUCCESS_STATUSES = {"PAID", "COMPLETED", "REFUNDED"}
VALUE_CONSUMING_ACTIONS = {"UPGRADE", "REPLACE_TRIAL"}


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _parse_uuid(raw_value):
    if not raw_value:
        return None
    try:
        return uuid.UUID(str(raw_value))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_uuid_list(raw_values) -> list[uuid.UUID]:
    values = raw_values or []
    parsed: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in values:
        value = _parse_uuid(raw)
        if value and value not in seen:
            seen.add(value)
            parsed.append(value)
    return parsed


def _extract_source_subscription_ids(payload: dict | None) -> list[uuid.UUID]:
    payload = payload or {}
    raw_source_ids = payload.get("source_subscription_ids") or []
    if not raw_source_ids and payload.get("source_subscription_id"):
        raw_source_ids = [payload.get("source_subscription_id")]
    return _parse_uuid_list(raw_source_ids)


def _sort_key(row) -> tuple[datetime, datetime, str]:
    paid_at = row["paid_at"] or datetime.min
    created_at = row["created_at"] or datetime.min
    return (paid_at, created_at, str(row["id"]))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ORDER_SETTLEMENT_STATUS.create(bind, checkfirst=True)

    if not _has_column(inspector, "orders", "order_chain_id"):
        op.add_column("orders", sa.Column("order_chain_id", sa.UUID(), nullable=True))
    if not _has_column(inspector, "orders", "root_order_id"):
        op.add_column("orders", sa.Column("root_order_id", sa.UUID(), nullable=True))
    if not _has_column(inspector, "orders", "parent_order_id"):
        op.add_column("orders", sa.Column("parent_order_id", sa.UUID(), nullable=True))
    if not _has_column(inspector, "orders", "superseded_by_order_id"):
        op.add_column("orders", sa.Column("superseded_by_order_id", sa.UUID(), nullable=True))
    if not _has_column(inspector, "orders", "settlement_status"):
        op.add_column(
            "orders",
            sa.Column(
                "settlement_status",
                ORDER_SETTLEMENT_STATUS,
                nullable=True,
                server_default="OPEN",
            ),
        )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "orders", op.f("ix_orders_order_chain_id")):
        op.create_index(
            op.f("ix_orders_order_chain_id"), "orders", ["order_chain_id"], unique=False
        )
    if not _has_index(inspector, "orders", op.f("ix_orders_root_order_id")):
        op.create_index(op.f("ix_orders_root_order_id"), "orders", ["root_order_id"], unique=False)
    if not _has_index(inspector, "orders", op.f("ix_orders_parent_order_id")):
        op.create_index(
            op.f("ix_orders_parent_order_id"), "orders", ["parent_order_id"], unique=False
        )
    if not _has_index(inspector, "orders", op.f("ix_orders_superseded_by_order_id")):
        op.create_index(
            op.f("ix_orders_superseded_by_order_id"),
            "orders",
            ["superseded_by_order_id"],
            unique=False,
        )
    if not _has_index(inspector, "orders", op.f("ix_orders_settlement_status")):
        op.create_index(
            op.f("ix_orders_settlement_status"), "orders", ["settlement_status"], unique=False
        )

    if not _has_table(inspector, "order_value_links"):
        op.create_table(
            "order_value_links",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("order_chain_id", sa.UUID(), nullable=False),
            sa.Column("source_order_id", sa.UUID(), nullable=False),
            sa.Column("target_order_id", sa.UUID(), nullable=False),
            sa.Column("relation_type", sa.String(length=50), nullable=False),
            sa.Column("consumed_amount", sa.Numeric(precision=18, scale=2), nullable=True),
            sa.Column("consumed_days", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["source_order_id"], ["orders.id"]),
            sa.ForeignKeyConstraint(["target_order_id"], ["orders.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_order_id", "target_order_id", name="uq_order_value_link_pair"
            ),
        )
        op.create_index(
            op.f("ix_order_value_links_order_chain_id"),
            "order_value_links",
            ["order_chain_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_order_value_links_source_order_id"),
            "order_value_links",
            ["source_order_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_order_value_links_target_order_id"),
            "order_value_links",
            ["target_order_id"],
            unique=False,
        )

    orders_table = sa.table(
        "orders",
        sa.column("id", sa.UUID()),
        sa.column("user_id", sa.UUID()),
        sa.column("type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("refund_status", sa.String()),
        sa.column("paid_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
        sa.column("pay_payload", sa.JSON()),
        sa.column("subscription_id", sa.UUID()),
        sa.column("order_chain_id", sa.UUID()),
        sa.column("root_order_id", sa.UUID()),
        sa.column("parent_order_id", sa.UUID()),
        sa.column("superseded_by_order_id", sa.UUID()),
        sa.column("settlement_status", ORDER_SETTLEMENT_STATUS),
    )
    value_links_table = sa.table(
        "order_value_links",
        sa.column("id", sa.UUID()),
        sa.column("order_chain_id", sa.UUID()),
        sa.column("source_order_id", sa.UUID()),
        sa.column("target_order_id", sa.UUID()),
        sa.column("relation_type", sa.String()),
        sa.column("consumed_amount", sa.Numeric(18, 2)),
        sa.column("consumed_days", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )

    rows = (
        bind.execute(
            sa.select(
                orders_table.c.id,
                orders_table.c.user_id,
                orders_table.c.type,
                orders_table.c.status,
                orders_table.c.refund_status,
                orders_table.c.paid_at,
                orders_table.c.created_at,
                orders_table.c.pay_payload,
                orders_table.c.subscription_id,
            ).order_by(orders_table.c.created_at.asc(), orders_table.c.id.asc())
        )
        .mappings()
        .all()
    )
    if not rows:
        op.alter_column("orders", "order_chain_id", nullable=False)
        op.alter_column("orders", "root_order_id", nullable=False)
        op.alter_column("orders", "settlement_status", nullable=False, server_default=None)
        return

    row_by_id = {row["id"]: row for row in rows}
    row_index = {row["id"]: index for index, row in enumerate(rows)}
    chain_by_order: dict[uuid.UUID, uuid.UUID] = {}
    root_by_order: dict[uuid.UUID, uuid.UUID] = {}
    latest_successful_order_by_subscription: dict[uuid.UUID, uuid.UUID] = {}
    successful_history_by_subscription: dict[uuid.UUID, list[uuid.UUID]] = {}

    for row in rows:
        order_id = row["id"]
        if not order_id:
            continue

        payload = row["pay_payload"] or {}
        purchase_action = str(payload.get("purchase_action") or "").upper()
        parent_id = None
        if str(row["type"]) == "PLAN":
            source_subscription_ids: list[uuid.UUID] = []
            if purchase_action == "RENEW":
                renewal_anchor = _parse_uuid(
                    payload.get("renewal_of_subscription_id")
                    or payload.get("source_subscription_id")
                )
                if renewal_anchor:
                    source_subscription_ids = [renewal_anchor]
            elif purchase_action in VALUE_CONSUMING_ACTIONS:
                source_subscription_ids = _extract_source_subscription_ids(payload)
                current_source = _parse_uuid(payload.get("source_subscription_id"))
                if current_source and current_source not in source_subscription_ids:
                    source_subscription_ids.append(current_source)

            parent_candidates = [
                latest_successful_order_by_subscription[source_id]
                for source_id in source_subscription_ids
                if source_id in latest_successful_order_by_subscription
            ]
            if parent_candidates:
                parent_id = max(
                    parent_candidates,
                    key=lambda candidate_id: _sort_key(row_by_id[candidate_id]),
                )

        if parent_id:
            chain_id = chain_by_order.get(parent_id, parent_id)
            root_id = root_by_order.get(parent_id, parent_id)
        else:
            chain_id = order_id
            root_id = order_id

        refunded = (
            str(row["status"] or "") == "REFUNDED" or str(row["refund_status"] or "") == "REFUNDED"
        )
        settlement_status = "REFUNDED" if refunded else "OPEN"
        bind.execute(
            orders_table.update()
            .where(orders_table.c.id == order_id)
            .values(
                order_chain_id=chain_id,
                root_order_id=root_id,
                parent_order_id=parent_id,
                settlement_status=settlement_status,
            )
        )
        chain_by_order[order_id] = chain_id
        root_by_order[order_id] = root_id

        if (
            str(row["type"]) == "PLAN"
            and str(row["status"] or "") in CHAIN_SUCCESS_STATUSES
            and row["subscription_id"]
        ):
            latest_successful_order_by_subscription[row["subscription_id"]] = order_id
            successful_history_by_subscription.setdefault(row["subscription_id"], []).append(
                order_id
            )

    existing_link_pairs = {
        (source_order_id, target_order_id)
        for source_order_id, target_order_id in bind.execute(
            sa.select(value_links_table.c.source_order_id, value_links_table.c.target_order_id)
        ).fetchall()
    }
    source_updates: dict[uuid.UUID, dict[str, object]] = {}
    for row in rows:
        order_id = row["id"]
        if not order_id or str(row["type"]) != "PLAN":
            continue

        payload = row["pay_payload"] or {}
        purchase_action = str(payload.get("purchase_action") or "").upper()
        if purchase_action not in VALUE_CONSUMING_ACTIONS:
            continue
        if str(row["status"] or "") not in CHAIN_SUCCESS_STATUSES:
            continue

        source_order_ids: list[uuid.UUID] = []
        for source_subscription_id in _extract_source_subscription_ids(payload):
            history = successful_history_by_subscription.get(source_subscription_id) or []
            eligible = [
                candidate_id
                for candidate_id in history
                if row_index[candidate_id] < row_index[order_id]
            ]
            if eligible:
                source_order_ids.append(eligible[-1])

        if not source_order_ids:
            continue

        target_cycle = str(payload.get("billing_cycle") or "").upper()
        source_cycles = {
            str(
                (row_by_id[source_order_id]["pay_payload"] or {}).get("billing_cycle") or ""
            ).upper()
            for source_order_id in source_order_ids
        }
        if purchase_action == "REPLACE_TRIAL":
            relation_type = "REPLACE_TRIAL"
        elif purchase_action == "UPGRADE" and target_cycle == "LIFETIME":
            relation_type = (
                "UPGRADE_LIFETIME_TIER"
                if source_cycles
                and all(source_cycle == "LIFETIME" for source_cycle in source_cycles)
                else "UPGRADE_TO_LIFETIME"
            )
        else:
            relation_type = purchase_action

        credit_amount = payload.get("credit_amount")
        consumed_amount = None
        if len(source_order_ids) == 1 and credit_amount is not None:
            consumed_amount = Decimal(str(credit_amount))

        for source_order_id in source_order_ids:
            source_row = row_by_id[source_order_id]
            source_updates[source_order_id] = {
                "superseded_by_order_id": order_id,
                "settlement_status": (
                    "REFUNDED"
                    if str(source_row["status"] or "") == "REFUNDED"
                    or str(source_row["refund_status"] or "") == "REFUNDED"
                    else "CONSUMED"
                ),
            }
            link_pair = (source_order_id, order_id)
            if link_pair in existing_link_pairs:
                continue
            bind.execute(
                value_links_table.insert().values(
                    id=uuid.uuid4(),
                    order_chain_id=chain_by_order.get(order_id, order_id),
                    source_order_id=source_order_id,
                    target_order_id=order_id,
                    relation_type=relation_type,
                    consumed_amount=consumed_amount,
                    consumed_days=None,
                    created_at=row["created_at"] or datetime.utcnow(),
                )
            )
            existing_link_pairs.add(link_pair)

    for source_order_id, values in source_updates.items():
        bind.execute(
            orders_table.update().where(orders_table.c.id == source_order_id).values(**values)
        )

    op.alter_column("orders", "order_chain_id", nullable=False)
    op.alter_column("orders", "root_order_id", nullable=False)
    op.alter_column("orders", "settlement_status", nullable=False, server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "order_value_links"):
        if _has_index(inspector, "order_value_links", op.f("ix_order_value_links_target_order_id")):
            op.drop_index(
                op.f("ix_order_value_links_target_order_id"), table_name="order_value_links"
            )
        if _has_index(inspector, "order_value_links", op.f("ix_order_value_links_source_order_id")):
            op.drop_index(
                op.f("ix_order_value_links_source_order_id"), table_name="order_value_links"
            )
        if _has_index(inspector, "order_value_links", op.f("ix_order_value_links_order_chain_id")):
            op.drop_index(
                op.f("ix_order_value_links_order_chain_id"), table_name="order_value_links"
            )
        op.drop_table("order_value_links")

    inspector = sa.inspect(bind)
    if _has_index(inspector, "orders", op.f("ix_orders_settlement_status")):
        op.drop_index(op.f("ix_orders_settlement_status"), table_name="orders")
    if _has_index(inspector, "orders", op.f("ix_orders_superseded_by_order_id")):
        op.drop_index(op.f("ix_orders_superseded_by_order_id"), table_name="orders")
    if _has_index(inspector, "orders", op.f("ix_orders_parent_order_id")):
        op.drop_index(op.f("ix_orders_parent_order_id"), table_name="orders")
    if _has_index(inspector, "orders", op.f("ix_orders_root_order_id")):
        op.drop_index(op.f("ix_orders_root_order_id"), table_name="orders")
    if _has_index(inspector, "orders", op.f("ix_orders_order_chain_id")):
        op.drop_index(op.f("ix_orders_order_chain_id"), table_name="orders")

    if _has_column(inspector, "orders", "settlement_status"):
        op.drop_column("orders", "settlement_status")
    if _has_column(inspector, "orders", "superseded_by_order_id"):
        op.drop_column("orders", "superseded_by_order_id")
    if _has_column(inspector, "orders", "parent_order_id"):
        op.drop_column("orders", "parent_order_id")
    if _has_column(inspector, "orders", "root_order_id"):
        op.drop_column("orders", "root_order_id")
    if _has_column(inspector, "orders", "order_chain_id"):
        op.drop_column("orders", "order_chain_id")

    ORDER_SETTLEMENT_STATUS.drop(bind, checkfirst=True)
