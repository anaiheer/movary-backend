from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CapabilitySet(BaseModel):
    edition: str = "base"
    license_status: str = "inactive"
    limits: dict[str, Any] = Field(
        default_factory=lambda: {
            "max_users": None,
            "max_emby_servers": 1,
            "max_moviepilot_servers": 1,
            "allow_backup_line": False,
            "allow_subscription_groups": False,
            "allow_group_upgrade": False,
        }
    )
    features: dict[str, bool] = Field(
        default_factory=lambda: {
            "simple_server_management": True,
            "simple_subscription_plan": True,
            "multi_emby_servers": False,
            "multi_moviepilot_servers": False,
            "backup_line": False,
            "subscription_groups": False,
            "group_upgrade": False,
            "license_activation": True,
        }
    )
    extensions: dict[str, Any] = Field(
        default_factory=lambda: {"enabled": [], "loaded": [], "failed": []}
    )


DEFAULT_CAPABILITIES = CapabilitySet()


def get_default_capabilities() -> CapabilitySet:
    return DEFAULT_CAPABILITIES.model_copy(deep=True)


def apply_capability_overrides(
    capabilities: CapabilitySet, overrides: dict[str, Any] | None = None
) -> CapabilitySet:
    payload = overrides or {}
    for key, value in payload.items() if isinstance(payload, dict) else []:
        if key in capabilities.features:
            capabilities.features[key] = bool(value)
        else:
            capabilities.limits[key] = value
    return capabilities
