from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
from app.db.session import Base


class EmbyServer(Base):
    __tablename__ = "emby_servers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    base_url = Column(String(255), nullable=False)
    external_url = Column(String(255), nullable=True)
    backup_url = Column(String(255), nullable=True)
    api_key = Column(String(255), nullable=False)
    webhook_url = Column(String(255), nullable=True)
    description = Column(String(1000), nullable=True)
    priority = Column(Integer, nullable=False, default=0, index=True)
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<EmbyServer {self.name}>"


class EmbyAccount(Base):
    __tablename__ = "emby_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "emby_server_id", name="uq_emby_account_user_server"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    emby_server_id = Column(
        UUID(as_uuid=True), ForeignKey("emby_servers.id"), nullable=False, index=True
    )
    emby_user_id = Column(String(255), nullable=False)  # Emby 内部用户 ID
    emby_username = Column(String(255), nullable=False)
    emby_password = Column(String(255), nullable=True)
    status = Column(String(50), default="ENABLED")  # ENABLED, DISABLED
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<EmbyAccount {self.emby_username}>"
