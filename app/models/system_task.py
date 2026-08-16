from sqlalchemy import Column, String, DateTime, Integer, Boolean
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
from app.db.session import Base


class SystemTaskStatus(str):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class SystemTask(Base):
    __tablename__ = "system_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(String(512), nullable=True)
    interval_seconds = Column(Integer, nullable=False, default=300)
    enabled = Column(Boolean, nullable=False, default=True)

    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(32), nullable=True)
    last_message = Column(String(1000), nullable=True)
    last_duration_ms = Column(Integer, nullable=True)
    run_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<SystemTask {self.key}>"
