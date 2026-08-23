from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

import httpx
from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.system_settings import SystemSettings
from app.models.tmdb_cache import TmdbCache
from app.services.cache import delete_prefix, get_cache_client, get_json, get_many_json, set_json
from app.services.site_languages import normalize_site_language

TMDB_IMAGE_ROOT = "https://image.tmdb.org/t/p"
TMDB_IMAGE_VARIANTS = {
    "poster": "w342",
    "poster-lg": "w500",
    "backdrop": "w780",
    "backdrop-lg": "w1280",
    "logo": "w300",
    "profile": "w185",
    "still": "w300",
}

DEFAULT_DISCOVER_CATEGORIES = [
    "trending",
    "movie_popular",
    "movie_upcoming",
    "tv_popular",
    "tv_on_the_air",
]

REDIS_CACHE_PREFIX = "tmdb:data:"
REDIS_STORAGE_TTL_SECONDS = 21600
REDIS_SYNC_SCAN_COUNT = 100
_CACHE_FETCH_LOCKS: dict[str, asyncio.Lock] = {}
_DOCKER_PROXY_HOSTS = ("host.docker.internal", "gateway.docker.internal")


def _build_cache_key(prefix: str, params: dict[str, Any]) -> str:
    packed = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return f"tmdb:{prefix}:{packed}"


def _get_cache_lock(cache_key: str) -> asyncio.Lock:
    lock = _CACHE_FETCH_LOCKS.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _CACHE_FETCH_LOCKS[cache_key] = lock
    return lock


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def _is_local_proxy_host(hostname: str | None) -> bool:
    normalized = str(hostname or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _proxy_candidates(proxy_url: str | None) -> list[str | None]:
    normalized = str(proxy_url or "").strip()
    if not normalized:
        return [None]

    candidates: list[str | None] = [normalized]
    if not _running_in_container():
        return candidates

    parts = urlsplit(normalized)
    if not _is_local_proxy_host(parts.hostname):
        return candidates

    for host in _DOCKER_PROXY_HOSTS:
        auth = ""
        if parts.username:
            auth = parts.username
            if parts.password:
                auth = f"{auth}:{parts.password}"
            auth = f"{auth}@"
        rewritten = urlunsplit(
            (
                parts.scheme,
                f"{auth}{host}" if parts.port is None else f"{auth}{host}:{parts.port}",
                parts.path,
                parts.query,
                parts.fragment,
            )
        )
        if rewritten not in candidates:
            candidates.append(rewritten)
    return candidates


async def request_tmdb(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    timeout: int = 15,
) -> httpx.Response:
    last_error: httpx.HTTPError | None = None
    for candidate in _proxy_candidates(proxy_url):
        try:
            if candidate:
                async with httpx.AsyncClient(timeout=timeout, proxy=candidate) as client:
                    return await client.get(url, params=params)
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.get(url, params=params)
        except httpx.HTTPError as exc:
            last_error = exc

    # Always try a clean direct connection as final fallback,
    # regardless of whether an explicit proxy_url is configured.
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            return await client.get(url, params=params)
    except httpx.HTTPError as exc:
        last_error = exc

    if last_error is not None:
        raise last_error
    raise httpx.HTTPError("TMDB request failed")


def _normalize_search_keyword(keyword: str) -> str:
    return re.sub(r"\s+", " ", keyword).strip()


def build_data_cache_key(prefix: str, params: dict[str, Any]) -> str:
    return _build_cache_key(prefix, params)


def get_image_base(variant: str | None = None) -> str:
    size = TMDB_IMAGE_VARIANTS.get(variant or "poster", TMDB_IMAGE_VARIANTS["poster"])
    return f"{TMDB_IMAGE_ROOT}/{size}"


def build_image_url(path: str | None, variant: str | None = None) -> str | None:
    if not path:
        return None
    normalized = path if path.startswith("/") else f"/{path}"
    base = f"{settings.API_V1_STR}/vod/image?path={quote(normalized, safe='')}"
    if variant:
        base = f"{base}&variant={quote(variant, safe='')}"
    return base


def resolve_tmdb_language(language: str | None = None) -> str:
    return normalize_site_language(language)


def build_image_language_preference(language: str | None = None) -> str:
    normalized = resolve_tmdb_language(language)
    if normalized == "en-US":
        return "en-US,en,null"
    return "zh-CN,zh,null"


async def _get_system_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _resolve_tmdb_config(row: SystemSettings) -> tuple[str, str | None, str | None]:
    base_url = (row.tmdb_base_url or settings.TMDB_BASE_URL).rstrip("/")
    api_key = row.tmdb_api_key or settings.TMDB_API_KEY
    proxy_url = row.tmdb_proxy_url or settings.TMDB_PROXY_URL
    return base_url, api_key, proxy_url


async def get_config(db: AsyncSession) -> tuple[str, str | None, str | None]:
    row = await _get_system_settings(db)
    return _resolve_tmdb_config(row)


def _redis_cache_key(cache_key: str) -> str:
    return f"{REDIS_CACHE_PREFIX}{cache_key}"


async def _prime_redis_cache(cache_key: str, payload: dict[str, Any]) -> None:
    await set_json(_redis_cache_key(cache_key), payload, REDIS_STORAGE_TTL_SECONDS)


async def _get_cached_payload(db: AsyncSession, cache_key: str) -> dict[str, Any] | None:
    cached = await get_json(_redis_cache_key(cache_key))
    if isinstance(cached, dict):
        return cached

    row = await db.scalar(select(TmdbCache).where(TmdbCache.cache_key == cache_key))
    if not row or not isinstance(row.payload, dict):
        return None

    await _prime_redis_cache(cache_key, row.payload)
    return row.payload


async def get_cached_data(
    db: AsyncSession,
    prefix: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    return await _get_cached_payload(db, _build_cache_key(prefix, params))


async def get_or_set_cached_data(
    db: AsyncSession,
    prefix: str,
    params: dict[str, Any],
    fetcher,
) -> dict[str, Any] | None:
    cache_key = _build_cache_key(prefix, params)
    cached = await _get_cached_payload(db, cache_key)
    if cached is not None and not _should_refresh_cached_payload(prefix, cached):
        return cached

    lock = _get_cache_lock(cache_key)
    async with lock:
        cached = await _get_cached_payload(db, cache_key)
        if cached is not None and not _should_refresh_cached_payload(prefix, cached):
            return cached

        payload = await fetcher()
        if payload is None:
            return None

        await _set_cache(db, cache_key, payload)
        return payload


async def _set_cache(db: AsyncSession, cache_key: str, payload: dict[str, Any]) -> None:
    now = datetime.utcnow()
    stmt = (
        insert(TmdbCache)
        .values(
            id=uuid.uuid4(),
            cache_key=cache_key,
            payload=payload,
            expires_at=None,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[TmdbCache.cache_key],
            set_={
                "payload": payload,
                "expires_at": None,
                "updated_at": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
    await _prime_redis_cache(cache_key, payload)


async def set_cached_data(
    db: AsyncSession,
    prefix: str,
    params: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    await _set_cache(db, _build_cache_key(prefix, params), payload)


def _should_refresh_cached_payload(prefix: str, payload: dict[str, Any]) -> bool:
    if prefix != "genres":
        return False
    results = payload.get("results")
    return isinstance(results, list) and len(results) == 0


async def sync_redis_cache_to_db(db: AsyncSession) -> dict[str, int]:
    redis_client = get_cache_client()
    if redis_client is None:
        return {"checked": 0, "updated": 0}

    checked = 0
    updated = 0
    cursor = 0
    now = datetime.utcnow()

    while True:
        cursor, keys = await redis_client.scan(
            cursor=cursor,
            match=f"{REDIS_CACHE_PREFIX}*",
            count=REDIS_SYNC_SCAN_COUNT,
        )
        redis_keys = [
            key for key in keys if isinstance(key, str) and key.startswith(REDIS_CACHE_PREFIX)
        ]
        payloads = await get_many_json(redis_keys)

        rows: list[dict[str, Any]] = []
        for redis_key, payload in zip(redis_keys, payloads, strict=False):
            checked += 1
            if not isinstance(payload, dict):
                continue

            cache_key = redis_key.removeprefix(REDIS_CACHE_PREFIX)
            if not cache_key:
                continue

            rows.append(
                {
                    "id": uuid.uuid4(),
                    "cache_key": cache_key,
                    "payload": payload,
                    "expires_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )

        if rows:
            stmt = insert(TmdbCache).values(rows)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[TmdbCache.cache_key],
                    set_={
                        "payload": stmt.excluded.payload,
                        "expires_at": stmt.excluded.expires_at,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )
            updated += len(rows)

        if int(cursor) == 0:
            break

    if updated:
        await db.commit()

    return {"checked": checked, "updated": updated}


async def _fetch_json(
    db: AsyncSession,
    path: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    system_settings = await _get_system_settings(db)
    base_url, api_key, proxy_url = _resolve_tmdb_config(system_settings)
    if not api_key:
        return None

    request_params = {
        "api_key": api_key,
        **params,
    }
    url = f"{base_url}{path}"
    try:
        resp = await request_tmdb(url, params=request_params, proxy_url=proxy_url, timeout=15)
    except httpx.HTTPError:
        return None

    if resp.status_code >= 400:
        return None
    return resp.json()


async def _fetch_search_results(
    db: AsyncSession, keyword: str, language: str | None = None
) -> list[dict[str, Any]]:
    resolved_language = resolve_tmdb_language(language)
    data = await _fetch_json(
        db,
        "/search/multi",
        {
            "query": keyword,
            "language": resolved_language,
            "include_adult": "false",
            "page": 1,
        },
    )
    if not data:
        return []

    results: list[dict[str, Any]] = []
    for item in data.get("results", []):
        media_type = item.get("media_type")
        if media_type not in {"movie", "tv"}:
            continue
        title = item.get("title") or item.get("name") or "-"
        date_value = item.get("release_date") or item.get("first_air_date") or ""
        year = date_value.split("-")[0] if date_value else "-"
        results.append(
            {
                "id": f"tmdb:{item.get('id')}",
                "title": title,
                "year": year,
                "overview": item.get("overview") or "",
                "media_type": "MOVIE" if media_type == "movie" else "TV",
                "poster_url": build_image_url(item.get("poster_path"), variant="poster"),
                "backdrop_url": build_image_url(item.get("backdrop_path"), variant="backdrop"),
            }
        )
    return results


def _infer_media_type(
    category: str, media_type: str | None, item_media_type: str | None
) -> str | None:
    if item_media_type:
        return item_media_type
    if media_type in {"movie", "tv"}:
        return media_type
    if category.startswith("movie"):
        return "movie"
    if category.startswith("tv"):
        return "tv"
    return "movie"


async def _fetch_discover_results(
    db: AsyncSession,
    category: str,
    page: int,
    media_type: str | None,
    genre_id: int | None,
    company_id: int | None,
    person_id: int | None,
    keyword_id: int | None,
    sort_by: str | None,
    year: int | None,
    language: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if person_id:
        return await _fetch_person_credit_results(
            db, person_id, page, media_type, sort_by, year, language
        )

    if genre_id or company_id or keyword_id or sort_by or year:
        resolved_media = media_type or "movie"
        if resolved_media not in {"movie", "tv"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid media type"
            )
        media_type = resolved_media
        path = f"/discover/{resolved_media}"
        if not sort_by:
            if category in {"movie_popular", "tv_popular"}:
                sort_by = "popularity.desc"
            elif category in {"movie_top_rated", "tv_top_rated"}:
                sort_by = "vote_average.desc"
    else:
        category_paths = {
            "trending": "/trending/all/week",
            "movie": "/discover/movie",
            "tv": "/discover/tv",
            "movie_popular": "/movie/popular",
            "movie_top_rated": "/movie/top_rated",
            "movie_upcoming": "/movie/upcoming",
            "movie_now_playing": "/movie/now_playing",
            "tv_popular": "/tv/popular",
            "tv_top_rated": "/tv/top_rated",
            "tv_on_the_air": "/tv/on_the_air",
            "tv_airing_today": "/tv/airing_today",
        }
        path = category_paths.get(category)
        if not path:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid category")

    resolved_language = resolve_tmdb_language(language)
    params: dict[str, Any] = {
        "language": resolved_language,
        "include_adult": "false",
        "page": page,
    }
    if genre_id:
        params["with_genres"] = genre_id
    if company_id:
        params["with_companies"] = company_id
    if keyword_id:
        params["with_keywords"] = keyword_id
    if sort_by:
        params["sort_by"] = sort_by
    if year:
        if media_type == "tv":
            params["first_air_date_year"] = year
        else:
            params["primary_release_year"] = year

    data = await _fetch_json(db, path, params)
    if not data:
        return [], False

    results: list[dict[str, Any]] = []
    for item in data.get("results", []):
        media = _infer_media_type(category, media_type, item.get("media_type"))
        if media not in {"movie", "tv"}:
            continue
        title = item.get("title") or item.get("name") or "-"
        date_value = item.get("release_date") or item.get("first_air_date") or ""
        year_value = date_value.split("-")[0] if date_value else "-"
        results.append(
            {
                "id": f"tmdb:{item.get('id')}",
                "title": title,
                "year": year_value,
                "overview": item.get("overview") or "",
                "media_type": "MOVIE" if media == "movie" else "TV",
                "poster_url": build_image_url(item.get("poster_path"), variant="poster"),
                "backdrop_url": build_image_url(item.get("backdrop_path"), variant="backdrop"),
            }
        )
    return results, True


async def _fetch_genres(
    db: AsyncSession, media_type: str, language: str | None = None
) -> list[dict[str, Any]]:
    resolved_language = resolve_tmdb_language(language)
    data = await _fetch_json(
        db,
        f"/genre/{media_type}/list",
        {"language": resolved_language},
    )
    if not data:
        return []

    return [
        {"id": item.get("id"), "name": item.get("name")}
        for item in data.get("genres", [])
        if item.get("id") and item.get("name")
    ]


async def _fetch_companies(db: AsyncSession, page: int) -> list[dict[str, Any]]:
    data = await _fetch_json(db, "/company/popular", {"page": page})
    if not data:
        return []

    results: list[dict[str, Any]] = []
    for item in data.get("results", []):
        results.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "logo_url": build_image_url(item.get("logo_path"), variant="logo"),
            }
        )
    return results


async def _fetch_person_credit_results(
    db: AsyncSession,
    person_id: int,
    page: int,
    media_type: str | None,
    sort_by: str | None,
    year: int | None,
    language: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    resolved_language = resolve_tmdb_language(language)
    data = await _fetch_json(
        db,
        f"/person/{person_id}/combined_credits",
        {"language": resolved_language},
    )
    if not data:
        return [], False

    normalized_media_type = media_type if media_type in {"movie", "tv"} else None
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    credit_items = [
        *(data.get("cast", []) or []),
        *(data.get("crew", []) or []),
    ]
    for item in credit_items:
        media = item.get("media_type")
        item_id = item.get("id")
        if media not in {"movie", "tv"} or not item_id:
            continue
        if normalized_media_type and media != normalized_media_type:
            continue
        identity = (media, int(item_id))
        if identity in seen:
            continue
        seen.add(identity)
        title = item.get("title") or item.get("name") or "-"
        date_value = item.get("release_date") or item.get("first_air_date") or ""
        year_value = date_value.split("-")[0] if date_value else "-"
        if year and year_value != str(year):
            continue
        rows.append(
            {
                "id": f"tmdb:{item_id}",
                "title": title,
                "year": year_value,
                "overview": item.get("overview") or "",
                "media_type": "MOVIE" if media == "movie" else "TV",
                "poster_url": build_image_url(item.get("poster_path"), variant="poster"),
                "backdrop_url": build_image_url(item.get("backdrop_path"), variant="backdrop"),
                "_popularity": float(item.get("popularity") or 0),
                "_vote_average": float(item.get("vote_average") or 0),
                "_date": date_value,
            }
        )

    if sort_by == "vote_average.desc":
        rows.sort(key=lambda item: item.get("_vote_average", 0), reverse=True)
    elif sort_by in {"primary_release_date.asc", "first_air_date.asc"}:
        rows.sort(key=lambda item: str(item.get("_date") or "9999-12-31"))
    elif sort_by in {"primary_release_date.desc", "first_air_date.desc"}:
        rows.sort(key=lambda item: str(item.get("_date") or ""), reverse=True)
    else:
        rows.sort(key=lambda item: item.get("_popularity", 0), reverse=True)

    page_size = 20
    start = max(0, (page - 1) * page_size)
    paged = rows[start : start + page_size]
    for item in paged:
        item.pop("_popularity", None)
        item.pop("_vote_average", None)
        item.pop("_date", None)
    return paged, True


async def search(
    db: AsyncSession, keyword: str, language: str | None = None
) -> list[dict[str, Any]]:
    normalized_keyword = _normalize_search_keyword(keyword)
    if not normalized_keyword:
        return []
    resolved_language = resolve_tmdb_language(language)

    cached = await get_or_set_cached_data(
        db,
        "search",
        {"q": normalized_keyword, "language": resolved_language},
        lambda: _fetch_search_payload(db, normalized_keyword, resolved_language),
    )
    return (cached or {}).get("results", [])


async def discover(
    db: AsyncSession,
    category: str,
    page: int,
    media_type: str | None,
    genre_id: int | None,
    company_id: int | None,
    person_id: int | None,
    keyword_id: int | None,
    sort_by: str | None,
    year: int | None,
    language: str | None = None,
) -> list[dict[str, Any]]:
    resolved_language = resolve_tmdb_language(language)
    params = {
        "category": category,
        "page": page,
        "media_type": media_type,
        "genre_id": genre_id,
        "company_id": company_id,
        "person_id": person_id,
        "keyword_id": keyword_id,
        "sort_by": sort_by,
        "year": year,
        "language": resolved_language,
    }
    cached = await get_or_set_cached_data(
        db,
        "discover",
        params,
        lambda: _fetch_discover_payload(
            db,
            category,
            page,
            media_type,
            genre_id,
            company_id,
            person_id,
            keyword_id,
            sort_by,
            year,
            resolved_language,
        ),
    )
    return (cached or {}).get("results", [])


async def genres(
    db: AsyncSession, media_type: str, language: str | None = None
) -> list[dict[str, Any]]:
    resolved_language = resolve_tmdb_language(language)
    cached = await get_or_set_cached_data(
        db,
        "genres",
        {"media_type": media_type, "language": resolved_language},
        lambda: _wrap_results(_fetch_genres(db, media_type, resolved_language)),
    )
    return (cached or {}).get("results", [])


async def companies(db: AsyncSession, page: int) -> list[dict[str, Any]]:
    cached = await get_or_set_cached_data(
        db,
        "companies",
        {"page": page},
        lambda: _wrap_results(_fetch_companies(db, page)),
    )
    return (cached or {}).get("results", [])


async def _wrap_results(coro) -> dict[str, Any]:
    return {"results": await coro}


async def _fetch_search_payload(
    db: AsyncSession, keyword: str, language: str | None = None
) -> dict[str, Any]:
    return {"results": await _fetch_search_results(db, keyword, language)}


async def _fetch_discover_payload(
    db: AsyncSession,
    category: str,
    page: int,
    media_type: str | None,
    genre_id: int | None,
    company_id: int | None,
    person_id: int | None,
    keyword_id: int | None,
    sort_by: str | None,
    year: int | None,
    language: str | None = None,
) -> dict[str, Any] | None:
    results, ok = await _fetch_discover_results(
        db,
        category,
        page,
        media_type,
        genre_id,
        company_id,
        person_id,
        keyword_id,
        sort_by,
        year,
        language,
    )
    if not ok:
        return None
    return {"results": results}


async def refresh_defaults(db: AsyncSession) -> dict[str, int]:
    total = 0
    for category in DEFAULT_DISCOVER_CATEGORIES:
        results, ok = await _fetch_discover_results(
            db,
            category=category,
            page=1,
            media_type=None,
            genre_id=None,
            company_id=None,
            person_id=None,
            keyword_id=None,
            sort_by=None,
            year=None,
            language="zh-CN",
        )
        if not ok:
            continue
        await set_cached_data(
            db,
            "discover",
            {
                "category": category,
                "page": 1,
                "media_type": None,
                "genre_id": None,
                "company_id": None,
                "person_id": None,
                "keyword_id": None,
                "sort_by": None,
                "year": None,
                "language": "zh-CN",
            },
            {"results": results},
        )
        total += len(results)

    movie_genres = await _fetch_genres(db, "movie", "zh-CN")
    await set_cached_data(
        db, "genres", {"media_type": "movie", "language": "zh-CN"}, {"results": movie_genres}
    )
    total += len(movie_genres)

    tv_genres = await _fetch_genres(db, "tv", "zh-CN")
    await set_cached_data(
        db, "genres", {"media_type": "tv", "language": "zh-CN"}, {"results": tv_genres}
    )
    total += len(tv_genres)

    popular_companies = await _fetch_companies(db, 1)
    await set_cached_data(db, "companies", {"page": 1}, {"results": popular_companies})
    total += len(popular_companies)

    return {"updated": total}


async def warmup_defaults(db: AsyncSession) -> dict[str, int]:
    return await refresh_defaults(db)


async def clear_cache(db: AsyncSession) -> None:
    await db.execute(delete(TmdbCache))
    await db.commit()
    await delete_prefix(REDIS_CACHE_PREFIX)
