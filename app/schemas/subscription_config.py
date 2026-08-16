from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class SubscriptionPlanUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)
    monthly_price: float = Field(ge=0)
    yearly_price: Optional[float] = Field(default=None, ge=0)
    vod_movie_times: int = Field(default=0, ge=0, le=999999)
    vod_tv_times: int = Field(default=0, ge=0, le=999999)
    is_visible: bool = True


class SubscriptionPlanOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    monthly_price: float
    yearly_price: Optional[float] = None
    vod_movie_times: int
    vod_tv_times: int
    is_visible: bool
    created_at: datetime


class SubscriptionConfigSummary(BaseModel):
    max_plan_count: int = 1
    plan_count: int
    visible_plan_count: int
    group_count: int
    extra_plan_count: int = 0
    locked: bool = False
    pro_data_detected: bool = False
    pro_subscription_extension_loaded: bool = False
    pro_subscription_admin_path: Optional[str] = None
    notices: list[str] = Field(default_factory=list)


class SubscriptionConfigData(BaseModel):
    plan: Optional[SubscriptionPlanOut] = None
    summary: SubscriptionConfigSummary


class UserPlanItem(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    monthly_price: float
    yearly_price: Optional[float] = None
    vod_movie_times: int
    vod_tv_times: int
    is_visible: bool


class UserPlanPreview(BaseModel):
    allowed: bool
    action: str
    message: str
    billing_cycle: str
    duration_days: int
    price: float
    button_label: str
    plan: UserPlanItem


class UserOrderCreate(BaseModel):
    plan_id: UUID
    billing_cycle: str = Field(pattern="^(MONTHLY|YEARLY)$")
