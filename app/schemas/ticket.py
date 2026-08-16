from pydantic import BaseModel, Field, ConfigDict
from typing import Optional
from uuid import UUID
from datetime import datetime


class TicketCreate(BaseModel):
    subject: str = Field(..., min_length=2, max_length=255)
    content: str = Field(..., min_length=2, max_length=5000)
    priority: str = "MEDIUM"


class TicketMessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)


class TicketMessageOut(BaseModel):
    id: UUID
    ticket_id: UUID
    sender_user_id: UUID
    sender_role: str
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TicketUserSummary(BaseModel):
    id: UUID
    username: str
    email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TicketOut(BaseModel):
    id: UUID
    user_id: UUID
    user: Optional[TicketUserSummary] = None
    subject: str
    status: str
    priority: str
    last_reply_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TicketDetailOut(BaseModel):
    ticket: TicketOut
    messages: list[TicketMessageOut]


class TicketListResponse(BaseModel):
    items: list[TicketOut]


class AdminTicketStats(BaseModel):
    open_count: int
    pending_count: int
    resolved_count: int
    closed_count: int


class AdminTicketListResponse(BaseModel):
    items: list[TicketOut]
    page: int
    page_size: int
    total: int
    stats: AdminTicketStats
