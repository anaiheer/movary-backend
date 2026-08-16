from app.core.license_status import build_license_overview


def test_license_overview_defaults_to_base_when_no_pro_extension_is_enabled() -> None:
    overview = build_license_overview({"enabled": [], "loaded": [], "failed": []})
    assert overview["edition"] == "base"
    assert overview["status"] == "BASE"


def test_license_overview_reports_ready_when_pro_extension_is_loaded() -> None:
    overview = build_license_overview(
        {"enabled": ["pro"], "loaded": [{"name": "pro"}], "failed": []}
    )
    assert overview["edition"] == "base"
    assert overview["status"] == "PENDING"


def test_license_overview_reports_error_when_pro_extension_fails() -> None:
    overview = build_license_overview(
        {"enabled": ["pro"], "loaded": [], "failed": [{"name": "pro", "error": "boom"}]}
    )
    assert overview["edition"] == "base"
    assert overview["status"] == "ERROR"


def test_license_overview_reports_active_when_stub_activation_and_pro_loaded() -> None:
    overview = build_license_overview(
        {"enabled": ["pro"], "loaded": [{"name": "pro"}], "failed": []},
        {"activation_present": True, "license_status": "active"},
    )
    assert overview["edition"] == "pro"
    assert overview["status"] == "ACTIVE"


def test_license_overview_reports_expired_license() -> None:
    overview = build_license_overview(
        {"enabled": ["pro"], "loaded": [], "failed": []},
        {"activation_present": True, "license_status": "expired"},
    )
    assert overview["edition"] == "base"
    assert overview["status"] == "EXPIRED"
