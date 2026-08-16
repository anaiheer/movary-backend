from app.core.config import settings
from app.models.system_settings import SystemSettings


def _normalize_url(value: str | None) -> str:
    return (value or "").strip()


def get_site_base_url(settings_row: SystemSettings) -> str:
    base_url = _normalize_url(getattr(settings_row, "site_url", None)) or settings.FRONTEND_BASE_URL
    return base_url.rstrip("/")


def build_site_url(settings_row: SystemSettings, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{get_site_base_url(settings_row)}{normalized_path}"


def get_epay_notify_url(settings_row: SystemSettings) -> str:
    explicit = _normalize_url(getattr(settings_row, "epay_notify_url", None))
    if explicit:
        return explicit
    return build_site_url(settings_row, f"{settings.API_V1_STR}/pay/epay/notify")


def get_epay_return_url(settings_row: SystemSettings) -> str:
    explicit = _normalize_url(getattr(settings_row, "epay_return_url", None))
    if explicit:
        return explicit
    return build_site_url(settings_row, "/pay/return")
