import httpx
import pytest
from sqlalchemy import delete


@pytest.mark.asyncio
async def test_tmdb_request_uses_httpx_028_proxy_argument(monkeypatch):
    from app.services import tmdb as tmdb_service

    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(
            self,
            *,
            timeout: int,
            proxy: str | None = None,
            trust_env: bool = True,
        ) -> None:
            captured.update(timeout=timeout, proxy=proxy, trust_env=trust_env)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def get(self, url: str, *, params=None) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("GET", url), json={"ok": True})

    monkeypatch.setattr(tmdb_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(tmdb_service, "_running_in_container", lambda: False)

    response = await tmdb_service.request_tmdb(
        "https://api.themoviedb.org/3/configuration",
        proxy_url="http://127.0.0.1:7890",
        timeout=20,
    )

    assert response.status_code == 200
    assert captured == {
        "timeout": 20,
        "proxy": "http://127.0.0.1:7890",
        "trust_env": True,
    }


@pytest.mark.asyncio
async def test_search_cache_normalizes_keyword(monkeypatch):
    from app.db.session import AsyncSessionLocal
    from app.models.tmdb_cache import TmdbCache
    from app.services import tmdb as tmdb_service

    fetch_calls = 0

    async def fake_fetch(_db, keyword, language=None):
        nonlocal fetch_calls
        fetch_calls += 1
        return [{"id": f"tmdb:{keyword}:{language or 'default'}"}]

    monkeypatch.setattr(tmdb_service, "_fetch_search_results", fake_fetch)

    async with AsyncSessionLocal() as session:
        await session.execute(delete(TmdbCache))
        await session.commit()

        first = await tmdb_service.search(session, "  the   matrix  ", "en-US")
        second = await tmdb_service.search(session, "the matrix", "en-US")

    assert first == second
    assert fetch_calls == 1


@pytest.mark.asyncio
async def test_search_cache_isolated_by_language(monkeypatch):
    from app.db.session import AsyncSessionLocal
    from app.models.tmdb_cache import TmdbCache
    from app.services import tmdb as tmdb_service

    fetch_calls = 0

    async def fake_fetch(_db, keyword, language=None):
        nonlocal fetch_calls
        fetch_calls += 1
        return [{"id": f"tmdb:{keyword}:{language}"}]

    monkeypatch.setattr(tmdb_service, "_fetch_search_results", fake_fetch)

    async with AsyncSessionLocal() as session:
        await session.execute(delete(TmdbCache))
        await session.commit()

        chinese = await tmdb_service.search(session, "the matrix", "zh-CN")
        english = await tmdb_service.search(session, "the matrix", "en-US")

    assert chinese != english
    assert fetch_calls == 2


@pytest.mark.asyncio
async def test_empty_genre_cache_entry_is_refreshed():
    from app.db.session import AsyncSessionLocal
    from app.models.tmdb_cache import TmdbCache
    from app.services import tmdb as tmdb_service

    fetch_calls = 0

    async def fetch_payload():
        nonlocal fetch_calls
        fetch_calls += 1
        return {"results": [{"id": 28, "name": "Action"}]}

    async with AsyncSessionLocal() as session:
        await session.execute(delete(TmdbCache))
        await session.commit()

        await tmdb_service.set_cached_data(
            session,
            "genres",
            {"media_type": "movie", "language": "en-US"},
            {"results": []},
        )
        refreshed = await tmdb_service.get_or_set_cached_data(
            session,
            "genres",
            {"media_type": "movie", "language": "en-US"},
            fetch_payload,
        )
        cached = await tmdb_service.get_or_set_cached_data(
            session,
            "genres",
            {"media_type": "movie", "language": "en-US"},
            fetch_payload,
        )

    assert refreshed == {"results": [{"id": 28, "name": "Action"}]}
    assert cached == refreshed
    assert fetch_calls == 1
