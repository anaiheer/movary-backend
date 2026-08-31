from __future__ import annotations

from typing import Any

import httpx

from app import __version__
from app.core.license_config import (
    LICENSE_KEY_ID,
    LICENSE_PUBLIC_KEY,
    LICENSE_REQUEST_TIMEOUT_SECONDS,
    license_settings,
)


class LicenseProviderError(ValueError):
    """Raised when the official license provider cannot serve a valid response."""


def _normalize_key_value(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized


def get_license_provider_status() -> dict[str, Any]:
    server_url = _normalize_key_value(license_settings.provider_url)
    public_key = _normalize_key_value(LICENSE_PUBLIC_KEY)
    key_id = _normalize_key_value(LICENSE_KEY_ID)

    missing_fields: list[str] = []
    if not server_url:
        missing_fields.append("server_url")
    if not public_key:
        missing_fields.append("public_key")
    if not key_id:
        missing_fields.append("key_id")

    ready = not missing_fields
    return {
        "mode": "online_signed",
        "ready": ready,
        "server_url": server_url or None,
        "key_id": key_id or None,
        "missing_fields": missing_fields,
    }


def _provider_base_url() -> str:
    provider = get_license_provider_status()
    if not provider["ready"] or not provider["server_url"]:
        raise ValueError("在线授权服务未配置完成")
    return str(provider["server_url"]).rstrip("/")


async def _post_provider(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=LICENSE_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{_provider_base_url()}{path}", json=payload)
    except httpx.HTTPError as exc:
        raise LicenseProviderError(f"授权服务暂时不可用：{exc}") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("detail") or body.get("message") or "").strip()
        except Exception:  # noqa: BLE001
            detail = response.text.strip()
        message = detail or f"授权服务请求失败 ({response.status_code})"
        if response.status_code >= 500:
            raise LicenseProviderError(message)
        raise ValueError(message)
    try:
        body = response.json()
    except ValueError as exc:
        raise LicenseProviderError("授权服务返回了无效响应") from exc
    if not isinstance(body, dict):
        raise LicenseProviderError("授权服务返回了无效响应")
    return body


async def activate_remote_license(
    *,
    code: str,
    instance_id: str,
    edition: str,
    instance_label: str | None = None,
) -> dict[str, Any]:
    return await _post_provider(
        "/license/activate",
        {
            "code": code,
            "instance_id": instance_id,
            "edition": edition,
            "instance_label": instance_label,
            "base_version": __version__,
        },
    )


async def refresh_remote_license(*, license_token: str, instance_id: str) -> dict[str, Any]:
    return await _post_provider(
        "/license/refresh",
        {"license": license_token, "instance_id": instance_id, "base_version": __version__},
    )


async def deactivate_remote_license(*, license_token: str, instance_id: str) -> dict[str, Any]:
    return await _post_provider(
        "/license/deactivate",
        {"license": license_token, "instance_id": instance_id},
    )
