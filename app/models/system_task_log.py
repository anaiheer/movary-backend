from datetime import datetime
import uuid

from sqlalchemy import Column, DateTime, Integer, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class SystemTaskLog(Base):
    __tablename__ = "system_task_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("system_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_key = Column(String(64), nullable=False, index=True)
    task_name = Column(String(128), nullable=False)
    status = Column(String(32), nullable=True)
    message = Column(String(1000), nullable=True)
    run_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    duration_ms = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<SystemTaskLog {self.task_key} {self.status}>"
