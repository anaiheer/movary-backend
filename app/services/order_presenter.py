from __future__ import annotations

from app.models.order import Order
from app.models.system_settings import SystemSettings
from app.schemas.order import RefundEligibility
from app.services import refunds as refund_service


async def build_refund_eligibility(
    order: Order,
    db,
    settings_row: SystemSettings,
) -> RefundEligibility:
    payload = await refund_service.get_refund_eligibility(order, db, settings_row)
    return RefundEligibility(**payload)
