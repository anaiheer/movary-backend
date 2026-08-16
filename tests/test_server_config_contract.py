from app.schemas.server_config import (
    ManagedEmbyServerUpsert,
    ManagedMoviePilotServerUpsert,
    ServerConfigSummary,
)


def test_server_summary_defaults_match_single_server_policy() -> None:
    summary = ServerConfigSummary(emby_count=0, moviepilot_count=0)

    assert summary.max_emby_servers == 1
    assert summary.max_moviepilot_servers == 1
    assert summary.pro_data_detected is False


def test_server_upsert_contract_accepts_lightweight_fields() -> None:
    emby = ManagedEmbyServerUpsert(
        name="Main Emby",
        base_url="https://emby.example.com",
        external_url="https://watch.example.com",
        api_key="secret-key",
        webhook_url="https://emby.example.com/webhook",
        is_active=True,
    )
    moviepilot = ManagedMoviePilotServerUpsert(
        name="Main MoviePilot",
        base_url="https://moviepilot.example.com",
        api_token="token-123",
        is_active=True,
    )

    assert str(emby.base_url) == "https://emby.example.com/"
    assert str(moviepilot.base_url) == "https://moviepilot.example.com/"
