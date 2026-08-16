import pytest
import httpx
from starlette.requests import Request


@pytest.mark.asyncio
async def test_person_credit_results_dedupe_and_sort_by_popularity(monkeypatch):
    from app.services import tmdb as tmdb_service

    async def fake_fetch_json(_db, path, params):
        assert path == "/person/42/combined_credits"
        assert params["language"] == "en-US"
        return {
            "cast": [
                {
                    "id": 1,
                    "media_type": "movie",
                    "title": "Low",
                    "release_date": "2001-01-01",
                    "popularity": 1,
                    "vote_average": 6,
                },
                {
                    "id": 2,
                    "media_type": "tv",
                    "name": "High",
                    "first_air_date": "2002-01-01",
                    "popularity": 9,
                    "vote_average": 8,
                },
                {
                    "id": 2,
                    "media_type": "tv",
                    "name": "Duplicate",
                    "first_air_date": "2002-01-01",
                    "popularity": 99,
                },
                {"id": 3, "media_type": "person", "name": "Ignored"},
            ]
        }

    monkeypatch.setattr(tmdb_service, "_fetch_json", fake_fetch_json)

    results, ok = await tmdb_service._fetch_person_credit_results(
        None, 42, 1, None, None, None, "en-US"
    )

    assert ok is True
    assert [item["id"] for item in results] == ["tmdb:2", "tmdb:1"]
    assert results[0]["media_type"] == "TV"
    assert "_popularity" not in results[0]


@pytest.mark.asyncio
async def test_person_credit_results_filters_media_type_and_year(monkeypatch):
    from app.services import tmdb as tmdb_service

    async def fake_fetch_json(_db, _path, _params):
        return {
            "cast": [
                {
                    "id": 10,
                    "media_type": "movie",
                    "title": "Movie 2001",
                    "release_date": "2001-05-01",
                    "vote_average": 4,
                },
                {
                    "id": 11,
                    "media_type": "movie",
                    "title": "Movie 2002",
                    "release_date": "2002-05-01",
                    "vote_average": 9,
                },
                {
                    "id": 12,
                    "media_type": "tv",
                    "name": "TV 2002",
                    "first_air_date": "2002-05-01",
                    "vote_average": 10,
                },
            ]
        }

    monkeypatch.setattr(tmdb_service, "_fetch_json", fake_fetch_json)

    results, ok = await tmdb_service._fetch_person_credit_results(
        None, 42, 1, "movie", "vote_average.desc", 2002, "zh-CN"
    )

    assert ok is True
    assert [item["id"] for item in results] == ["tmdb:11"]
    assert results[0]["title"] == "Movie 2002"


@pytest.mark.asyncio
async def test_discover_results_passes_keyword_filter(monkeypatch):
    from app.services import tmdb as tmdb_service

    async def fake_fetch_json(_db, path, params):
        assert path == "/discover/movie"
        assert params["with_keywords"] == 99
        assert params["language"] == "zh-CN"
        return {
            "results": [
                {
                    "id": 7,
                    "media_type": "movie",
                    "title": "Keyword Movie",
                    "release_date": "2024-01-01",
                }
            ]
        }

    monkeypatch.setattr(tmdb_service, "_fetch_json", fake_fetch_json)

    results, ok = await tmdb_service._fetch_discover_results(
        None, "trending", 1, "movie", None, None, None, 99, None, None, "zh-CN"
    )

    assert ok is True
    assert results[0]["id"] == "tmdb:7"


@pytest.mark.asyncio
async def test_vod_detail_includes_tv_episodes_and_cached_backdrops(monkeypatch):
    from app.api.routes import vod as vod_route

    cached_payload = {}

    async def fake_get_cached_data(_db, _prefix, _params):
        return None

    async def fake_set_cached_data(_db, prefix, params, payload):
        cached_payload["prefix"] = prefix
        cached_payload["params"] = params
        cached_payload["payload"] = payload

    async def fake_get_config(_db):
        return "https://api.themoviedb.org/3", "tmdb-key", None

    async def fake_request_tmdb(url, params, proxy_url=None, timeout=15):
        assert params["api_key"] == "tmdb-key"
        if url.endswith("/tv/123"):
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "name": "Demo Show",
                    "first_air_date": "2024-01-01",
                    "poster_path": "/poster.jpg",
                    "backdrop_path": "/hero.jpg",
                    "genres": [],
                    "credits": {"cast": [], "crew": []},
                    "recommendations": {"results": []},
                    "keywords": {"results": []},
                    "images": {
                        "backdrops": [
                            {
                                "file_path": "/still-a.jpg",
                                "width": 1920,
                                "height": 1080,
                                "vote_average": 7.5,
                            }
                        ]
                    },
                    "seasons": [
                        {
                            "id": 1,
                            "name": "Season 1",
                            "season_number": 1,
                            "episode_count": 1,
                            "air_date": "2024-01-01",
                            "poster_path": "/season.jpg",
                        }
                    ],
                },
            )
        if url.endswith("/tv/123/season/1"):
            return httpx.Response(
                200,
                json={
                    "episodes": [
                        {
                            "id": 10,
                            "name": "Pilot",
                            "episode_number": 1,
                            "overview": "First episode",
                            "air_date": "2024-01-01",
                            "still_path": "/episode.jpg",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected TMDB URL: {url}")

    monkeypatch.setattr(vod_route.tmdb_service, "get_cached_data", fake_get_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "set_cached_data", fake_set_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "get_config", fake_get_config)
    monkeypatch.setattr(vod_route.tmdb_service, "request_tmdb", fake_request_tmdb)

    request = Request({"type": "http", "headers": [(b"accept-language", b"zh-CN")]})
    payload = await vod_route.get_detail(request, media_type="tv", tmdb_id=123, db=None)

    assert payload["seasons"][0]["episodes"][0]["name"] == "Pilot"
    assert payload["seasons"][0]["episodes"][0]["still_url"].startswith("/api/v1/vod/image?")
    assert payload["backdrops"][0]["image_url"].startswith("/api/v1/vod/image?")
    assert payload["backdrops"][0]["image_url_large"].startswith("/api/v1/vod/image?")
    assert cached_payload["prefix"] == "detail"
    assert cached_payload["payload"]["seasons"][0]["episodes"]


@pytest.mark.asyncio
async def test_vod_detail_season_episode_retry_on_transient_error(monkeypatch):
    """Season episode fetch retries on transient network error, then succeeds."""
    from app.api.routes import vod as vod_route

    cached_payload = {}

    async def fake_get_cached_data(_db, _prefix, _params):
        return None

    async def fake_set_cached_data(_db, prefix, params, payload):
        cached_payload["prefix"] = prefix
        cached_payload["params"] = params
        cached_payload["payload"] = payload

    async def fake_get_config(_db):
        return "https://api.themoviedb.org/3", "tmdb-key", None

    call_count = 0

    async def fake_request_tmdb(url, params, proxy_url=None, timeout=15):
        nonlocal call_count
        if url.endswith("/tv/200"):
            return httpx.Response(
                200,
                json={
                    "id": 200,
                    "name": "Retry Show",
                    "first_air_date": "2024-01-01",
                    "poster_path": None,
                    "backdrop_path": None,
                    "genres": [],
                    "credits": {"cast": [], "crew": []},
                    "recommendations": {"results": []},
                    "keywords": {"results": []},
                    "images": {"backdrops": []},
                    "seasons": [
                        {
                            "id": 1,
                            "name": "Season 1",
                            "season_number": 1,
                            "episode_count": 2,
                            "air_date": "2024-01-01",
                            "poster_path": None,
                        }
                    ],
                },
            )
        if url.endswith("/tv/200/season/1"):
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("transient connection refused")
            return httpx.Response(
                200,
                json={
                    "episodes": [
                        {
                            "id": 20,
                            "name": "Retry Episode",
                            "episode_number": 1,
                            "overview": "After retry",
                            "air_date": "2024-01-01",
                            "still_path": None,
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected TMDB URL: {url}")

    monkeypatch.setattr(vod_route.tmdb_service, "get_cached_data", fake_get_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "set_cached_data", fake_set_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "get_config", fake_get_config)
    monkeypatch.setattr(vod_route.tmdb_service, "request_tmdb", fake_request_tmdb)

    request = Request({"type": "http", "headers": [(b"accept-language", b"zh-CN")]})
    payload = await vod_route.get_detail(request, media_type="tv", tmdb_id=200, db=None)

    assert call_count == 2  # first attempt failed, second succeeded
    assert payload["seasons"][0]["episodes"][0]["name"] == "Retry Episode"
    assert cached_payload["prefix"] == "detail"


@pytest.mark.asyncio
async def test_vod_detail_season_partial_failure_skips_cache(monkeypatch):
    """When one season fetch fails after all retries, cache write is skipped."""
    from app.api.routes import vod as vod_route

    cached_payload = {}

    async def fake_get_cached_data(_db, _prefix, _params):
        return None

    async def fake_set_cached_data(_db, prefix, params, payload):
        cached_payload["prefix"] = prefix
        cached_payload["params"] = params
        cached_payload["payload"] = payload

    async def fake_get_config(_db):
        return "https://api.themoviedb.org/3", "tmdb-key", None

    async def fake_request_tmdb(url, params, proxy_url=None, timeout=15):
        if url.endswith("/tv/300"):
            return httpx.Response(
                200,
                json={
                    "id": 300,
                    "name": "Partial Fail Show",
                    "first_air_date": "2024-01-01",
                    "poster_path": None,
                    "backdrop_path": None,
                    "genres": [],
                    "credits": {"cast": [], "crew": []},
                    "recommendations": {"results": []},
                    "keywords": {"results": []},
                    "images": {"backdrops": []},
                    "seasons": [
                        {
                            "id": 1,
                            "name": "Season 1",
                            "season_number": 1,
                            "episode_count": 5,
                            "air_date": "2024-01-01",
                            "poster_path": None,
                        },
                        {
                            "id": 2,
                            "name": "Season 2",
                            "season_number": 2,
                            "episode_count": 3,
                            "air_date": "2025-01-01",
                            "poster_path": None,
                        },
                    ],
                },
            )
        if url.endswith("/tv/300/season/1"):
            return httpx.Response(
                200,
                json={
                    "episodes": [
                        {
                            "id": 30,
                            "name": "S1E1",
                            "episode_number": 1,
                            "overview": "ok",
                            "air_date": "2024-01-01",
                            "still_path": None,
                        }
                    ]
                },
            )
        if url.endswith("/tv/300/season/2"):
            # Always fail — exhausts all retries
            raise httpx.ConnectError("season 2 unavailable")
        raise AssertionError(f"unexpected TMDB URL: {url}")

    monkeypatch.setattr(vod_route.tmdb_service, "get_cached_data", fake_get_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "set_cached_data", fake_set_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "get_config", fake_get_config)
    monkeypatch.setattr(vod_route.tmdb_service, "request_tmdb", fake_request_tmdb)

    request = Request({"type": "http", "headers": [(b"accept-language", b"zh-CN")]})
    payload = await vod_route.get_detail(request, media_type="tv", tmdb_id=300, db=None)

    # Season 1 has episodes, season 2 is empty (failed)
    assert len(payload["seasons"]) == 2
    s1 = next(s for s in payload["seasons"] if s["season_number"] == 1)
    s2 = next(s for s in payload["seasons"] if s["season_number"] == 2)
    assert len(s1["episodes"]) == 1
    assert s1["episodes"][0]["name"] == "S1E1"
    assert s2["episodes"] == []

    # Cache must NOT have been written (partial failure)
    assert "prefix" not in cached_payload


@pytest.mark.asyncio
async def test_vod_detail_movie_always_caches(monkeypatch):
    """Movies should always write cache regardless of _all_seasons_ok."""
    from app.api.routes import vod as vod_route

    cached_payload = {}

    async def fake_get_cached_data(_db, _prefix, _params):
        return None

    async def fake_set_cached_data(_db, prefix, params, payload):
        cached_payload["prefix"] = prefix
        cached_payload["params"] = params
        cached_payload["payload"] = payload

    async def fake_get_config(_db):
        return "https://api.themoviedb.org/3", "tmdb-key", None

    async def fake_request_tmdb(url, params, proxy_url=None, timeout=15):
        if url.endswith("/movie/400"):
            return httpx.Response(
                200,
                json={
                    "id": 400,
                    "title": "Test Movie",
                    "release_date": "2024-06-01",
                    "poster_path": None,
                    "backdrop_path": None,
                    "genres": [],
                    "credits": {"cast": [], "crew": []},
                    "recommendations": {"results": []},
                    "keywords": {"results": []},
                    "images": {"backdrops": []},
                    "production_companies": [],
                    "production_countries": [],
                },
            )
        raise AssertionError(f"unexpected TMDB URL: {url}")

    monkeypatch.setattr(vod_route.tmdb_service, "get_cached_data", fake_get_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "set_cached_data", fake_set_cached_data)
    monkeypatch.setattr(vod_route.tmdb_service, "get_config", fake_get_config)
    monkeypatch.setattr(vod_route.tmdb_service, "request_tmdb", fake_request_tmdb)

    request = Request({"type": "http", "headers": [(b"accept-language", b"zh-CN")]})
    payload = await vod_route.get_detail(request, media_type="movie", tmdb_id=400, db=None)

    assert payload["media_type"] == "MOVIE"
    assert payload["seasons"] == []
    assert cached_payload["prefix"] == "detail"
