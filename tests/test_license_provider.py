from hashlib import sha256

import pytest
from cryptography.hazmat.primitives import serialization

from app import __version__
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
