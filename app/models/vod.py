from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
    Boolean,
)
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
from app.db.session import Base


class VodRequestStatus(str):
    PENDING = "PENDING"
    CREATED = "CREATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class VodRequest(Base):
    __tablename__ = "vod_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    subscription_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    quota_consumed = Column(Boolean, default=False, nullable=False)
    status = Column(String(50), default=VodRequestStatus.PENDING, index=True)
    title = Column(String(255), nullable=False)
    year = Column(Integer, nullable=True)
    media_type = Column(String(50), nullable=False)  # MOVIE, TV
    tmdb_id = Column(Integer, nullable=True)
    douban_id = Column(String(255), nullable=True)
    moviepilot_subscribe_id = Column(String(255), nullable=True)
    moviepilot_download_hash = Column(String(255), nullable=True)
    cost_type = Column(String(50), nullable=False)  # TIMES, POINTS, MONEY
    cost_amount = Column(Integer, nullable=False)
    fail_reason = Column(String(500), nullable=True)
    extra_data = Column(JSON, default={})  # 扩展字段
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<VodRequest {self.title}>"


class VodFavorite(Base):
    __tablename__ = "vod_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "tmdb_id", "media_type", name="uq_vod_favorites_user_tmdb"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(50), nullable=False)
    title = Column(String(255), nullable=False)
    year = Column(Integer, nullable=True)
    overview = Column(Text, nullable=True)
    poster_url = Column(String(500), nullable=True)
    backdrop_url = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<VodFavorite {self.title}>"
