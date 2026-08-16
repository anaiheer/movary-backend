from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, AnyHttpUrl


class EmbyServerBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    base_url: AnyHttpUrl
    external_url: Optional[AnyHttpUrl] = None
    backup_url: Optional[AnyHttpUrl] = None
    api_key: str = Field(..., min_length=6, max_length=255)
    webhook_url: Optional[AnyHttpUrl] = None
    description: Optional[str] = Field(default=None, max_length=1000)
    priority: int = Field(default=0, ge=0, le=9999)
    is_active: bool = True
    is_default: bool = False


class EmbyServerCreate(EmbyServerBase):
    pass


class EmbyServerUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[AnyHttpUrl] = None
    external_url: Optional[AnyHttpUrl] = None
    backup_url: Optional[AnyHttpUrl] = None
    api_key: Optional[str] = Field(default=None, min_length=6, max_length=255)
    webhook_url: Optional[AnyHttpUrl] = None
    description: Optional[str] = Field(default=None, max_length=1000)
    priority: Optional[int] = Field(default=None, ge=0, le=9999)
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


class EmbyServerOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    external_url: Optional[str]
    backup_url: Optional[str]
    webhook_url: Optional[str]
    description: Optional[str]
    priority: int
    status: str
    latency: int
    library: Optional[str] = None
    is_default: bool = False
    is_active: bool = True
    user_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class EmbyServersResponse(BaseModel):
    servers: List[EmbyServerOut]


class EmbyProbeResponse(BaseModel):
    id: UUID
    status: str
    latency: int
    message: Optional[str] = None


class EmbyServerStats(BaseModel):
    server_id: UUID
    name: str
    base_url: str
    user_total: int
    enabled_accounts: int
    disabled_accounts: int
    active_users: int
    banned_users: int
    expired_subscriptions: int
    created_at: datetime
    is_active: bool
    is_default: bool
    priority: int


class EmbyAccountOut(BaseModel):
    id: str
    user_id: Optional[str]
    username: str
    email: Optional[str] = None
    account_status: str
    user_status: str
    emby_status: str
    subscription_status: str
    last_login_at: Optional[datetime]
    created_at: Optional[datetime]


class EmbyAccountsResponse(BaseModel):
    items: List[EmbyAccountOut]
    total: int


class EmbyUserAccountOut(BaseModel):
    server_id: UUID
    server_name: str
    base_url: str
    external_url: Optional[str]
    backup_url: Optional[str] = None
    username: str
    emby_password: Optional[str] = None
    status: str
    created_at: datetime


class EmbyUserAccountsResponse(BaseModel):
    items: List[EmbyUserAccountOut]


class EmbyUserServerOut(BaseModel):
    server_id: UUID
    server_name: str
    base_url: str
    external_url: Optional[str] = None
    plan_id: UUID
    plan_name: str


class EmbyUserServersResponse(BaseModel):
    items: List[EmbyUserServerOut]


class EmbyPasswordUpdate(BaseModel):
    password: str = Field(min_length=6, max_length=64)
