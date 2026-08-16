from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field


class ManagedEmbyServerUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: AnyHttpUrl
    external_url: Optional[AnyHttpUrl] = None
    api_key: Optional[str] = Field(default=None, min_length=6, max_length=255)
    webhook_url: Optional[AnyHttpUrl] = None
    description: Optional[str] = Field(default=None, max_length=1000)
    is_active: bool = True


class ManagedMoviePilotServerUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: AnyHttpUrl
    api_token: Optional[str] = Field(default=None, max_length=255)
    is_active: bool = True


class ManagedEmbyServerOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    external_url: Optional[str] = None
    webhook_url: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    created_at: datetime


class ManagedMoviePilotServerOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    is_active: bool
    created_at: datetime


class ServerConfigSummary(BaseModel):
    max_emby_servers: int = 1
    max_moviepilot_servers: int = 1
    emby_count: int
    moviepilot_count: int
    extra_emby_servers: int = 0
    extra_moviepilot_servers: int = 0
    emby_locked: bool = False
    moviepilot_locked: bool = False
    pro_data_detected: bool = False
    pro_server_extension_loaded: bool = False
    pro_server_admin_path: Optional[str] = None
    notices: list[str] = Field(default_factory=list)


class ServerConfigData(BaseModel):
    emby_server: Optional[ManagedEmbyServerOut] = None
    moviepilot_server: Optional[ManagedMoviePilotServerOut] = None
    summary: ServerConfigSummary
