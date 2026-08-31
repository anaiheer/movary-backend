from hashlib import sha256

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException

from app import __version__
from app.api.routes import admin_license
from app.core.license_config import (
    DEFAULT_LICENSE_PROVIDER_URL,
    LICENSE_KEY_ID,
    LICENSE_PUBLIC_KEY,
    LicenseSettings,
    license_settings,
)
from app.services import license_provider
from app.services.license_provider import get_license_provider_status


def test_license_settings_default_to_official_provider(monkeypatch) -> None:
    monkeypatch.delenv("MOVARY_LICENSE_SERVER_URL", raising=False)

    configured = LicenseSettings(_env_file=None)

    assert configured.provider_url == "https://movary.top"
    assert DEFAULT_LICENSE_PROVIDER_URL == "https://movary.top"
    public_key = serialization.load_pem_public_key(LICENSE_PUBLIC_KEY.encode("ascii"))
    public_key_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert LICENSE_KEY_ID == f"movary-license-{sha256(public_key_der).hexdigest()[:16]}"


def test_license_provider_url_allows_hidden_test_override(monkeypatch) -> None:
    monkeypatch.setenv("MOVARY_LICENSE_SERVER_URL", "https://license.example.com")

    configured = LicenseSettings(_env_file=None)

    assert configured.provider_url == "https://license.example.com"


def test_license_trust_material_is_not_environment_configurable() -> None:
    assert "public_key" not in LicenseSettings.model_fields
    assert "key_id" not in LicenseSettings.model_fields
    assert "request_timeout" not in LicenseSettings.model_fields
    assert "instance_label" not in LicenseSettings.model_fields


def test_license_provider_reports_official_signed_mode(monkeypatch) -> None:
    monkeypatch.setattr(license_settings, "provider_url", DEFAULT_LICENSE_PROVIDER_URL)

    status = get_license_provider_status()

    assert status["mode"] == "online_signed"
    assert status["ready"] is True
    assert status["missing_fields"] == []
    assert status["server_url"] == "https://movary.top"
    assert status["key_id"] == LICENSE_KEY_ID


@pytest.mark.asyncio
async def test_provider_connection_errors_are_reported_as_provider_errors(monkeypatch) -> None:
    class FailingClient:
        async def __aenter__(self):
            raise license_provider.httpx.ConnectError("connection refused")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        license_provider.httpx,
        "AsyncClient",
        lambda **_kwargs: FailingClient(),
    )

    with pytest.raises(license_provider.LicenseProviderError, match="授权服务暂时不可用"):
        await license_provider.activate_remote_license(
            code="activation-code",
            instance_id="instance-id",
            edition="pro",
        )


@pytest.mark.asyncio
async def test_provider_5xx_responses_are_reported_as_provider_errors(monkeypatch) -> None:
    class Response:
        status_code = 503
        text = "service unavailable"

        @staticmethod
        def json() -> dict[str, str]:
            return {"detail": "service unavailable"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(license_provider.httpx, "AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(license_provider.LicenseProviderError, match="service unavailable"):
        await license_provider.activate_remote_license(
            code="activation-code",
            instance_id="instance-id",
            edition="pro",
        )


@pytest.mark.asyncio
async def test_provider_invalid_success_payload_is_reported_as_provider_error(monkeypatch) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            raise ValueError("invalid json")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(license_provider.httpx, "AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(license_provider.LicenseProviderError, match="无效响应"):
        await license_provider.activate_remote_license(
            code="activation-code",
            instance_id="instance-id",
            edition="pro",
        )


@pytest.mark.asyncio
async def test_admin_activate_maps_provider_errors_to_bad_gateway(monkeypatch) -> None:
    async def allow_admin(*_args, **_kwargs) -> None:
        return None

    async def fail_activation(**_kwargs):
        raise license_provider.LicenseProviderError("provider unavailable")

    monkeypatch.setattr(admin_license, "_ensure_admin", allow_admin)
    monkeypatch.setattr(
        admin_license,
        "get_or_create_instance_identity",
        lambda **_kwargs: {"instance_id": "instance-id", "instance_label": None},
    )
    monkeypatch.setattr(admin_license, "activate_remote_license", fail_activation)

    with pytest.raises(HTTPException) as exc_info:
        await admin_license.activate_admin_license(
            payload=admin_license.LicenseActivateRequest(code="activation-code"),
            current_user={},
            db=None,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "provider unavailable"


@pytest.mark.asyncio
async def test_admin_activate_hides_artifact_download_details(monkeypatch) -> None:
    artifact_url = "https://0.0.0.0:3000/artifacts/backend/v1.0.7/backend.zip"

    async def allow_admin(*_args, **_kwargs) -> None:
        return None

    async def activate_license(**_kwargs):
        return {
            "base_version": admin_license.__version__,
            "backend_artifact_version": "v1.0.7",
            "backend_artifact_url": artifact_url,
            "frontend_artifact_version": "v1.0.7",
            "frontend_artifact_url": "https://0.0.0.0:3000/artifacts/frontend/v1.0.7/index.js",
            "license": "signed-license",
        }

    async def fail_artifact_sync(_provider_payload):
        raise admin_license.ProArtifactError(
            f"下载 Pro artifact 失败：{artifact_url}（connection refused）"
        )

    monkeypatch.setattr(admin_license, "_ensure_admin", allow_admin)
    monkeypatch.setattr(
        admin_license,
        "get_or_create_instance_identity",
        lambda **_kwargs: {"instance_id": "instance-id", "instance_label": None},
    )
    monkeypatch.setattr(admin_license, "activate_remote_license", activate_license)
    monkeypatch.setattr(
        admin_license,
        "verify_signed_license",
        lambda *_args, **_kwargs: {"license_id": "license-id"},
    )
    monkeypatch.setattr(admin_license, "sync_pro_artifacts_from_license", fail_artifact_sync)

    with pytest.raises(HTTPException) as exc_info:
        await admin_license.activate_admin_license(
            payload=admin_license.LicenseActivateRequest(code="activation-code"),
            current_user={},
            db=None,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "专业版制品服务暂时不可用，请稍后重试"
    assert artifact_url not in str(exc_info.value.detail)
    assert "connection refused" not in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_activate_reports_base_version_to_artifact_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(path: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update({"path": path, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(license_provider, "_post_provider", fake_post)

    await license_provider.activate_remote_license(
        code="activation-code",
        instance_id="instance-id",
        edition="pro",
    )

    assert captured["path"] == "/license/activate"
    assert captured["payload"] == {
        "code": "activation-code",
        "instance_id": "instance-id",
        "edition": "pro",
        "instance_label": None,
        "base_version": __version__,
    }


@pytest.mark.asyncio
async def test_refresh_reports_base_version_to_artifact_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(path: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update({"path": path, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(license_provider, "_post_provider", fake_post)

    await license_provider.refresh_remote_license(
        license_token="signed-license",
        instance_id="instance-id",
    )

    assert captured["path"] == "/license/refresh"
    assert captured["payload"] == {
        "license": "signed-license",
        "instance_id": "instance-id",
        "base_version": __version__,
    }
