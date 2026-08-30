from __future__ import annotations

from typing import Any

LICENSE_MANAGE_URL = "/admin/settings?tab=license"


def build_license_overview(
    extension_state: dict[str, Any], runtime_state: dict[str, Any] | None = None
) -> dict[str, str]:
    enabled = extension_state.get("enabled", [])
    loaded = extension_state.get("loaded", [])
    failed = extension_state.get("failed", [])
    runtime_state = runtime_state or {}
    license_status = str(runtime_state.get("license_status") or "inactive").lower()
    active = license_status == "active"
    expired = license_status == "expired"
    invalid = license_status == "invalid"
    pro_loaded = any(item.get("name") == "pro" for item in loaded)
    pro_failed = any(item.get("name") == "pro" for item in failed)

    if active and pro_loaded:
        return {
            "edition": "pro",
            "status": "ACTIVE",
            "message": "专业版授权已生效，当前实例正在运行专业版能力。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    if active and pro_failed:
        return {
            "edition": "base",
            "status": "ERROR",
            "message": "授权有效，但专业版扩展加载失败，请检查扩展环境。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    if active and "pro" in enabled:
        return {
            "edition": "base",
            "status": "PENDING",
            "message": "授权有效，但专业版能力尚未对当前实例生效。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    if pro_failed:
        return {
            "edition": "base",
            "status": "ERROR",
            "message": "专业版扩展加载失败，请检查运行环境或扩展路径。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    if expired:
        return {
            "edition": "base",
            "status": "EXPIRED",
            "message": "专业版授权已过期，请续期或重新激活。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    if invalid:
        return {
            "edition": "base",
            "status": "INVALID",
            "message": "当前许可证无效或与实例不匹配，请重新激活。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    if "pro" in enabled:
        return {
            "edition": "base",
            "status": "PENDING",
            "message": "当前实例已启用专业版扩展配置，但尚未完成有效授权。",
            "manage_url": LICENSE_MANAGE_URL,
        }
    return {
        "edition": "base",
        "status": "BASE",
        "message": "当前运行基础版，未启用专业版扩展。",
        "manage_url": LICENSE_MANAGE_URL,
    }
