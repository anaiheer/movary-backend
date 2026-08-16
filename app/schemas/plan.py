from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict


class PlanBillingCycleBase(BaseModel):
    billing_cycle: str
    price: float
    duration_days: int = 0
    is_default: bool = False
    sort_order: int = 0


class PlanBillingCycleIn(PlanBillingCycleBase):
    pass


class PlanBillingCycleOut(PlanBillingCycleBase):
    id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionGroupBase(BaseModel):
    key: str
    name: str
    description: Optional[str] = None
    tier_count: int = Field(default=3, ge=1, le=12)


class SubscriptionGroupCreate(SubscriptionGroupBase):
    pass


class SubscriptionGroupUpdate(BaseModel):
    key: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    tier_count: Optional[int] = Field(default=None, ge=1, le=12)


class SubscriptionGroupOut(SubscriptionGroupBase):
    id: UUID
    plan_count: int = 0
    used_tiers: List[int] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PlanBase(BaseModel):
    name: str
    group_key: str
    group_name: str
    tier_level: int = 1
    description: Optional[str] = None
    duration_days: int
    price: float
    default_billing_cycle: str = "MONTHLY"
    vod_times: int = 0
    vod_movie_times: int = 0
    vod_tv_times: int = 0
    features: Optional[Dict[str, Any]] = {}
    trial_price: Optional[float] = None
    trial_days: Optional[int] = None
    monthly_price: Optional[float] = None
    quarterly_price: Optional[float] = None
    semi_annual_price: Optional[float] = None
    annual_price: Optional[float] = None
    lifetime_price: Optional[float] = None
    auto_renew_enabled: bool = False
    server_allocation_strategy: Optional[str] = None
    is_visible: bool = True


class PlanCreate(PlanBase):
    pass


class PlanResponse(PlanBase):
    id: UUID
    status: str
    created_at: datetime
    updated_at: datetime
    cycles: List[PlanBillingCycleOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class SubscriptionResponse(BaseModel):
    id: UUID
    user_id: UUID
    plan_id: UUID
    plan_name: Optional[str] = None
    group_key: Optional[str] = None
    group_name: Optional[str] = None
    tier_level: Optional[int] = None
    status: str
    billing_cycle: str
    start_at: datetime
    end_at: datetime
    vod_times_used: int

    model_config = ConfigDict(from_attributes=True)


class PlanServerAssignmentIn(BaseModel):
    server_id: UUID
    template_emby_user_id: str
    template_emby_username: str


class PlanServerAssignmentOut(PlanServerAssignmentIn):
    server_name: Optional[str] = None


class AdminPlanCreate(BaseModel):
    name: str
    group_id: UUID
    tier_level: int = 1
    description: Optional[str] = None
    price: Optional[float] = None
    default_billing_cycle: str = "MONTHLY"
    duration_days: Optional[int] = None
    vod_times: int = 0
    vod_movie_times: int = 0
    vod_tv_times: int = 0
    features: Optional[Dict[str, Any]] = None
    trial_price: Optional[float] = None
    trial_days: Optional[int] = None
    monthly_price: Optional[float] = None
    quarterly_price: Optional[float] = None
    semi_annual_price: Optional[float] = None
    annual_price: Optional[float] = None
    lifetime_price: Optional[float] = None
    auto_renew_enabled: bool = False
    server_allocation_strategy: str = "ALL"
    is_visible: bool = True
    status: str = "ON"
    cycles: List[PlanBillingCycleIn] = Field(default_factory=list)
    server_assignments: List[PlanServerAssignmentIn] = Field(default_factory=list)


class AdminPlanUpdate(BaseModel):
    name: Optional[str] = None
    group_id: Optional[UUID] = None
    tier_level: Optional[int] = None
    description: Optional[str] = None
    price: Optional[float] = None
    default_billing_cycle: Optional[str] = None
    duration_days: Optional[int] = None
    vod_times: Optional[int] = None
    vod_movie_times: Optional[int] = None
    vod_tv_times: Optional[int] = None
    features: Optional[Dict[str, Any]] = None
    trial_price: Optional[float] = None
    trial_days: Optional[int] = None
    monthly_price: Optional[float] = None
    quarterly_price: Optional[float] = None
    semi_annual_price: Optional[float] = None
    annual_price: Optional[float] = None
    lifetime_price: Optional[float] = None
    auto_renew_enabled: Optional[bool] = None
    server_allocation_strategy: Optional[str] = None
    is_visible: Optional[bool] = None
    status: Optional[str] = None
    cycles: Optional[List[PlanBillingCycleIn]] = None
    server_assignments: Optional[List[PlanServerAssignmentIn]] = None


class AdminPlanOut(BaseModel):
    id: UUID
    name: str
    group_id: Optional[UUID] = None
    group_key: str
    group_name: str
    tier_level: int
    description: Optional[str] = None
    duration_days: int
    price: float
    default_billing_cycle: str
    vod_times: int
    vod_movie_times: int
    vod_tv_times: int
    features: Optional[Dict[str, Any]] = None
    trial_price: Optional[float] = None
    trial_days: Optional[int] = None
    monthly_price: Optional[float] = None
    quarterly_price: Optional[float] = None
    semi_annual_price: Optional[float] = None
    annual_price: Optional[float] = None
    lifetime_price: Optional[float] = None
    auto_renew_enabled: bool
    server_allocation_strategy: str
    is_visible: bool
    status: str
    created_at: datetime
    cycles: List[PlanBillingCycleOut] = Field(default_factory=list)
    server_assignments: List[PlanServerAssignmentOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class PurchasePreviewSubscription(BaseModel):
    id: UUID
    plan_id: UUID
    plan_name: str
    group_name: str
    tier_level: int
    billing_cycle: str
    start_at: datetime
    end_at: datetime


class PlanPurchasePreview(BaseModel):
    allowed: bool
    action: str
    message: str
    billing_cycle: str
    duration_days: int
    base_price: float
    credit_amount: float
    payable_amount: float
    carry_balance_amount: float = 0
    button_label: str
    target_plan: dict
    current_subscription: Optional[PurchasePreviewSubscription] = None
