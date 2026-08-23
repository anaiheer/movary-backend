from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.license_config import license_settings


PUBLIC_STATUS_FIELDS = {
    "edition",
    "status",
    "message",
    "manage_url",
    "activation_present",
    "activation_code_hint",
    "activated_at",
    "last_refresh_at",
    "expires_at",
    "instance_id",
    "instance_label",
    "license_id",
    "package_code",
    "package_name",
}

INTERNAL_STATUS_FIELDS = {
    "activation_mode",
    "provider_mode",
    "provider_ready",
    "provider_reachable",
    "provider_health_message",
    "provider_server_url",
    "provider_key_id",
    "provider_missing_fields",
    "pro_effective",
    "backend_artifact_version",
    "frontend_artifact_version",
    "backend_artifact_status",
    "backend_artifact_error",
    "frontend_artifact_status",
    "frontend_artifact_error",
    "frontend_artifact_entry_url",
    "frontend_artifact_style_url",
    "extension_enabled",
    "extension_loaded",
    "extension_failed",
    "loaded_extensions",
    "failed_extensions",
}


@pytest.mark.asyncio
async def test_admin_license_status_only_exposes_customer_fields(
    async_client, admin_token, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(license_settings, "state_file", str(tmp_path / "license-state.json"))
    monkeypatch.setattr(
        license_settings,
        "instance_file",
        str(tmp_path / "license-instance.json"),
    )

    response = await async_client.get(
        "/api/v1/admin/license/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert set(payload) == PUBLIC_STATUS_FIELDS
    assert INTERNAL_STATUS_FIELDS.isdisjoint(payload)


@pytest.mark.asyncio
async def test_admin_license_provider_contract_is_not_public(async_client, admin_token) -> None:
    response = await async_client.get(
        "/api/v1/admin/license/provider-contract",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 404


def test_public_env_example_hides_license_infrastructure_settings() -> None:
    env_example = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")

    for variable in (
        "MOVARY_LICENSE_SERVER_URL",
        "MOVARY_LICENSE_PUBLIC_KEY",
        "MOVARY_LICENSE_KEY_ID",
        "MOVARY_LICENSE_INSTANCE_LABEL",
        "MOVARY_LICENSE_REQUEST_TIMEOUT",
        "MOVARY_PRO_FRONTEND_ARTIFACT_LOCAL_ENTRY",
    ):
        assert variable not in env_example


def test_general_settings_do_not_expose_license_infrastructure() -> None:
    assert not any(name.startswith("MOVARY_LICENSE_") for name in Settings.model_fields)
