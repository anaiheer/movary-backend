from typing import Any, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 基本配置
    DEBUG: bool = True
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "Movary"

    # 数据库
    DATABASE_URL: str = "postgresql://postgres:password@localhost:5432/movary"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    # CORS
    CORS_ORIGINS: list = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Frontend
    FRONTEND_BASE_URL: str = "http://localhost:5173"

    # Emby
    EMBY_PASSWORD_KEY: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: Optional[str] = None

    # TMDB
    TMDB_BASE_URL: str = "https://api.themoviedb.org/3"
    TMDB_API_KEY: Optional[str] = None
    TMDB_PROXY_URL: Optional[str] = None

    # Uploads
    AVATAR_UPLOAD_DIR: str = "uploads/avatars"
    AVATAR_URL_PREFIX: str = "/uploads/avatars"

    # Admin seed
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_EMAIL: str = "admin@example.com"

    # 邮件
    MAIL_FROM: str = "noreply@example.com"
    SMTP_HOST: str = "smtp.example.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "your-email"
    SMTP_PASSWORD: str = "your-password"

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value: Any) -> Any:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"release", "prod", "production"}:
                return False
            if lowered in {"debug", "dev", "development"}:
                return True
        return value

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


settings = Settings()
