from sqlalchemy import Column, String, DateTime, Boolean, Integer, JSON
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
from app.db.session import Base
from app.services.client_apps import default_client_app_configs
from app.services.social_auth import default_social_auth_providers
from app.services.site_languages import DEFAULT_SITE_LANGUAGE, default_enabled_site_languages


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    default_theme = Column(String(16), default="dark", nullable=False)
    email_verification_enabled = Column(Boolean, default=False, nullable=False)
    invite_registration_enabled = Column(Boolean, default=False, nullable=False)

    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_user = Column(String(255), nullable=True)
    smtp_password = Column(String(255), nullable=True)
    smtp_from = Column(String(255), nullable=True)
    smtp_use_tls = Column(Boolean, default=True, nullable=False)
    smtp_use_ssl = Column(Boolean, default=False, nullable=False)
    email_templates = Column(JSON, nullable=True, default=list)

    epay_enabled = Column(Boolean, default=False, nullable=False)
    refund_enabled = Column(Boolean, default=False, nullable=False)
    refund_window_days = Column(Integer, default=7, nullable=False)
    refund_forbid_if_vod_used = Column(Boolean, default=True, nullable=False)
    refund_vod_used_threshold = Column(Integer, default=0, nullable=False)
    refund_user_monthly_limit = Column(Integer, default=1, nullable=False)
    refund_user_monthly_window_days = Column(Integer, default=30, nullable=False)
    epay_merchant_id = Column(String(64), nullable=True)
    epay_key = Column(String(255), nullable=True)
    epay_gateway = Column(String(255), nullable=True)
    epay_notify_url = Column(String(255), nullable=True)
    epay_return_url = Column(String(255), nullable=True)

    site_name = Column(String(128), nullable=True, default="Movary")
    site_url = Column(String(255), nullable=True)
    site_logo_url = Column(String(255), nullable=True)
    enabled_languages = Column(JSON, nullable=True, default=default_enabled_site_languages)
    default_language = Column(String(16), nullable=False, default=DEFAULT_SITE_LANGUAGE)
    task_log_retention_days = Column(Integer, default=30, nullable=False)
    subscription_retention_days = Column(Integer, default=30, nullable=False)
    emby_client_apps = Column(JSON, nullable=True, default=default_client_app_configs)
    social_auth_providers = Column(JSON, nullable=True, default=default_social_auth_providers)

    tmdb_base_url = Column(String(255), nullable=True)
    tmdb_api_key = Column(String(255), nullable=True)
    tmdb_proxy_url = Column(String(255), nullable=True)
    tmdb_cache_search_ttl = Column(Integer, nullable=True)
    tmdb_cache_discover_ttl = Column(Integer, nullable=True)
    tmdb_cache_genres_ttl = Column(Integer, nullable=True)
    tmdb_cache_companies_ttl = Column(Integer, nullable=True)
    tmdb_warmup_enabled = Column(Boolean, default=True, nullable=False)
    tmdb_warmup_categories = Column(String(255), nullable=True)
    tmdb_warmup_include_genres = Column(Boolean, default=True, nullable=False)
    tmdb_warmup_include_companies = Column(Boolean, default=True, nullable=False)
    tmdb_warmup_interval_seconds = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<SystemSettings {self.default_theme}>"
