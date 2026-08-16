from app.core.config import Settings


def test_local_config_defaults_use_local_services(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.DATABASE_URL == "postgresql://postgres:password@localhost:5432/movary"
    assert settings.REDIS_URL == "redis://localhost:6379/0"
