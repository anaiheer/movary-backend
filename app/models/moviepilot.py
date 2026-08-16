from datetime import datetime
import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class MoviePilotServer(Base):
    __tablename__ = "moviepilot_servers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    base_url = Column(String(255), nullable=False)
    api_token = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)
    status = Column(String(20), default="OFFLINE", nullable=False)
    latency = Column(Integer, nullable=True)
    last_check_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<MoviePilotServer {self.name}>"
