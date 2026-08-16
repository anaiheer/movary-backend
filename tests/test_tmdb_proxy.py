import httpx
import pytest

from app.services import tmdb as tmdb_service


def test_proxy_candidates_keep_original_outside_container(monkeypatch):
    monkeypatch.setattr(tmdb_service, "_running_in_container", lambda: False)
    assert tmdb_service._proxy_candidates("http://127.0.0.1:7897") == ["http://127.0.0.1:7897"]


def test_proxy_candidates_expand_local_proxy_inside_container(monkeypatch):
    monkeypatch.setattr(tmdb_service, "_running_in_container", lambda: True)
    assert tmdb_service._proxy_candidates("http://127.0.0.1:7897") == [
        "http://127.0.0.1:7897",
        "http://host.docker.internal:7897",
        "http://gateway.docker.internal:7897",
    ]


def test_proxy_candidates_ignore_remote_proxy_inside_container(monkeypatch):
    monkeypatch.setattr(tmdb_service, "_running_in_container", lambda: True)
    assert tmdb_service._proxy_candidates("http://10.0.0.8:7897") == ["http://10.0.0.8:7897"]


def test_proxy_candidates_keep_auth_when_rewriting_host(monkeypatch):
    monkeypatch.setattr(tmdb_service, "_running_in_container", lambda: True)
    assert tmdb_service._proxy_candidates("http://user:pass@127.0.0.1:7897") == [
        "http://user:pass@127.0.0.1:7897",
        "http://user:pass@host.docker.internal:7897",
        "http://user:pass@gateway.docker.internal:7897",
    ]


@pytest.mark.asyncio
async def test_request_tmdb_retries_without_environment_proxy(monkeypatch):
    client_options = []

    class FakeAsyncClient:
        def __init__(self, **options):
            client_options.append(options)
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def get(self, url, params=None):
            if self.options.get("trust_env") is False:
                return httpx.Response(200, request=httpx.Request("GET", url), json={"ok": True})
            raise httpx.ConnectError(
                "environment proxy unavailable",
                request=httpx.Request("GET", url, params=params),
            )

    monkeypatch.setattr(tmdb_service.httpx, "AsyncClient", FakeAsyncClient)

    response = await tmdb_service.request_tmdb("https://example.test/tmdb")

    assert response.status_code == 200
    assert len(client_options) == 2
    assert "trust_env" not in client_options[0]
    assert client_options[1]["trust_env"] is False
