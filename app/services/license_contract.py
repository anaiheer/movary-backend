from __future__ import annotations

from typing import Any

from app.services.license_provider import get_license_provider_status

REQUIRED_ENV_VARS = [
    "MOVARY_LICENSE_SERVER_URL",
    "MOVARY_LICENSE_PUBLIC_KEY",
    "MOVARY_LICENSE_KEY_ID",
]


def get_license_provider_contract() -> dict[str, Any]:
    provider = get_license_provider_status()
    return {
        "provider_mode": provider["mode"],
        "provider_ready": provider["ready"],
        "provider_server_url": provider["server_url"],
        "provider_key_id": provider["key_id"],
        "required_env": REQUIRED_ENV_VARS,
        "missing_env": provider["missing_fields"],
        "remote_endpoints": [
            {
                "name": "activate",
                "method": "POST",
                "path": "/license/activate",
                "purpose": "使用授权码换取签名许可证",
            },
            {
                "name": "refresh",
                "method": "POST",
                "path": "/license/refresh",
                "purpose": "刷新本地缓存许可证",
            },
            {
                "name": "deactivate",
                "method": "POST",
                "path": "/license/deactivate",
                "purpose": "撤销当前实例上的许可证缓存",
            },
        ],
        "activation_flow": [
            "后台录入授权码",
            "Movary 调用远端 /license/activate",
            "远端返回签名许可证与有效期",
            "Movary 本地缓存并在启动/刷新时验签",
        ],
    }
