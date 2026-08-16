from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.schemas.system_settings import EmbyClientAppConfig


# Keep defaults conservative: only include schemes we can verify from official docs.
DEFAULT_CLIENT_APP_CONFIGS: tuple[dict[str, Any], ...] = (
    {
        "id": "forward-ios",
        "name": "forward",
        "icon": "F",
        "platform": "iOS",
        "scheme_template": (
            "forward://import?"
            "type=emby&title={{server_name_encoded}}&scheme={{server_scheme}}"
            "&host={{server_host_encoded}}&port={{server_port}}"
            "&username={{username_encoded}}&password={{password_encoded}}"
        ),
        "enabled": True,
        "is_default": True,
        "sort_order": 0,
    },
    {
        "id": "xiaohuan-windows",
        "name": "小幻影视",
        "icon": "小",
        "platform": "Windows",
        "scheme_template": (
            "rodelplayer://import?"
            "type=emby&title={{server_name_encoded}}&scheme={{server_scheme}}"
            "&host={{server_host_encoded}}&path={{server_path_encoded}}&port={{server_port}}"
            "&username={{username_encoded}}&password={{password_encoded}}"
        ),
        "enabled": True,
        "is_default": False,
        "sort_order": 1,
    },
)

LEGACY_PLACEHOLDER_SCHEME_TEMPLATES: dict[str, str] = {
    "infuse-ios": (
        "infuse://x-callback-url/add?"
        "url={{server_url_encoded}}&username={{username_encoded}}&password={{password_encoded}}"
    ),
    "senplayer-ios": (
        "senplayer://add-server?"
        "url={{server_url_encoded}}&username={{username_encoded}}&password={{password_encoded}}"
    ),
    "forward-ios": (
        "forward://import?"
        "url={{server_url_encoded}}&username={{username_encoded}}&password={{password_encoded}}"
    ),
    "yamby-android": (
        "yamby://add-server?"
        "url={{server_url_encoded}}&username={{username_encoded}}&password={{password_encoded}}"
    ),
    "xiaohuan-windows": (
        "xiaohuan://add-server?"
        "url={{server_url_encoded}}&username={{username_encoded}}&password={{password_encoded}}"
    ),
}
VERIFIED_DEFAULT_CONFIGS_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: dict(item) for item in DEFAULT_CLIENT_APP_CONFIGS
}


def default_client_app_configs() -> list[dict[str, Any]]:
    return [dict(item) for item in DEFAULT_CLIENT_APP_CONFIGS]


def migrate_legacy_client_app_configs(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    migrated: list[dict[str, Any]] = []
    changed = False

    for raw in raw_items:
        if not isinstance(raw, dict):
            migrated.append(raw)
            continue

        app_id = str(raw.get("id") or "").strip()
        scheme_template = str(raw.get("scheme_template") or "").strip()
        legacy_scheme = LEGACY_PLACEHOLDER_SCHEME_TEMPLATES.get(app_id)

        if not legacy_scheme or scheme_template != legacy_scheme:
            migrated.append(raw)
            continue

        changed = True
        replacement = VERIFIED_DEFAULT_CONFIGS_BY_ID.get(app_id)
        if not replacement:
            continue

        migrated.append(
            {
                **replacement,
                "enabled": raw.get("enabled", replacement["enabled"]),
                "is_default": raw.get("is_default", replacement["is_default"]),
                "sort_order": raw.get("sort_order", replacement["sort_order"]),
            }
        )

    if changed and not migrated:
        return default_client_app_configs()
    return migrated


def normalize_client_app_configs(
    raw_items: list[dict[str, Any]] | None,
    *,
    only_enabled: bool = False,
) -> list[dict[str, Any]]:
    items = default_client_app_configs() if raw_items is None else raw_items
    if raw_items is not None:
        items = migrate_legacy_client_app_configs(items)
    parsed: list[tuple[int, EmbyClientAppConfig]] = []

    for index, raw in enumerate(items):
        try:
            item = EmbyClientAppConfig.model_validate(raw or {})
        except ValidationError:
            continue

        app_id = (item.id or "").strip() or f"client-app-{index + 1}"
        normalized = item.model_copy(update={"id": app_id})
        if only_enabled and not normalized.enabled:
            continue
        parsed.append((index, normalized))

    parsed.sort(key=lambda pair: (pair[1].sort_order, pair[0], pair[1].name.lower()))

    default_index = next(
        (index for index, (_, item) in enumerate(parsed) if item.enabled and item.is_default),
        None,
    )
    if default_index is None:
        default_index = next(
            (index for index, (_, item) in enumerate(parsed) if item.enabled), None
        )

    normalized_items: list[dict[str, Any]] = []
    for index, (_, item) in enumerate(parsed):
        normalized_items.append(
            item.model_copy(
                update={"is_default": bool(item.enabled and index == default_index)}
            ).model_dump(mode="python")
        )
    return normalized_items
