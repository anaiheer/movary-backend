from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.models.order import Order, OrderType


MONEY_QUANT = Decimal("0.01")


ACTION_LABELS = {
    "DIRECT_PURCHASE": "新购订阅",
    "RENEW": "续费订阅",
    "UPGRADE": "升级订阅",
    "REPLACE_TRIAL": "试用转正式",
    "RECHARGE": "余额充值",
    "VOD": "点播下单",
}


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP))


def _action_for_order(order: Order) -> str:
    payload = order.pay_payload or {}
    if order.type == OrderType.RECHARGE:
        return "RECHARGE"
    if order.type == OrderType.VOD:
        return "VOD"
    return str(payload.get("purchase_action") or "DIRECT_PURCHASE").upper()


def _action_label_for_order(order: Order, action: str) -> str:
    payload = order.pay_payload or {}
    target_cycle = str(payload.get("billing_cycle") or "").upper()
    source_cycle = str(payload.get("source_billing_cycle") or "").upper()

    if action == "REPLACE_TRIAL" and target_cycle == "LIFETIME":
        return "试用转永久"

    if (
        action == "UPGRADE"
        and target_cycle == "LIFETIME"
        and source_cycle
        and source_cycle != "LIFETIME"
    ):
        return "周期转永久"

    return ACTION_LABELS.get(action, action)


def build_order_summary(order: Order) -> dict:
    payload = order.pay_payload or {}
    refund = payload.get("refund") if isinstance(payload.get("refund"), dict) else {}
    action = _action_for_order(order)

    base_amount = _money(payload.get("base_amount") if "base_amount" in payload else order.amount)
    credit_amount = _money(payload.get("credit_amount"))
    payable_amount = _money(
        payload.get("payable_amount") if "payable_amount" in payload else order.amount
    )
    carry_balance_amount = _money(payload.get("carry_balance_amount"))
    refund_amount = _money(refund.get("money")) if refund else None

    return {
        "purchase_action": action,
        "purchase_action_label": _action_label_for_order(order, action),
        "billing_cycle": payload.get("billing_cycle"),
        "duration_days": int(payload.get("duration_days") or 0),
        "base_amount": base_amount,
        "credit_amount": credit_amount,
        "payable_amount": payable_amount,
        "carry_balance_amount": carry_balance_amount,
        "refund_amount": refund_amount,
        "refund_to": refund.get("refund_to") if refund else None,
    }
