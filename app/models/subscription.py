from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class PlanStatus(str, enum.Enum):
    ON = "ON"
    OFF = "OFF"


class PlanServerAllocationStrategy(str, enum.Enum):
    ALL = "ALL"
    LEAST_LOAD = "LEAST_LOAD"


class BillingCycle(str, enum.Enum):
    TRIAL = "TRIAL"
    UNSET = "UNSET"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMI_ANNUAL = "SEMI_ANNUAL"
    YEARLY = "YEARLY"
    LIFETIME = "LIFETIME"


class SubscriptionGroup(Base):
    __tablename__ = "subscription_groups"
    __table_args__ = (
        UniqueConstraint("key", name="uq_subscription_groups_key"),
        UniqueConstraint("name", name="uq_subscription_groups_name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(String(1000), nullable=True)
    tier_count = Column(Integer, nullable=False, default=3)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SubscriptionGroup {self.name}>"


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("group_key", "tier_level", name="uq_plan_group_tier"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    group_key = Column(String(64), nullable=False, index=True)
    group_name = Column(String(255), nullable=False, index=True)
    tier_level = Column(Integer, nullable=False, default=1)
    description = Column(String(1000), nullable=True)
    duration_days = Column(Integer, nullable=False)
    price = Column(Numeric(18, 2), nullable=False)
    default_billing_cycle = Column(Enum(BillingCycle), default=BillingCycle.UNSET, nullable=False)
    trial_price = Column(Numeric(18, 2), nullable=True)
    trial_days = Column(Integer, nullable=True)
    monthly_price = Column(Numeric(18, 2), nullable=True)
    quarterly_price = Column(Numeric(18, 2), nullable=True)
    semi_annual_price = Column(Numeric(18, 2), nullable=True)
    annual_price = Column(Numeric(18, 2), nullable=True)
    lifetime_price = Column(Numeric(18, 2), nullable=True)
    auto_renew_enabled = Column(Boolean, default=False, nullable=False)
    server_allocation_strategy = Column(
        Enum(PlanServerAllocationStrategy), default=PlanServerAllocationStrategy.ALL, nullable=False
    )
    vod_times = Column(Integer, default=0)
    vod_movie_times = Column(Integer, default=0, nullable=False)
    vod_tv_times = Column(Integer, default=0, nullable=False)
    features = Column(JSON, default={})
    status = Column(Enum(PlanStatus), default=PlanStatus.ON)
    is_visible = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Plan {self.name}>"


class PlanBillingCycle(Base):
    __tablename__ = "plan_billing_cycles"
    __table_args__ = (UniqueConstraint("plan_id", "billing_cycle", name="uq_plan_billing_cycle"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False, index=True)
    billing_cycle = Column(Enum(BillingCycle), nullable=False)
    price = Column(Numeric(18, 2), nullable=False)
    duration_days = Column(Integer, nullable=False, default=0)
    is_default = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<PlanBillingCycle {self.plan_id} {self.billing_cycle}>"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class SubscriptionSource(str, enum.Enum):
    PAYMENT = "PAYMENT"
    CARD = "CARD"
    ADMIN = "ADMIN"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.ACTIVE, index=True)
    billing_cycle = Column(Enum(BillingCycle), default=BillingCycle.UNSET, nullable=False)
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    auto_renew = Column(Boolean, default=False)
    source = Column(Enum(SubscriptionSource), default=SubscriptionSource.PAYMENT)
    vod_times_used = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Subscription {self.user_id}>"


class PlanServerAssignment(Base):
    __tablename__ = "plan_server_assignments"
    __table_args__ = (UniqueConstraint("plan_id", "server_id", name="uq_plan_server"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False, index=True)
    server_id = Column(
        UUID(as_uuid=True), ForeignKey("emby_servers.id"), nullable=False, index=True
    )
    template_emby_user_id = Column(String(255), nullable=False)
    template_emby_username = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PlanServerAssignment {self.plan_id} {self.server_id}>"
