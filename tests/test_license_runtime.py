from app.core.config import settings
from app.services.license_runtime import (
    clear_cached_license,
    get_or_create_instance_identity,
    load_license_state,
    save_license_state,
)


def test_license_runtime_identity_and_clear_roundtrip(tmp_path) -> None:
    state_file = tmp_path / "license-state.json"
    instance_file = tmp_path / "license-instance.json"

    initial = load_license_state(state_file)
    assert initial["activation_present"] is False

    identity = get_or_create_instance_identity(instance_file)
    assert identity["instance_id"]

    saved = save_license_state(
        {
            "activation_present": True,
            "activation_code_hint": "***1234",
            "activated_at": "2026-01-01T00:00:00Z",
            "last_refresh_at": "2026-01-01T00:00:00Z",
            "license": "signed-license",
            "expires_at": "2026-02-01T00:00:00Z",
            "key_id": "key-01",
            "package_code": "pro-monthly",
            "package_name": "Pro Monthly",
            "license_id": "lic-01",
            "edition": "pro",
        },
        state_file,
    )
    assert saved["activation_present"] is True
    assert saved["package_name"] == "Pro Monthly"

    cleared = clear_cached_license(state_file)
    assert cleared["activation_present"] is False
    assert cleared["package_name"] is None


def test_instance_identity_does_not_use_loopback_frontend_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "http://localhost:5173")
    monkeypatch.setattr("app.services.license_runtime.socket.gethostname", lambda: "movary-host")

    identity = get_or_create_instance_identity(tmp_path / "license-instance.json")

    assert identity["instance_label"] == "movary-host"


def test_instance_identity_accepts_and_persists_public_origin(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "http://localhost:5173")
    instance_file = tmp_path / "license-instance.json"
    initial = get_or_create_instance_identity(instance_file)

    updated = get_or_create_instance_identity(
        instance_file,
        instance_label="https://movary.example.com/",
    )
    reloaded = get_or_create_instance_identity(instance_file)

    assert updated["instance_id"] == initial["instance_id"]
    assert updated["instance_label"] == "https://movary.example.com"
    assert reloaded == updated
