from __future__ import annotations

from typing import Any


SUPPORTED_SITE_LANGUAGES: tuple[str, str] = ("zh-CN", "en-US")
DEFAULT_SITE_LANGUAGE = "zh-CN"
DEFAULT_ENABLED_SITE_LANGUAGES: tuple[str, str] = SUPPORTED_SITE_LANGUAGES


def default_enabled_site_languages() -> list[str]:
    return list(DEFAULT_ENABLED_SITE_LANGUAGES)


def normalize_site_languages(
    enabled_languages: list[Any] | None,
    default_language: Any | None,
) -> tuple[list[str], str]:
    normalized_enabled: list[str] = []
    for item in enabled_languages or []:
        value = str(item or "").strip()
        if value in SUPPORTED_SITE_LANGUAGES and value not in normalized_enabled:
            normalized_enabled.append(value)

    if not normalized_enabled:
        normalized_enabled = [DEFAULT_SITE_LANGUAGE]

    normalized_default = str(default_language or "").strip()
    if normalized_default not in normalized_enabled:
        normalized_default = normalized_enabled[0]

    return normalized_enabled, normalized_default


def normalize_site_language(value: Any | None) -> str:
    raw = str(value or "").strip()
    if raw in SUPPORTED_SITE_LANGUAGES:
        return raw

    lowered = raw.lower()
    if lowered.startswith("en"):
        return "en-US"
    if lowered.startswith("zh"):
        return "zh-CN"
    return DEFAULT_SITE_LANGUAGE


def resolve_request_language(*values: Any | None) -> str:
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        for part in raw.split(","):
            candidate = part.split(";", 1)[0].strip()
            if not candidate:
                continue
            normalized = normalize_site_language(candidate)
            if candidate in SUPPORTED_SITE_LANGUAGES or candidate.lower().startswith(("en", "zh")):
                return normalized
    return DEFAULT_SITE_LANGUAGE
