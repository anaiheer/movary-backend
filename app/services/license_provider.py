from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


def _normalize_key_value(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized


def get_license_provider_status() -> dict[str, Any]:
    server_url = _normalize_key_value(settings.MOVARY_LICENSE_SERVER_URL)
    public_key = _normalize_key_value(settings.MOVARY_LICENSE_PUBLIC_KEY)
    key_id = _normalize_key_value(settings.MOVARY_LICENSE_KEY_ID)

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
    timeout = max(int(settings.MOVARY_LICENSE_REQUEST_TIMEOUT or 10), 1)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{_provider_base_url()}{path}", json=payload)
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("detail") or body.get("message") or "").strip()
        except Exception:  # noqa: BLE001
            detail = response.text.strip()
        raise ValueError(detail or f"授权服务请求失败 ({response.status_code})")
    return response.json()


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
        },
    )


async def refresh_remote_license(*, license_token: str, instance_id: str) -> dict[str, Any]:
    return await _post_provider(
        "/license/refresh",
        {"license": license_token, "instance_id": instance_id},
    )


async def deactivate_remote_license(*, license_token: str, instance_id: str) -> dict[str, Any]:
    return await _post_provider(
        "/license/deactivate",
        {"license": license_token, "instance_id": instance_id},
    )


async def get_license_provider_health() -> tuple[bool, str]:
    provider = get_license_provider_status()
    if not provider["ready"] or not provider["server_url"]:
        return False, "在线授权服务未配置完成"

    timeout = max(int(settings.MOVARY_LICENSE_REQUEST_TIMEOUT or 10), 1)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{str(provider['server_url']).rstrip('/')}/license/health")
        if response.status_code >= 400:
            return False, f"授权服务不可用 ({response.status_code})"
        payload = response.json()
        if payload.get("status") == "ok":
            return True, "在线授权服务可用"
        return False, "在线授权服务响应异常"
    except Exception as exc:  # noqa: BLE001
        return False, f"无法连接在线授权服务: {exc}"
