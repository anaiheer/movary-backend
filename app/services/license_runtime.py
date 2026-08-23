from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.core.license_config import license_settings
from app.services.license_tokens import verify_signed_license


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_datetime(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text or None


def get_license_state_file() -> Path:
    if license_settings.state_file:
        return Path(license_settings.state_file).expanduser().resolve()
    base_dir = Path(__file__).resolve().parent.parent.parent
    return (base_dir / "runtime" / "license-state.json").resolve()


def get_license_instance_file() -> Path:
    if license_settings.instance_file:
        return Path(license_settings.instance_file).expanduser().resolve()
    base_dir = Path(__file__).resolve().parent.parent.parent
    return (base_dir / "runtime" / "license-instance.json").resolve()


def _default_instance_label() -> str:
    if settings.FRONTEND_BASE_URL:
        return str(settings.FRONTEND_BASE_URL).strip().rstrip("/")
    return socket.gethostname()


def get_or_create_instance_identity(path: Path | None = None) -> dict[str, str]:
    instance_file = path or get_license_instance_file()
    if instance_file.exists():
        payload = json.loads(instance_file.read_text(encoding="utf-8"))
        instance_id = str(payload.get("instance_id") or "").strip()
        if instance_id:
            return {
                "instance_id": instance_id,
                "instance_label": str(payload.get("instance_label") or _default_instance_label()),
            }

    payload = {
        "instance_id": str(uuid4()),
        "instance_label": _default_instance_label(),
    }
    instance_file.parent.mkdir(parents=True, exist_ok=True)
    instance_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return payload


def load_license_state(path: Path | None = None) -> dict[str, Any]:
    identity = get_or_create_instance_identity()
    state_file = path or get_license_state_file()
    if not state_file.exists():
        return {
            **identity,
            "activation_present": False,
            "activation_code_hint": None,
            "activated_at": None,
            "last_refresh_at": None,
            "license": None,
            "expires_at": None,
            "key_id": None,
            "package_code": None,
            "package_name": None,
            "license_id": None,
            "edition": "base",
            "license_status": "inactive",
            "license_message": "当前尚未激活专业版授权。",
        }

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    merged = {
        **identity,
        "activation_present": bool(payload.get("activation_present")),
        "activation_code_hint": payload.get("activation_code_hint"),
        "activated_at": _safe_datetime(payload.get("activated_at")),
        "last_refresh_at": _safe_datetime(payload.get("last_refresh_at")),
        "license": payload.get("license"),
        "expires_at": _safe_datetime(payload.get("expires_at")),
        "key_id": payload.get("key_id"),
        "package_code": payload.get("package_code"),
        "package_name": payload.get("package_name"),
        "license_id": payload.get("license_id"),
        "edition": payload.get("edition") or "base",
    }
    validated = evaluate_cached_license(merged)
    merged["license_status"] = validated["license_status"]
    merged["license_message"] = validated["license_message"]
    merged["activation_present"] = bool(merged.get("license"))
    return merged


def save_license_state(data: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    identity = get_or_create_instance_identity()
    state_file = path or get_license_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "activation_present": bool(data.get("activation_present")),
        "activation_code_hint": data.get("activation_code_hint"),
        "activated_at": _safe_datetime(data.get("activated_at")),
        "last_refresh_at": _safe_datetime(data.get("last_refresh_at")),
        "license": data.get("license"),
        "expires_at": _safe_datetime(data.get("expires_at")),
        "key_id": data.get("key_id"),
        "package_code": data.get("package_code"),
        "package_name": data.get("package_name"),
        "license_id": data.get("license_id"),
        "edition": data.get("edition") or "base",
    }
    state_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {**identity, **payload}


def cache_remote_license(
    *,
    code_hint: str,
    license_token: str,
    expires_at: str | None,
    key_id: str | None,
    package_code: str | None,
    package_name: str | None,
    license_id: str | None,
) -> dict[str, Any]:
    now = _utcnow()
    return save_license_state(
        {
            "activation_present": True,
            "activation_code_hint": code_hint,
            "activated_at": now,
            "last_refresh_at": now,
            "license": license_token,
            "expires_at": expires_at,
            "key_id": key_id,
            "package_code": package_code,
            "package_name": package_name,
            "license_id": license_id,
            "edition": "pro",
        }
    )


def update_cached_license_refresh(
    *,
    current_state: dict[str, Any],
    license_token: str,
    expires_at: str | None,
    key_id: str | None,
    package_code: str | None,
    package_name: str | None,
    license_id: str | None,
) -> dict[str, Any]:
    return save_license_state(
        {
            **current_state,
            "activation_present": True,
            "last_refresh_at": _utcnow(),
            "license": license_token,
            "expires_at": expires_at,
            "key_id": key_id,
            "package_code": package_code,
            "package_name": package_name,
            "license_id": license_id,
            "edition": "pro",
        }
    )


def clear_cached_license(path: Path | None = None) -> dict[str, Any]:
    return save_license_state(
        {
            "activation_present": False,
            "activation_code_hint": None,
            "activated_at": None,
            "last_refresh_at": None,
            "license": None,
            "expires_at": None,
            "key_id": None,
            "package_code": None,
            "package_name": None,
            "license_id": None,
            "edition": "base",
        },
        path=path,
    )


def evaluate_cached_license(state: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_state = dict(state or load_license_state())
    license_token = str(runtime_state.get("license") or "").strip()
    if not license_token:
        return {
            **runtime_state,
            "license_status": "inactive",
            "license_message": "当前尚未激活专业版授权。",
            "effective_edition": "base",
            "is_active": False,
        }

    try:
        payload = verify_signed_license(
            license_token,
            expected_instance_id=str(runtime_state.get("instance_id") or ""),
        )
    except ValueError as exc:
        message = str(exc)
        status = "expired" if "已过期" in message else "invalid"
        return {
            **runtime_state,
            "license_status": status,
            "license_message": message,
            "effective_edition": "base",
            "is_active": False,
        }

    return {
        **runtime_state,
        "license_status": "active",
        "license_message": "专业版授权有效。",
        "effective_edition": "pro",
        "is_active": True,
        "payload": payload,
        "expires_at": _safe_datetime(payload.get("expires_at")) or runtime_state.get("expires_at"),
        "package_code": payload.get("package_code") or runtime_state.get("package_code"),
        "package_name": payload.get("package_name") or runtime_state.get("package_name"),
        "license_id": payload.get("license_id") or runtime_state.get("license_id"),
        "edition": payload.get("edition") or "pro",
    }


def has_active_pro_license() -> bool:
    return bool(evaluate_cached_license().get("is_active"))
