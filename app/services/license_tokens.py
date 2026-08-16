from __future__ import annotations

import base64
from typing import Any

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings

LICENSE_ALGORITHM = "RS256"


def normalize_pem(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "BEGIN " in raw:
        return raw.replace("\\n", "\n")
    try:
        decoded = base64.b64decode(raw.encode("utf-8"), validate=True).decode("utf-8")
        if "BEGIN " in decoded:
            return decoded
    except Exception:  # noqa: BLE001
        pass
    return raw.replace("\\n", "\n")


def verify_signed_license(token: str, *, expected_instance_id: str) -> dict[str, Any]:
    normalized = token.strip()
    if not normalized:
        raise ValueError("许可证内容为空")

    public_key = normalize_pem(settings.MOVARY_LICENSE_PUBLIC_KEY)
    if not public_key:
        raise ValueError("未配置授权服务公钥")

    expected_key_id = (settings.MOVARY_LICENSE_KEY_ID or "").strip()
    try:
        header = jwt.get_unverified_header(normalized)
    except InvalidTokenError as exc:
        raise ValueError("许可证格式无效") from exc

    if expected_key_id and header.get("kid") != expected_key_id:
        raise ValueError("许可证签名密钥不匹配")

    try:
        payload = jwt.decode(
            normalized,
            public_key,
            algorithms=[LICENSE_ALGORITHM],
            options={"verify_aud": False},
        )
    except ExpiredSignatureError as exc:
        raise ValueError("许可证已过期") from exc
    except InvalidTokenError as exc:
        raise ValueError("许可证验签失败") from exc

    if payload.get("edition") != "pro":
        raise ValueError("许可证版本无效")
    if payload.get("instance_id") != expected_instance_id:
        raise ValueError("许可证实例不匹配")
    features = payload.get("features") or {}
    if not isinstance(features, dict) or not features.get("pro"):
        raise ValueError("许可证功能无效")
    return payload
