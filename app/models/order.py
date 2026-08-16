from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
import enum
from app.db.session import Base


class OrderType(str, enum.Enum):
    PLAN = "PLAN"
    VOD = "VOD"
    RECHARGE = "RECHARGE"


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    PAID = "PAID"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"
    TIMEOUT = "TIMEOUT"
    REFUNDED = "REFUNDED"


class RefundStatus(str, enum.Enum):
    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PROCESSING = "PROCESSING"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"


class OrderSettlementStatus(str, enum.Enum):
    OPEN = "OPEN"
    CONSUMED = "CONSUMED"
    COVERED_BY_DESCENDANT_REFUND = "COVERED_BY_DESCENDANT_REFUND"
    REFUNDED = "REFUNDED"


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=True, index=True)
    order_chain_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    root_order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    parent_order_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    superseded_by_order_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    order_no = Column(String(255), unique=True, nullable=False, index=True)
    type = Column(Enum(OrderType), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="CNY")
    status = Column(Enum(OrderStatus), default=OrderStatus.CREATED, index=True)
    pay_provider = Column(String(50), nullable=True)  # STRIPE, ALIPAY, etc
    pay_payload = Column(JSON, nullable=True)  # 下单参数快照
    paid_at = Column(DateTime, nullable=True)
    refunded_at = Column(DateTime, nullable=True)
    refund_status = Column(
        Enum(RefundStatus), default=RefundStatus.NONE, nullable=False, index=True
    )
    refund_requested_at = Column(DateTime, nullable=True)
    refund_reviewed_at = Column(DateTime, nullable=True)
    refund_reviewed_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    refund_reject_reason = Column(String(1000), nullable=True)
    settlement_status = Column(
        Enum(OrderSettlementStatus),
        default=OrderSettlementStatus.OPEN,
        nullable=False,
        index=True,
    )
    subscription_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Order {self.order_no}>"


@event.listens_for(Order, "before_insert")
def _ensure_order_chain_defaults(mapper, connection, target):  # noqa: ARG001
    if not target.id:
        target.id = uuid.uuid4()
    if not target.order_chain_id:
        target.order_chain_id = target.id
    if not target.root_order_id:
        target.root_order_id = target.id
    if not target.settlement_status:
        target.settlement_status = OrderSettlementStatus.OPEN


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True)
    provider_trade_no = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False)
    raw_callback = Column(JSON, nullable=True)  # 原始回调信息
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PaymentTransaction {self.provider_trade_no}>"


class OrderValueLink(Base):
    __tablename__ = "order_value_links"
    __table_args__ = (
        UniqueConstraint("source_order_id", "target_order_id", name="uq_order_value_link_pair"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_chain_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    source_order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    target_order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    relation_type = Column(String(50), nullable=False)
    consumed_amount = Column(Numeric(18, 2), nullable=True)
    consumed_days = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<OrderValueLink {self.source_order_id} -> {self.target_order_id}>"
