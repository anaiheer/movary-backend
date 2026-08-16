from __future__ import annotations

from copy import deepcopy


def default_telegram_social_auth_config() -> dict:
    return {
        "enabled": False,
        "allow_login": False,
        "allow_bind": False,
        "bot_username": None,
        "bot_display_name": "Telegram",
        "login_mode": "widget",
    }


def default_google_social_auth_config() -> dict:
    return {
        "enabled": False,
        "allow_login": False,
        "allow_bind": False,
        "client_id": None,
        "client_secret": None,
        "redirect_uri": None,
        "display_name": "Google",
    }


def default_social_auth_providers() -> dict:
    return {
        "telegram": default_telegram_social_auth_config(),
        "google": default_google_social_auth_config(),
    }


def _normalize_optional_string(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_telegram_bot_username(value) -> str | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    return normalized.lstrip("@") or None


def normalize_social_auth_providers(value: dict | None, *, public: bool = False) -> dict:
    source = value if isinstance(value, dict) else {}
    normalized = default_social_auth_providers()

    telegram = source.get("telegram") if isinstance(source.get("telegram"), dict) else {}
    google = source.get("google") if isinstance(source.get("google"), dict) else {}
    telegram_enabled = bool(telegram.get("enabled", normalized["telegram"]["enabled"]))
    google_enabled = bool(google.get("enabled", normalized["google"]["enabled"]))

    normalized["telegram"].update(
        {
            "enabled": telegram_enabled,
            "allow_login": telegram_enabled,
            "allow_bind": telegram_enabled,
            "bot_username": _normalize_telegram_bot_username(telegram.get("bot_username")),
            "bot_display_name": _normalize_optional_string(telegram.get("bot_display_name"))
            or "Telegram",
            "login_mode": _normalize_optional_string(telegram.get("login_mode")) or "widget",
        }
    )
    normalized["google"].update(
        {
            "enabled": google_enabled,
            "allow_login": google_enabled,
            "allow_bind": google_enabled,
            "client_id": _normalize_optional_string(google.get("client_id")),
            "client_secret": _normalize_optional_string(google.get("client_secret")),
            "redirect_uri": _normalize_optional_string(google.get("redirect_uri")),
            "display_name": _normalize_optional_string(google.get("display_name")) or "Google",
        }
    )

    if public:
        public_payload = deepcopy(normalized)
        public_payload["google"]["client_secret"] = None
        return public_payload

    return normalized


def get_social_auth_provider_config(providers: dict | None, provider: str) -> dict:
    normalized = normalize_social_auth_providers(providers)
    return normalized.get(provider, {})


def is_social_auth_action_enabled(
    providers: dict | None,
    provider: str,
    action: str,
) -> bool:
    config = get_social_auth_provider_config(providers, provider)
    if not config or not config.get("enabled"):
        return False
    return bool(config.get("enabled"))
