from datetime import datetime
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class SocialAccountBinding(Base):
    __tablename__ = "social_account_bindings"
    __table_args__ = (
        Index(
            "uq_social_account_bindings_active_provider_user",
            "provider",
            "provider_user_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "uq_social_account_bindings_active_user_provider",
            "user_id",
            "provider",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        Index("ix_social_account_bindings_user_id", "user_id"),
        Index("ix_social_account_bindings_provider", "provider"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider = Column(String(32), nullable=False)
    provider_user_id = Column(String(255), nullable=False)
    provider_email = Column(String(255), nullable=True)
    provider_name = Column(String(255), nullable=True)
    provider_avatar_url = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    bound_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    unbound_at = Column(DateTime, nullable=True)
    last_interaction_at = Column(DateTime, default=datetime.utcnow, nullable=False)
