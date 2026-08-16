from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel


class MoviePilotServerBase(BaseModel):
    name: str
    base_url: AnyHttpUrl
    api_token: Optional[str] = None
    is_active: bool = True
    is_default: bool = False


class MoviePilotServerCreate(MoviePilotServerBase):
    pass


class MoviePilotServerUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[AnyHttpUrl] = None
    api_token: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


class MoviePilotServerOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    is_active: bool
    is_default: bool
    status: str
    latency: Optional[int] = None
    last_check_at: Optional[datetime] = None
    created_at: datetime


class MoviePilotServersResponse(BaseModel):
    servers: list[MoviePilotServerOut]


class MoviePilotProbeResponse(BaseModel):
    id: UUID
    status: str
    latency: int
    message: Optional[str] = None
