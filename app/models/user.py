from sqlalchemy import (
    Column,
    String,
    DateTime,
    Boolean,
    Enum,
    Integer,
    Text,
    ForeignKey,
    Numeric,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
import enum
from app.db.session import Base


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    BANNED = "BANNED"
    ABNORMAL = "ABNORMAL"
    DELETED = "DELETED"


class UserRole(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"
    SUPERADMIN = "SUPERADMIN"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_users_email_active",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_users_username_active",
            "username",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=True)
    email_verified = Column(Boolean, default=False, nullable=False)
    email_verified_at = Column(DateTime, nullable=True)
    email_verification_token = Column(String(128), nullable=True, index=True)
    email_verification_expires_at = Column(DateTime, nullable=True)
    username = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    phone = Column(String(20), nullable=True)
    avatar_url = Column(Text, nullable=True)
    balance = Column(Numeric(18, 2), default=0, nullable=False)
    expire_remind = Column(Boolean, default=True, nullable=False)
    trial_used = Column(Boolean, default=False, nullable=False)
    inviter_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    vod_movie_limit = Column(Integer, default=0, nullable=False)
    vod_tv_limit = Column(Integer, default=0, nullable=False)
    vod_movie_used = Column(Integer, default=0, nullable=False)
    vod_tv_used = Column(Integer, default=0, nullable=False)
    status = Column(Enum(UserStatus), default=UserStatus.ACTIVE)
    role = Column(Enum(UserRole), default=UserRole.USER)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<User {self.username}>"
