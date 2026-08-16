from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class SystemTaskOut(BaseModel):
    id: UUID
    key: str
    name: str
    description: Optional[str] = None
    interval_seconds: int
    enabled: bool
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_message: Optional[str] = None
    last_duration_ms: Optional[int] = None
    run_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SystemTaskUpdate(BaseModel):
    interval_seconds: int = Field(ge=10, le=86400)
    enabled: bool


class SystemTaskRunResult(BaseModel):
    task_id: UUID
    status: str
    message: str
    updated: int = 0
    checked: int = 0


class SystemTaskLogItem(BaseModel):
    task_id: UUID
    task_key: str
    task_name: str
    status: Optional[str] = None
    message: Optional[str] = None
    run_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


class SystemTaskLogsResponse(BaseModel):
    items: list[SystemTaskLogItem]
