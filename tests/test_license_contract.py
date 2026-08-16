from app.services.license_contract import get_license_provider_contract
from app.core.config import settings


def test_license_contract_contains_required_env_and_remote_endpoints() -> None:
    previous_url = settings.MOVARY_LICENSE_SERVER_URL
    previous_key = settings.MOVARY_LICENSE_PUBLIC_KEY
    previous_key_id = settings.MOVARY_LICENSE_KEY_ID
    settings.MOVARY_LICENSE_SERVER_URL = None
    settings.MOVARY_LICENSE_PUBLIC_KEY = None
    settings.MOVARY_LICENSE_KEY_ID = None
    try:
        contract = get_license_provider_contract()
        assert contract["required_env"] == [
            "MOVARY_LICENSE_SERVER_URL",
            "MOVARY_LICENSE_PUBLIC_KEY",
            "MOVARY_LICENSE_KEY_ID",
        ]
        assert [item["name"] for item in contract["remote_endpoints"]] == [
            "activate",
            "refresh",
            "deactivate",
        ]
        assert contract["provider_ready"] is False
    finally:
        settings.MOVARY_LICENSE_SERVER_URL = previous_url
        settings.MOVARY_LICENSE_PUBLIC_KEY = previous_key
        settings.MOVARY_LICENSE_KEY_ID = previous_key_id
