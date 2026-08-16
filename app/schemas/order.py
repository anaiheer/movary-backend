from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from typing import Optional
from app.schemas.plan import PlanResponse


class OrderCreate(BaseModel):
    plan_id: UUID
    pay_type: str | None = None
    billing_cycle: str | None = None


class RefundEligibility(BaseModel):
    can_request: bool
    available_methods: list[str]
    reason: Optional[str] = None


class OrderResponse(BaseModel):
    id: UUID
    order_chain_id: UUID
    root_order_id: UUID
    parent_order_id: Optional[UUID] = None
    superseded_by_order_id: Optional[UUID] = None
    order_no: str
    type: str
    plan_id: Optional[UUID] = None
    amount: float
    currency: str
    status: str
    settlement_status: Optional[str] = None
    user_id: UUID
    paid_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    refund_status: Optional[str] = None
    refund_requested_at: Optional[datetime] = None
    refund_reviewed_at: Optional[datetime] = None
    refund_reviewed_by: Optional[UUID] = None
    refund_reject_reason: Optional[str] = None
    created_at: datetime
    purchase_action: Optional[str] = None
    purchase_action_label: Optional[str] = None
    billing_cycle: Optional[str] = None
    duration_days: Optional[int] = None
    base_amount: Optional[float] = None
    credit_amount: Optional[float] = None
    payable_amount: Optional[float] = None
    carry_balance_amount: Optional[float] = None
    refund_amount: Optional[float] = None
    refund_to: Optional[str] = None
    refund_eligibility: Optional[RefundEligibility] = None

    class Config:
        from_attributes = True


class OrderPayRequest(BaseModel):
    pay_type: str | None = None


class OrderChainEntryResponse(BaseModel):
    id: UUID
    order_no: str
    type: str
    order_chain_id: UUID
    root_order_id: UUID
    parent_order_id: Optional[UUID] = None
    superseded_by_order_id: Optional[UUID] = None
    status: str
    settlement_status: Optional[str] = None
    refund_status: Optional[str] = None
    amount: float
    created_at: datetime
    paid_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    is_current: bool = False
    purchase_action: Optional[str] = None
    purchase_action_label: Optional[str] = None
    billing_cycle: Optional[str] = None
    duration_days: Optional[int] = None
    base_amount: Optional[float] = None
    credit_amount: Optional[float] = None
    payable_amount: Optional[float] = None
    carry_balance_amount: Optional[float] = None
    refund_amount: Optional[float] = None
    refund_to: Optional[str] = None


class OrderChainValueLinkResponse(BaseModel):
    id: UUID
    source_order_id: UUID
    target_order_id: UUID
    relation_type: str
    consumed_amount: Optional[float] = None
    consumed_days: Optional[int] = None
    created_at: datetime


class OrderChainResponse(BaseModel):
    chain_id: UUID
    root_order_id: UUID
    current_order_id: UUID
    orders: list[OrderChainEntryResponse]
    value_links: list[OrderChainValueLinkResponse]


class OrderDetailResponse(BaseModel):
    order: OrderResponse
    plan: Optional[PlanResponse] = None
    pay_provider: Optional[str] = None
    pay_payload: Optional[dict] = None
    order_chain: Optional[OrderChainResponse] = None
