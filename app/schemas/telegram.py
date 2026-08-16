from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.schemas.username import normalize_login_identifier


def _strip_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class TelegramBindRequest(BaseModel):
    telegram_user_id: str
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None
    telegram_last_name: Optional[str] = None
    telegram_language_code: Optional[str] = None
    username: str
    password: str

    @field_validator("telegram_user_id")
    @classmethod
    def validate_telegram_user_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("telegram_user_id is required")
        return normalized

    @field_validator(
        "telegram_username",
        "telegram_first_name",
        "telegram_last_name",
        "telegram_language_code",
    )
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)

    @field_validator("username")
    @classmethod
    def normalize_login(cls, value: str) -> str:
        return normalize_login_identifier(value)


class TelegramAuthRequest(BaseModel):
    telegram_user_id: str

    @field_validator("telegram_user_id")
    @classmethod
    def validate_telegram_user_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("telegram_user_id is required")
        return normalized


class TelegramWebAppAuthRequest(BaseModel):
    init_data: str

    @field_validator("init_data")
    @classmethod
    def validate_init_data(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("init_data is required")
        return normalized


class TelegramWidgetAuthRequest(BaseModel):
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: str
    hash: str

    @field_validator("id", "auth_date", "hash")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("required field is empty")
        return normalized

    @field_validator("first_name", "last_name", "username", "photo_url")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)


class TelegramAuthUser(BaseModel):
    id: UUID
    username: str
    email: Optional[str] = None
    status: str
    balance: float
    vod_movie_limit: int
    vod_tv_limit: int
    vod_movie_used: int
    vod_tv_used: int
    role: str


class TelegramTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: TelegramAuthUser


class TelegramBindingOut(BaseModel):
    telegram_user_id: str
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None
    telegram_last_name: Optional[str] = None
    telegram_language_code: Optional[str] = None
    bound_at: datetime
    is_active: bool
    last_interaction_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TelegramNotificationOut(BaseModel):
    id: UUID
    type: str
    title: str
    content: str
    reference_id: Optional[str] = None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class TelegramNotificationListResponse(BaseModel):
    items: list[TelegramNotificationOut]
    total: int
    page: int
    size: int
    pages: int


class TelegramNotificationPreferenceOut(BaseModel):
    subscription_expiry: bool
    payment: bool
    vod: bool
    ticket: bool

    class Config:
        from_attributes = True


class TelegramNotificationPreferenceUpdate(BaseModel):
    subscription_expiry: bool
    payment: bool
    vod: bool
    ticket: bool


class TelegramMessageResponse(BaseModel):
    message: str


class TelegramMarkAllReadResponse(TelegramMessageResponse):
    count: int
