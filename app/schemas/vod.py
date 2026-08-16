from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class VodRequestCreate(BaseModel):
    title: str
    media_type: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    douban_id: Optional[str] = None


class VodRequestOut(BaseModel):
    id: UUID
    user_id: UUID
    title: str
    media_type: str
    status: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    cost_amount: int
    fail_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class VodSearchHit(BaseModel):
    id: str
    title: str
    year: str
    overview: str
    media_type: str
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None


class VodRequestUser(BaseModel):
    id: UUID
    username: str
    email: Optional[str] = None


class VodRequestAdminOut(BaseModel):
    id: UUID
    user: VodRequestUser
    title: str
    media_type: str
    status: str
    status_label: Optional[str] = None
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    cost_amount: int
    fail_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class VodRequestListResponse(BaseModel):
    items: list[VodRequestAdminOut]
    page: int
    page_size: int
    total: int


class VodSettingsOut(BaseModel):
    auto_approve: bool


class VodSettingsUpdate(BaseModel):
    auto_approve: bool


class VodRejectRequest(BaseModel):
    reason: str


class VodSearchResponse(BaseModel):
    results: list[VodSearchHit]


class VodFavoriteCreate(BaseModel):
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    overview: Optional[str] = None
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None


class VodFavoriteOut(BaseModel):
    id: UUID
    user_id: UUID
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    overview: Optional[str] = None
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class VodFavoriteCheck(BaseModel):
    favorited: bool
