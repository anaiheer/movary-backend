from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, EmailStr, field_validator

from app.core.passwords import validate_account_password
from app.schemas.order import OrderChainResponse
from app.schemas.username import validate_username


class LicenseInfo(BaseModel):
    edition: str
    status: str
    message: str
    manage_url: str


class AdminLicenseStatus(LicenseInfo):
    activation_mode: str
    provider_mode: str = "local_stub"
    provider_ready: bool = False
    provider_reachable: bool = False
    provider_health_message: Optional[str] = None
    provider_server_url: Optional[str] = None
    provider_key_id: Optional[str] = None
    provider_missing_fields: list[str] = []
    activation_present: bool = False
    activation_code_hint: Optional[str] = None
    activated_at: Optional[str] = None
    last_refresh_at: Optional[str] = None
    expires_at: Optional[str] = None
    instance_id: Optional[str] = None
    instance_label: Optional[str] = None
    license_id: Optional[str] = None
    package_code: Optional[str] = None
    package_name: Optional[str] = None
    pro_effective: bool = False
    artifact_version: Optional[str] = None
    backend_artifact_status: Optional[str] = None
    backend_artifact_error: Optional[str] = None
    frontend_artifact_status: Optional[str] = None
    frontend_artifact_error: Optional[str] = None
    frontend_artifact_entry_url: Optional[str] = None
    frontend_artifact_style_url: Optional[str] = None
    extension_enabled: bool
    extension_loaded: bool
    extension_failed: bool
    loaded_extensions: list[dict] = []
    failed_extensions: list[dict] = []


class AdminLicenseProviderContract(BaseModel):
    provider_mode: str
    provider_ready: bool
    provider_server_url: Optional[str] = None
    provider_key_id: Optional[str] = None
    required_env: list[str] = []
    missing_env: list[str] = []
    remote_endpoints: list[dict] = []
    activation_flow: list[str] = []


class KpiStats(BaseModel):
    total_users: int
    active_users: int
    total_revenue: str
    active_subscriptions: int
    total_vod_requests: int
    expiring_soon: int
    expiring_days: int


class ChartPoint(BaseModel):
    period: str
    value: str


class ChartPointInt(BaseModel):
    period: str
    value: int


class DashboardCharts(BaseModel):
    user_growth: list[ChartPointInt]
    revenue_trend: list[ChartPoint]


class RecentUser(BaseModel):
    id: UUID
    username: str
    email: Optional[EmailStr] = None
    inviter: Optional[dict[str, str]] = None
    created_at: datetime


class RecentOrderUser(BaseModel):
    id: UUID
    username: str


class RecentOrderPlan(BaseModel):
    id: UUID
    name: str


class RecentOrder(BaseModel):
    id: UUID
    user: RecentOrderUser
    plan: Optional[RecentOrderPlan] = None
    amount: str
    status: str
    created_at: datetime


class DashboardOverview(BaseModel):
    license: LicenseInfo
    kpi: KpiStats
    charts: DashboardCharts
    recent_users: list[RecentUser]
    recent_orders: list[RecentOrder]


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int


class PlanSummary(BaseModel):
    id: UUID
    name: str


class SubscriptionSummary(BaseModel):
    status: str
    plan: Optional[PlanSummary] = None
    billing_cycle: str
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class SubscriptionSummaryItem(BaseModel):
    id: UUID
    plan_id: UUID
    plan_name: Optional[str] = None
    group_key: Optional[str] = None
    group_name: Optional[str] = None
    tier_level: Optional[int] = None
    status: str
    billing_cycle: str
    start_at: datetime
    end_at: datetime
    is_current: bool
    is_trial: bool


class UserSubscriptionSummary(BaseModel):
    status: str
    active_count: int
    group_count: int
    future_count: int
    latest_end_at: Optional[datetime] = None
    items: list[SubscriptionSummaryItem]


class ServerAssignmentSummary(BaseModel):
    total: int
    online: int


class ServerAssignmentItem(BaseModel):
    server_id: UUID
    name: str
    base_url: str
    is_online: bool
    assigned_at: datetime


class InviterSummary(BaseModel):
    id: UUID
    username: str


class AdminTelegramBindingSummary(BaseModel):
    telegram_user_id: str
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None
    telegram_last_name: Optional[str] = None
    telegram_language_code: Optional[str] = None
    is_active: bool
    bound_at: datetime
    last_interaction_at: Optional[datetime] = None


class AdminUserListItem(BaseModel):
    id: UUID
    username: str
    email: Optional[EmailStr] = None
    avatar_url: Optional[str] = None
    role: str
    status: str
    balance: str
    expire_remind: bool
    trial_used: bool
    vod_movie_limit: int
    vod_tv_limit: int
    vod_movie_used: int
    vod_tv_used: int
    emby_password: Optional[str] = None
    telegram_binding: Optional[AdminTelegramBindingSummary] = None
    inviter: Optional[InviterSummary] = None
    subscription: SubscriptionSummary
    subscription_summary: UserSubscriptionSummary
    server_assignment_summary: ServerAssignmentSummary
    server_assignments: list[ServerAssignmentItem]
    created_at: datetime


class AdminUserStats(BaseModel):
    total_users: int
    active_users: int
    expired_users: int
    disabled_users: int


class AdminUsersResponse(BaseModel):
    items: list[AdminUserListItem]
    pagination: Pagination
    stats: AdminUserStats


class AdminOrderUserSummary(BaseModel):
    id: UUID
    username: str
    email: Optional[EmailStr] = None


class AdminOrderPlanSummary(BaseModel):
    id: UUID
    name: str
    duration_days: int


class AdminOrderListItem(BaseModel):
    id: UUID
    order_chain_id: UUID
    root_order_id: UUID
    parent_order_id: Optional[UUID] = None
    superseded_by_order_id: Optional[UUID] = None
    order_no: str
    user: AdminOrderUserSummary
    plan: Optional[AdminOrderPlanSummary] = None
    amount: str
    status: str
    type: str
    settlement_status: Optional[str] = None
    created_at: datetime
    paid_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    refund_status: Optional[str] = None
    refund_status_label: Optional[str] = None
    refund_requested_at: Optional[datetime] = None
    refund_reviewed_at: Optional[datetime] = None
    refund_reviewed_by: Optional[UUID] = None
    refund_reject_reason: Optional[str] = None
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


class AdminOrderStats(BaseModel):
    total_orders: int
    successful_orders: int
    pending_orders: int
    refund_orders: int


class AdminOrdersResponse(BaseModel):
    items: list[AdminOrderListItem]
    pagination: Pagination
    stats: AdminOrderStats


class AdminOrderDetail(AdminOrderListItem):
    currency: str
    pay_provider: Optional[str] = None
    pay_payload: Optional[dict] = None
    order_chain: Optional[OrderChainResponse] = None


class AdminUserCreate(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    password: str
    role: str = "USER"
    balance: Decimal = Decimal("0.00")
    expire_remind: bool = True
    vod_movie_limit: int = 0
    vod_tv_limit: int = 0

    @field_validator("username")
    @classmethod
    def validate_input_username(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("password")
    @classmethod
    def validate_input_password(cls, value: str) -> str:
        return validate_account_password(value)


class AdminUserManagedSubscription(BaseModel):
    plan_id: UUID
    billing_cycle: str
    start_at: datetime
    end_at: datetime


class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    status: Optional[str] = None
    expire_remind: Optional[bool] = None
    trial_used: Optional[bool] = None
    vod_movie_limit: Optional[int] = None
    vod_tv_limit: Optional[int] = None
    vod_movie_used: Optional[int] = None
    vod_tv_used: Optional[int] = None
    reset_password: Optional[bool] = False
    new_password: Optional[str] = None
    balance_set: Optional[Decimal] = None
    subscription: Optional[dict] = None
    subscriptions: Optional[list[AdminUserManagedSubscription]] = None
    emby_password: Optional[str] = None

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return validate_account_password(value)


class EmbyImportUser(BaseModel):
    emby_user_id: str
    username: str
    email: Optional[EmailStr] = None
    account_status: str
    user_status: Optional[str] = None


class EmbyImportRequest(BaseModel):
    server_id: UUID
    users: list[EmbyImportUser]


class EmbyImportResult(BaseModel):
    imported: int
    updated: int
    skipped: int
    errors: list[str] = []


class BalanceAdjustRequest(BaseModel):
    delta: Decimal
    reason: str = "ADMIN_ADJUST"


class BulkAssignSubscriptionRequest(BaseModel):
    user_ids: list[UUID]
    plan_id: UUID
    billing_cycle: str
    start_at: datetime
    end_at: datetime
    mode: str = "SKIP"
    emby_password: Optional[str] = None


class BulkAssignSubscriptionResult(BaseModel):
    assigned: int
    skipped: int
    conflicts: list[dict] = []


class BulkExtendSubscriptionRequest(BaseModel):
    user_ids: list[UUID]
    extend_days: int


class BulkExtendSubscriptionResult(BaseModel):
    extended: int
    skipped: int


class StatusUpdateRequest(BaseModel):
    status: str


class InvitationCreate(BaseModel):
    invitee_email: EmailStr
    plan_id: Optional[UUID] = None
    initial_balance: Optional[Decimal] = None


class InvitationResponse(BaseModel):
    id: UUID
    invitee_email: EmailStr
    token: str
    expires_at: datetime
    invite_url: str


class InvitationListItem(BaseModel):
    id: UUID
    invitee_email: EmailStr
    token: str
    invite_url: str
    inviter: dict
    plan: Optional[dict] = None
    initial_balance: Optional[Decimal] = None
    status: str
    expires_at: datetime
    created_at: datetime


class InvitationListResponse(BaseModel):
    items: list[InvitationListItem]
    page: int
    page_size: int
    total: int
    accepted_count: int
