import inspect

from app.services import moviepilot


def test_moviepilot_requests_require_a_database_resolved_base_url() -> None:
    parameter = inspect.signature(moviepilot.subscribe_vod).parameters["base_url"]

    assert parameter.default is inspect.Parameter.empty


def test_moviepilot_headers_do_not_fall_back_to_environment_settings(monkeypatch) -> None:
    legacy_settings = getattr(moviepilot, "settings", None)
    if legacy_settings is not None:
        monkeypatch.setattr(legacy_settings, "MOVIEPILOT_API_TOKEN", "legacy-token")

    headers = moviepilot._build_headers(None)

    assert headers == {"Content-Type": "application/json"}
