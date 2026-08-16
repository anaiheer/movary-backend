from pydantic import BaseModel, EmailStr, field_validator, ConfigDict
from typing import Optional
from datetime import datetime
from uuid import UUID

from app.core.passwords import validate_account_password
from app.models.user import UserRole, UserStatus
from app.schemas.username import normalize_login_identifier, validate_username


class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    username: str
    phone: Optional[str] = None
    avatar_url: Optional[str] = None


class UserCreate(UserBase):
    password: str
    invite_token: Optional[str] = None

    @field_validator("username")
    @classmethod
    def validate_input_username(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("password")
    @classmethod
    def validate_input_password(cls, value: str) -> str:
        return validate_account_password(value)


class AdminUserCreate(UserCreate):
    status: UserStatus = UserStatus.ACTIVE
    role: UserRole = UserRole.USER


class BulkUserCreate(BaseModel):
    email: Optional[EmailStr] = None
    username: str
    password: str
    status: UserStatus = UserStatus.ACTIVE
    role: UserRole = UserRole.USER
    phone: Optional[str] = None

    @field_validator("username")
    @classmethod
    def validate_input_username(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("password")
    @classmethod
    def validate_input_password(cls, value: str) -> str:
        return validate_account_password(value)


class UserResponse(UserBase):
    id: UUID
    status: str
    role: str
    balance: float
    email_verified: bool
    email_verified_at: Optional[datetime] = None
    created_at: datetime
    last_login_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class UserLogin(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def normalize_login_username(cls, value: str) -> str:
        return normalize_login_identifier(value)


class UserSelfUpdate(BaseModel):
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    password: Optional[str] = None
    avatar_url: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_input_password(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return validate_account_password(value)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class EmailVerificationRequest(BaseModel):
    email: EmailStr


class UserListItem(UserResponse):
    expired_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class UserStats(BaseModel):
    total: int
    active: int
    banned: int
    admins: int


class UserTrendPoint(BaseModel):
    label: str
    count: int


class UserListResponse(BaseModel):
    items: list[UserListItem]
    stats: UserStats
    trend: list[UserTrendPoint]


class UserStatusUpdate(BaseModel):
    status: UserStatus
    role: Optional[UserRole] = None


class BulkImportRequest(BaseModel):
    users: list[BulkUserCreate]


class BulkImportResult(BaseModel):
    created: int
    skipped: int
    items: list[UserListItem]
