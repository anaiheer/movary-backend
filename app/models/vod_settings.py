from sqlalchemy import Column, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
from app.db.session import Base


class VodSettings(Base):
    __tablename__ = "vod_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    auto_approve = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<VodSettings auto_approve={self.auto_approve}>"
