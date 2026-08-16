from app.core.edition import get_default_capabilities


def test_default_capabilities_match_base_contract() -> None:
    capabilities = get_default_capabilities()

    assert capabilities.edition == "base"
    assert capabilities.license_status == "inactive"
    assert capabilities.limits["max_emby_servers"] == 1
    assert capabilities.limits["max_moviepilot_servers"] == 1
    assert capabilities.features["simple_server_management"] is True
    assert capabilities.features["subscription_groups"] is False
    assert capabilities.features["group_upgrade"] is False
