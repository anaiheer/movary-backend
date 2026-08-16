from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DocBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(default="")
    is_visible: bool = True
    sort_order: int = Field(default=0, ge=0, le=999999)


class DocCreate(DocBase):
    pass


class DocUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    content: Optional[str] = None
    is_visible: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=999999)


class DocOut(DocBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AdminDocsListResponse(BaseModel):
    items: list[DocOut]


class UserDocsListResponse(BaseModel):
    items: list[DocOut]
