from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


TELEGRAM_NOTIFICATION_PREFERENCE_MAP = {
    "subscription_expiry_warning": "subscription_expiry",
    "subscription_expired": "subscription_expiry",
    "subscription_activated": "subscription_expiry",
    "payment_success": "payment",
    "payment_failed": "payment",
    "vod_approved": "vod",
    "vod_rejected": "vod",
    "vod_completed": "vod",
    "ticket_reply": "ticket",
}


class TelegramUserBinding(Base):
    __tablename__ = "telegram_user_bindings"
    __table_args__ = (
        Index(
            "uq_telegram_user_bindings_active_tg_user",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "uq_telegram_user_bindings_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_telegram_user_bindings_user_id", "user_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    telegram_user_id = Column(String(64), nullable=False)
    telegram_username = Column(String(255), nullable=True)
    telegram_first_name = Column(String(255), nullable=True)
    telegram_last_name = Column(String(255), nullable=True)
    telegram_language_code = Column(String(10), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    bound_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    unbound_at = Column(DateTime, nullable=True)
    last_interaction_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TelegramNotification(Base):
    __tablename__ = "telegram_notifications"
    __table_args__ = (
        Index("ix_telegram_notifications_user_created_at", "user_id", "created_at"),
        Index("ix_telegram_notifications_user_type", "user_id", "type"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String(32), nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    reference_id = Column(String(64), nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TelegramNotificationPreference(Base):
    __tablename__ = "telegram_notification_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    subscription_expiry = Column(Boolean, default=True, nullable=False)
    payment = Column(Boolean, default=True, nullable=False)
    vod = Column(Boolean, default=True, nullable=False)
    ticket = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def is_type_enabled(self, notification_type: str) -> bool:
        field_name = TELEGRAM_NOTIFICATION_PREFERENCE_MAP.get(notification_type)
        if not field_name:
            return True
        return bool(getattr(self, field_name, True))
