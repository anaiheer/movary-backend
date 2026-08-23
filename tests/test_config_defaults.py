from app.core.config import Settings


def test_local_config_defaults_use_local_services(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.DATABASE_URL == "postgresql://postgres:password@localhost:5432/movary"
    assert settings.REDIS_URL == "redis://localhost:6379/0"


def test_server_connections_are_not_environment_settings() -> None:
    obsolete_fields = {
        "EMBY_BASE_URLS",
        "EMBY_API_KEYS",
        "EMBY_DEFAULT_POLICY",
        "MOVIEPILOT_BASE_URL",
        "MOVIEPILOT_USERNAME",
        "MOVIEPILOT_PASSWORD",
        "MOVIEPILOT_API_TOKEN",
        "MOVARY_BACKEND_EXTENSIONS",
        "MOVARY_BACKEND_PRO_PATH",
    }

    assert obsolete_fields.isdisjoint(Settings.model_fields)
    assert "EMBY_PASSWORD_KEY" in Settings.model_fields
