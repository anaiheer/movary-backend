from app.services.license_provider import get_license_provider_status
from app.core.config import settings


def test_license_provider_defaults_to_local_stub_when_not_configured() -> None:
    previous_url = settings.MOVARY_LICENSE_SERVER_URL
    previous_key = settings.MOVARY_LICENSE_PUBLIC_KEY
    previous_key_id = settings.MOVARY_LICENSE_KEY_ID
    settings.MOVARY_LICENSE_SERVER_URL = None
    settings.MOVARY_LICENSE_PUBLIC_KEY = None
    settings.MOVARY_LICENSE_KEY_ID = None
    try:
        status = get_license_provider_status()
        assert status["mode"] == "online_signed"
        assert status["ready"] is False
        assert status["missing_fields"] == ["server_url", "public_key", "key_id"]
    finally:
        settings.MOVARY_LICENSE_SERVER_URL = previous_url
        settings.MOVARY_LICENSE_PUBLIC_KEY = previous_key
        settings.MOVARY_LICENSE_KEY_ID = previous_key_id


def test_license_provider_reports_online_signed_when_fully_configured() -> None:
    previous_url = settings.MOVARY_LICENSE_SERVER_URL
    previous_key = settings.MOVARY_LICENSE_PUBLIC_KEY
    previous_key_id = settings.MOVARY_LICENSE_KEY_ID
    settings.MOVARY_LICENSE_SERVER_URL = "https://license.example.com"
    settings.MOVARY_LICENSE_PUBLIC_KEY = "PUBLIC-KEY"
    settings.MOVARY_LICENSE_KEY_ID = "key-01"
    try:
        status = get_license_provider_status()
        assert status["mode"] == "online_signed"
        assert status["ready"] is True
        assert status["missing_fields"] == []
        assert status["server_url"] == "https://license.example.com"
        assert status["key_id"] == "key-01"
    finally:
        settings.MOVARY_LICENSE_SERVER_URL = previous_url
        settings.MOVARY_LICENSE_PUBLIC_KEY = previous_key
        settings.MOVARY_LICENSE_KEY_ID = previous_key_id
