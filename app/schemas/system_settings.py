from pydantic import BaseModel, Field, EmailStr, field_validator, model_validator, ConfigDict
from typing import Optional
from uuid import UUID
from app.services.site_languages import DEFAULT_SITE_LANGUAGE, SUPPORTED_SITE_LANGUAGES


class EmailTemplateConfig(BaseModel):
    key: str
    label: str = ""
    description: str = ""
    variables: list[str] = Field(default_factory=list)
    subject: str
    html_body: str
    text_body: Optional[str] = None
    default_subject: str = ""
    default_html_body: str = ""
    default_text_body: Optional[str] = None


class EmbyClientAppConfig(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    icon: Optional[str] = Field(default=None, max_length=255)
    platform: Optional[str] = Field(default=None, max_length=64)
    scheme_template: str = Field(min_length=1, max_length=2048)
    user_agent: Optional[str] = Field(default=None, max_length=255)
    enabled: bool = True
    is_default: bool = False
    sort_order: int = Field(default=0, ge=0, le=999)

    @field_validator("id", "name", "scheme_template", mode="before")
    @classmethod
    def _strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return value.strip()

    @field_validator("icon", "platform", "user_agent", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class SocialAuthProviderBase(BaseModel):
    enabled: bool = False
    allow_login: bool = False
    allow_bind: bool = False

    @model_validator(mode="after")
    def _sync_action_flags(self):
        self.allow_login = self.enabled
        self.allow_bind = self.enabled
        return self


class TelegramSocialAuthProviderConfig(SocialAuthProviderBase):
    bot_username: Optional[str] = None
    bot_display_name: str = "Telegram"
    login_mode: str = "widget"

    @field_validator("bot_username", "bot_display_name", "login_mode", mode="before")
    @classmethod
    def _strip_telegram_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        return value or None


class GoogleSocialAuthProviderConfig(SocialAuthProviderBase):
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None
    display_name: str = "Google"

    @field_validator("client_id", "client_secret", "redirect_uri", "display_name", mode="before")
    @classmethod
    def _strip_google_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        return value or None


class SocialAuthProvidersConfig(BaseModel):
    telegram: TelegramSocialAuthProviderConfig = Field(
        default_factory=TelegramSocialAuthProviderConfig
    )
    google: GoogleSocialAuthProviderConfig = Field(default_factory=GoogleSocialAuthProviderConfig)


class SystemSettingsBase(BaseModel):
    default_theme: str = Field(default="dark")
    email_verification_enabled: bool = False
    invite_registration_enabled: bool = False

    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    email_templates: list[EmailTemplateConfig] = Field(default_factory=list)

    epay_enabled: bool = False
    refund_enabled: bool = False
    refund_window_days: int = Field(default=7, ge=0, le=365)
    refund_forbid_if_vod_used: bool = True
    refund_vod_used_threshold: int = Field(default=0, ge=0, le=999999)
    refund_user_monthly_limit: int = Field(default=1, ge=0, le=999999)
    refund_user_monthly_window_days: int = Field(default=30, ge=1, le=365)
    epay_merchant_id: Optional[str] = None
    epay_key: Optional[str] = None
    epay_gateway: Optional[str] = None
    epay_notify_url: Optional[str] = None
    epay_return_url: Optional[str] = None

    site_name: Optional[str] = None
    site_url: Optional[str] = None
    site_logo_url: Optional[str] = None
    enabled_languages: list[str] = Field(default_factory=lambda: [DEFAULT_SITE_LANGUAGE])
    default_language: str = Field(default=DEFAULT_SITE_LANGUAGE)
    task_log_retention_days: int = Field(default=30, ge=1, le=3650)
    subscription_retention_days: int = Field(default=30, ge=0, le=3650)
    emby_client_apps: list[EmbyClientAppConfig] = Field(default_factory=list)
    social_auth_providers: SocialAuthProvidersConfig = Field(
        default_factory=SocialAuthProvidersConfig
    )

    tmdb_base_url: Optional[str] = None
    tmdb_api_key: Optional[str] = None
    tmdb_proxy_url: Optional[str] = None
    tmdb_warmup_enabled: bool = True
    tmdb_warmup_interval_seconds: Optional[int] = Field(default=None, ge=300, le=86400)

    @field_validator("enabled_languages", mode="before")
    @classmethod
    def _normalize_enabled_languages(cls, value: list[str] | None) -> list[str]:
        items = value or []
        normalized: list[str] = []
        for item in items:
            language = str(item or "").strip()
            if language in SUPPORTED_SITE_LANGUAGES and language not in normalized:
                normalized.append(language)
        return normalized or [DEFAULT_SITE_LANGUAGE]

    @field_validator("default_language", mode="before")
    @classmethod
    def _normalize_default_language(cls, value: str | None) -> str:
        language = str(value or "").strip()
        if language in SUPPORTED_SITE_LANGUAGES:
            return language
        return DEFAULT_SITE_LANGUAGE


class SystemSettingsUpdate(SystemSettingsBase):
    pass


class SystemSettingsOut(SystemSettingsBase):
    id: UUID

    model_config = ConfigDict(from_attributes=True)


class SmtpTestRequest(BaseModel):
    to_email: EmailStr
