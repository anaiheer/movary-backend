from pathlib import Path
import asyncio
import hashlib
import logging
import mimetypes
from email.utils import formatdate, parsedate_to_datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.core.security import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.vod import VodRequest, VodRequestStatus, VodFavorite
from app.models.vod_settings import VodSettings
from app.services import tmdb as tmdb_service
from app.services.cache import delete_many, delete_prefix, get_json, get_many_json, set_json
from app.services.vod_cache import emby_image_cache_dir, tmdb_image_cache_dir
from app.models.moviepilot import MoviePilotServer
from app.models.emby import EmbyServer
from app.models.user import User
from app.models.subscription import Subscription
from app.services.subscriptions import (
    get_active_subscription_for_user,
    get_effective_subscriptions_for_user,
    get_vod_quota_subscription_for_user,
)
from app.schemas.vod import (
    VodFavoriteCheck,
    VodFavoriteCreate,
    VodFavoriteOut,
    VodRequestCreate,
    VodRequestOut,
    VodSearchHit,
    VodSearchResponse,
)
from app.services.moviepilot import subscribe_vod, MoviePilotError
from app.services.telegram import create_telegram_notification
from app.services.site_languages import resolve_request_language
from pydantic import BaseModel

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/vod", tags=["vod"])

OVERVIEW_CACHE_TTL = 120
RECENT_CACHE_TTL = 120
REQUESTS_CACHE_TTL = 90
FAVORITES_CACHE_TTL = 180
AVAILABILITY_CACHE_TTL = 300
OVERVIEW_RECENT_TIMEOUT = 2.5
_IMAGE_FETCH_LOCKS: dict[str, asyncio.Lock] = {}


class AvailabilityItem(BaseModel):
    tmdb_id: int
    media_type: str


class AvailabilityRequest(BaseModel):
    items: list[AvailabilityItem]


def _media_type_label(value: str) -> str:
    lowered = value.lower()
    if lowered in {"tv", "series"}:
        return "TV"
    return "MOVIE"


def _tmdb_image_cache_dir() -> Path:
    return tmdb_image_cache_dir()


def _tmdb_image_cache_path(image_base: str, path: str) -> Path:
    suffix = Path(path).suffix or ".jpg"
    key = f"{image_base}:{path}".encode("utf-8")
    filename = f"{hashlib.sha256(key).hexdigest()}{suffix}"
    return _tmdb_image_cache_dir() / filename


def _emby_image_cache_dir() -> Path:
    return emby_image_cache_dir()


def _emby_image_cache_path(
    server_id: UUID,
    item_id: str,
    image_type: str,
    image_index: int | None,
    max_width: int | None,
) -> Path:
    key = f"{server_id}:{item_id}:{image_type}:{image_index}:{max_width}".encode("utf-8")
    filename = f"{hashlib.sha256(key).hexdigest()}.jpg"
    return _emby_image_cache_dir() / filename


async def _get_settings(db: AsyncSession) -> VodSettings:
    stmt = select(VodSettings).limit(1)
    settings_row = (await db.execute(stmt)).scalar()
    if settings_row:
        return settings_row
    settings_row = VodSettings(auto_approve=False)
    db.add(settings_row)
    await db.commit()
    await db.refresh(settings_row)
    return settings_row


async def _check_quota(user: User, media_type: str) -> None:
    if media_type == "MOVIE":
        if user.vod_movie_limit <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="影片点播额度为 0")
        if user.vod_movie_used >= user.vod_movie_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="影片点播额度已用完"
            )
    else:
        if user.vod_tv_limit <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="剧集点播额度为 0")
        if user.vod_tv_used >= user.vod_tv_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="剧集点播额度已用完"
            )


async def _consume_quota(user: User, vod: VodRequest, media_type: str, db: AsyncSession) -> None:
    if media_type == "MOVIE":
        user.vod_movie_used += 1
    else:
        user.vod_tv_used += 1
    db.add(user)

    # Attribute quota usage to the effective subscription that still has quota.
    sub = await get_vod_quota_subscription_for_user(db, user.id, media_type)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前没有可用的订阅额度，请刷新后重试。",
        )
    vod.subscription_id = sub.id
    vod.quota_consumed = True
    db.add(vod)


async def _require_active_subscription(
    db: AsyncSession,
    user_id,
    *,
    detail: str = "请先购买有效订阅服务",
) -> Subscription:
    subscription = await get_active_subscription_for_user(db, user_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return subscription


async def _submit_moviepilot(vod: VodRequest, server: MoviePilotServer) -> dict:
    payload = {
        "title": vod.title,
        "tmdb_id": vod.tmdb_id,
        "douban_id": vod.douban_id,
        "media_type": "movie" if vod.media_type == "MOVIE" else "tv",
        "year": vod.year,
    }
    return await subscribe_vod(payload, base_url=server.base_url, api_token=server.api_token)


async def _get_moviepilot_server(db: AsyncSession) -> MoviePilotServer:
    stmt = select(MoviePilotServer).where(
        MoviePilotServer.is_default.is_(True),
        MoviePilotServer.is_active.is_(True),
    )
    server = (await db.execute(stmt)).scalar()
    if server:
        return server
    fallback_stmt = (
        select(MoviePilotServer)
        .where(MoviePilotServer.is_active.is_(True))
        .order_by(MoviePilotServer.created_at.asc())
    )
    server = (await db.execute(fallback_stmt)).scalar()
    if server:
        return server
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MoviePilot 服务器未配置")


async def _get_default_emby_server(db: AsyncSession) -> EmbyServer | None:
    stmt = (
        select(EmbyServer)
        .where(EmbyServer.is_active.is_(True))
        .order_by(
            EmbyServer.is_default.desc(), EmbyServer.priority.desc(), EmbyServer.created_at.asc()
        )
    )
    return (await db.execute(stmt)).scalars().first()


def _request_language(request: Request) -> str:
    return resolve_request_language(
        request.headers.get("X-Site-Language"),
        request.headers.get("Accept-Language"),
    )


def _overview_cache_key(
    user_id: str | UUID, include_recent: bool = True, language: str = "zh-CN"
) -> str:
    variant = "full" if include_recent else "core"
    return f"vod:overview:v4:{variant}:{language}:{user_id}"


def _requests_cache_key(user_id: str | UUID) -> str:
    return f"vod:requests:{user_id}"


def _favorites_cache_prefix(user_id: str | UUID) -> str:
    return f"vod:favorites:{user_id}:"


def _favorites_cache_key(user_id: str | UUID, limit: int, page: int) -> str:
    return f"{_favorites_cache_prefix(user_id)}{limit}:{page}"


def _recent_cache_key(server: EmbyServer, limit: int, page: int) -> str:
    return f"vod:recent:v2:{server.id}:{limit}:{page}"


def _availability_cache_key(server: EmbyServer, media_type: str, tmdb_id: int) -> str:
    return f"vod:availability:{server.id}:{media_type.upper()}:{tmdb_id}"


def _tmdb_image_headers(cache_path: Path) -> dict[str, str]:
    stat = cache_path.stat()
    etag = f'W/"{stat.st_size:x}-{int(stat.st_mtime):x}"'
    return {
        "Cache-Control": "public, max-age=0, must-revalidate",
        "ETag": etag,
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }


def _get_image_fetch_lock(cache_key: str) -> asyncio.Lock:
    lock = _IMAGE_FETCH_LOCKS.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _IMAGE_FETCH_LOCKS[cache_key] = lock
    return lock


def _not_modified(request: Request, headers: dict[str, str]) -> bool:
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == headers["ETag"]:
        return True

    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since:
        try:
            modified_since = parsedate_to_datetime(if_modified_since)
            cache_mtime = parsedate_to_datetime(headers["Last-Modified"])
            if modified_since >= cache_mtime:
                return True
        except (TypeError, ValueError, IndexError):
            return False
    return False


def _guess_media_type(cache_path: Path) -> str | None:
    guessed, _ = mimetypes.guess_type(str(cache_path))
    return guessed


async def _fetch_recent_items(
    server: EmbyServer,
    limit: int,
    page: int,
) -> list[dict]:
    base_url = server.base_url.rstrip("/")
    api_key = server.api_key
    params = {
        "api_key": api_key,
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Series",
        "Fields": "ProviderIds,Overview,ProductionYear,ImageTags,PremiereDate,BackdropImageTags",
        "SortBy": "DateCreated",
        "SortOrder": "Descending",
        "Limit": limit,
        "StartIndex": (page - 1) * limit,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base_url}/Items", params=params)
        if resp.status_code >= 400:
            return []
    except httpx.HTTPError:
        return []

    data = resp.json()
    results: list[VodSearchHit] = []
    for item in data.get("Items", []) or []:
        provider_ids = item.get("ProviderIds") or {}
        tmdb_id = provider_ids.get("Tmdb")
        if not tmdb_id:
            continue
        item_type = item.get("Type")
        if item_type not in {"Movie", "Series"}:
            continue
        title = item.get("Name") or "-"
        year_value = item.get("ProductionYear") or ""
        if not year_value:
            premiere = item.get("PremiereDate") or ""
            year_value = premiere.split("-")[0] if premiere else ""
        media_type = "MOVIE" if item_type == "Movie" else "TV"
        overview = item.get("Overview") or ""
        image_tags = item.get("ImageTags") or {}
        poster_url = None
        if image_tags.get("Primary"):
            poster_url = (
                f"{settings.API_V1_STR}/vod/emby-image?"
                f"server_id={server.id}&item_id={item.get('Id')}&image_type=Primary&max_width=400"
            )
        backdrop_url = None
        backdrop_tags = item.get("BackdropImageTags") or []
        if backdrop_tags:
            backdrop_url = (
                f"{settings.API_V1_STR}/vod/emby-image?"
                f"server_id={server.id}&item_id={item.get('Id')}&image_type=Backdrop"
                f"&image_index=0&max_width=1280"
            )
        elif image_tags.get("Thumb"):
            backdrop_url = (
                f"{settings.API_V1_STR}/vod/emby-image?"
                f"server_id={server.id}&item_id={item.get('Id')}&image_type=Thumb&max_width=1280"
            )
        results.append(
            VodSearchHit(
                id=f"tmdb:{tmdb_id}",
                title=title,
                year=str(year_value) if year_value else "-",
                overview=overview,
                media_type=media_type,
                poster_url=poster_url,
                backdrop_url=backdrop_url,
            )
        )
    return jsonable_encoder(results)


async def _get_recent_cached(server: EmbyServer | None, limit: int, page: int) -> list[dict]:
    if not server:
        return []
    cache_key = _recent_cache_key(server, limit, page)
    cached = await get_json(cache_key)
    if isinstance(cached, list):
        return cached
    results = await _fetch_recent_items(server, limit, page)
    await set_json(cache_key, results, RECENT_CACHE_TTL)
    return results


async def _fetch_user_requests(db: AsyncSession, user_id: str | UUID) -> list[dict]:
    stmt = (
        select(VodRequest)
        .where(VodRequest.user_id == user_id)
        .order_by(VodRequest.created_at.desc())
    )
    result = await db.execute(stmt)
    return jsonable_encoder(list(result.scalars().all()))


async def _get_user_requests_cached(db: AsyncSession, user_id: str | UUID) -> list[dict]:
    cache_key = _requests_cache_key(user_id)
    cached = await get_json(cache_key)
    if isinstance(cached, list):
        return cached
    results = await _fetch_user_requests(db, user_id)
    await set_json(cache_key, results, REQUESTS_CACHE_TTL)
    return results


async def _fetch_user_favorites(
    db: AsyncSession,
    user_id: str | UUID,
    limit: int,
    page: int,
) -> list[dict]:
    stmt = (
        select(VodFavorite)
        .where(VodFavorite.user_id == user_id)
        .order_by(VodFavorite.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    result = await db.execute(stmt)
    favorites = list(result.scalars().all())
    hits: list[VodSearchHit] = []
    for fav in favorites:
        hits.append(
            VodSearchHit(
                id=f"tmdb:{fav.tmdb_id}",
                title=fav.title,
                year=str(fav.year) if fav.year else "-",
                overview=fav.overview or "",
                media_type=fav.media_type,
                poster_url=fav.poster_url,
                backdrop_url=fav.backdrop_url,
            )
        )
    return jsonable_encoder(hits)


async def _get_user_favorites_cached(
    db: AsyncSession,
    user_id: str | UUID,
    limit: int,
    page: int,
) -> list[dict]:
    cache_key = _favorites_cache_key(user_id, limit, page)
    cached = await get_json(cache_key)
    if isinstance(cached, list):
        return cached
    results = await _fetch_user_favorites(db, user_id, limit, page)
    await set_json(cache_key, results, FAVORITES_CACHE_TTL)
    return results


async def _invalidate_user_vod_cache(user_id: str | UUID) -> None:
    await delete_many(
        [
            _overview_cache_key(user_id, include_recent=True, language="zh-CN"),
            _overview_cache_key(user_id, include_recent=False, language="zh-CN"),
            _overview_cache_key(user_id, include_recent=True, language="en-US"),
            _overview_cache_key(user_id, include_recent=False, language="en-US"),
            _requests_cache_key(user_id),
        ]
    )
    await delete_prefix(_favorites_cache_prefix(user_id))


async def _fetch_tmdb_image(
    image_base: str,
    normalized: str,
    proxy_url: str | None,
) -> tuple[bytes, dict]:
    url = f"{image_base}{normalized}"
    try:
        resp = await tmdb_service.request_tmdb(url, proxy_url=proxy_url, timeout=20)
    except httpx.HTTPError:
        raise

    if resp.status_code >= 400:
        raise httpx.HTTPError(f"TMDB image status {resp.status_code}")
    return resp.content, resp.headers


@router.get("/search", response_model=VodSearchResponse)
async def search_vod(
    request: Request,
    q: str = Query("", description="关键词/片名/tmdb/douban"),
    db: AsyncSession = Depends(get_db),
):
    if not q:
        return {"results": []}
    results = await tmdb_service.search(db, q, _request_language(request))
    return {"results": results}


@router.get("/discover", response_model=VodSearchResponse)
async def discover_vod(
    request: Request,
    category: str = Query("trending", description="trending/movie/tv"),
    page: int = Query(1, ge=1),
    media_type: str | None = Query(None, description="movie/tv"),
    genre_id: int | None = Query(None),
    company_id: int | None = Query(None),
    person_id: int | None = Query(None),
    keyword_id: int | None = Query(None),
    sort_by: str | None = Query(None),
    year: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    language = _request_language(request)
    results = await tmdb_service.discover(
        db=db,
        category=category,
        page=page,
        media_type=media_type,
        genre_id=genre_id,
        company_id=company_id,
        person_id=person_id,
        keyword_id=keyword_id,
        sort_by=sort_by,
        year=year,
        language=language,
    )
    return {"results": results}


@router.get("/discover/overview")
async def discover_overview(
    request: Request,
    include_recent: bool = Query(True),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    language = _request_language(request)
    cache_key = _overview_cache_key(
        current_user["user_id"], include_recent=include_recent, language=language
    )
    cached = await get_json(cache_key)
    if isinstance(cached, dict):
        return cached

    default_server = await _get_default_emby_server(db)

    async def _safe(coro, fallback, timeout: float | None = None):
        try:
            if timeout is not None:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro
        except Exception:
            return fallback

    categories = {
        "trending": tmdb_service.discover(
            db, "trending", 1, None, None, None, None, None, None, None, language
        ),
        "movie_popular": tmdb_service.discover(
            db, "movie_popular", 1, None, None, None, None, None, None, None, language
        ),
        "movie_upcoming": tmdb_service.discover(
            db, "movie_upcoming", 1, None, None, None, None, None, None, None, language
        ),
        "tv_popular": tmdb_service.discover(
            db, "tv_popular", 1, None, None, None, None, None, None, None, language
        ),
        "tv_on_the_air": tmdb_service.discover(
            db, "tv_on_the_air", 1, None, None, None, None, None, None, None, language
        ),
    }

    recent_task = (
        _safe(_get_recent_cached(default_server, 24, 1), [], timeout=OVERVIEW_RECENT_TIMEOUT)
        if include_recent
        else None
    )
    results = await asyncio.gather(
        *([recent_task] if recent_task is not None else []),
        *[_safe(coro, []) for coro in categories.values()],
        _safe(tmdb_service.genres(db, "movie", language), []),
        _safe(tmdb_service.genres(db, "tv", language), []),
        _safe(tmdb_service.companies(db, 1), []),
        _safe(_get_user_requests_cached(db, current_user["user_id"]), []),
        _safe(_get_user_favorites_cached(db, current_user["user_id"], 24, 1), []),
    )

    offset = 1 if include_recent else 0
    recent = results[0] if include_recent else []
    cat_values = results[offset : offset + len(categories)]
    movie_genres = results[offset + len(categories)]
    tv_genres = results[offset + len(categories) + 1]
    companies = results[offset + len(categories) + 2]
    requests = results[offset + len(categories) + 3]
    favorites = results[offset + len(categories) + 4]

    payload = {
        "recent": recent,
        "discover": dict(zip(categories.keys(), cat_values)),
        "genres": {"movie": movie_genres, "tv": tv_genres},
        "companies": companies,
        "requests": requests,
        "favorites": favorites,
        "has_emby_server": default_server is not None,
    }
    await set_json(cache_key, payload, OVERVIEW_CACHE_TTL)
    return payload


@router.get("/genres")
async def get_genres(
    request: Request,
    media_type: str = Query("movie", description="movie/tv"),
    db: AsyncSession = Depends(get_db),
):
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的媒体类型")
    results = await tmdb_service.genres(db, media_type, _request_language(request))
    return {"results": results}


@router.get("/person/{person_id}")
async def get_person_detail(
    request: Request,
    person_id: int,
    db: AsyncSession = Depends(get_db),
):
    language = _request_language(request)
    cache_params = {
        "person_id": person_id,
        "language": tmdb_service.resolve_tmdb_language(language),
        "schema": 1,
    }
    cached = await tmdb_service.get_cached_data(db, "person", cache_params)
    if cached:
        return cached

    base_url, api_key, proxy_url = await tmdb_service.get_config(db)
    if not api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TMDB 未配置")

    url = base_url.rstrip("/") + f"/person/{person_id}"
    params = {
        "api_key": api_key,
        "language": tmdb_service.resolve_tmdb_language(language),
    }
    try:
        resp = await tmdb_service.request_tmdb(url, params=params, proxy_url=proxy_url, timeout=15)
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="获取 TMDB 人员失败")
    if resp.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="获取 TMDB 人员失败")

    data = resp.json()
    payload = {
        "id": person_id,
        "name": data.get("name") or "",
        "profile_url": tmdb_service.build_image_url(data.get("profile_path"), variant="profile"),
    }
    await tmdb_service.set_cached_data(db, "person", cache_params, payload)
    return payload


@router.get("/companies")
async def get_companies(
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    results = await tmdb_service.companies(db, page)
    return {"results": results}


@router.get("/image")
async def proxy_tmdb_image(
    request: Request,
    path: str = Query(..., description="TMDB image path, e.g. /abc.jpg"),
    ttl: int | None = Query(None, ge=0, description="legacy param, ignored"),
    variant: str | None = Query(None, description="poster/backdrop/logo/profile"),
    db: AsyncSession = Depends(get_db),
):
    if ".." in path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的路径")
    normalized = path if path.startswith("/") else f"/{path}"

    image_base = tmdb_service.get_image_base(variant).rstrip("/")
    cache_path = _tmdb_image_cache_path(image_base, normalized)
    if cache_path.exists():
        headers = _tmdb_image_headers(cache_path)
        if _not_modified(request, headers):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return FileResponse(
            cache_path,
            headers=headers,
            media_type=_guess_media_type(cache_path),
        )

    async with _get_image_fetch_lock(str(cache_path)):
        if cache_path.exists():
            headers = _tmdb_image_headers(cache_path)
            if _not_modified(request, headers):
                return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
            return FileResponse(
                cache_path,
                headers=headers,
                media_type=_guess_media_type(cache_path),
            )

        _, _, proxy_url = await tmdb_service.get_config(db)
        try:
            content, response_headers = await _fetch_tmdb_image(image_base, normalized, proxy_url)
        except httpx.HTTPError:
            if cache_path.exists():
                headers = _tmdb_image_headers(cache_path)
                return FileResponse(
                    cache_path,
                    headers=headers,
                    media_type=_guess_media_type(cache_path),
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="获取 TMDB 图片失败",
            )

        cache_path.write_bytes(content)
        headers = _tmdb_image_headers(cache_path)
        if _not_modified(request, headers):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return FileResponse(
            cache_path,
            headers=headers,
            media_type=response_headers.get("content-type") or _guess_media_type(cache_path),
        )


@router.get("/emby-image")
async def proxy_emby_image(
    request: Request,
    server_id: UUID = Query(...),
    item_id: str = Query(...),
    image_type: str = Query("Primary"),
    image_index: int | None = Query(None, ge=0),
    max_width: int | None = Query(None, ge=1),
    ttl: int | None = Query(None, ge=0, description="legacy param, ignored"),
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(EmbyServer, server_id)
    if not server or not server.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emby 服务器不存在")

    cache_path = _emby_image_cache_path(server_id, item_id, image_type, image_index, max_width)
    if cache_path.exists():
        headers = _tmdb_image_headers(cache_path)
        if _not_modified(request, headers):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return FileResponse(
            cache_path,
            headers=headers,
            media_type=_guess_media_type(cache_path),
        )

    base_url = (server.base_url or server.external_url or "").rstrip("/")
    if not base_url or not server.api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Emby 服务器未配置")

    image_path = f"/Items/{item_id}/Images/{image_type}"
    if image_index is not None:
        image_path = f"{image_path}/{image_index}"
    params = {"api_key": server.api_key}
    if max_width:
        params["maxWidth"] = str(max_width)

    async with _get_image_fetch_lock(str(cache_path)):
        if cache_path.exists():
            headers = _tmdb_image_headers(cache_path)
            if _not_modified(request, headers):
                return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
            return FileResponse(
                cache_path,
                headers=headers,
                media_type=_guess_media_type(cache_path),
            )

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{base_url}{image_path}", params=params)
            if response.status_code >= 400:
                raise httpx.HTTPError(f"Emby image status {response.status_code}")
        except httpx.HTTPError:
            if cache_path.exists():
                headers = _tmdb_image_headers(cache_path)
                return FileResponse(
                    cache_path,
                    headers=headers,
                    media_type=_guess_media_type(cache_path),
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="获取 Emby 图片失败",
            )

        cache_path.write_bytes(response.content)
        headers = _tmdb_image_headers(cache_path)
        if _not_modified(request, headers):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return FileResponse(
            cache_path,
            headers=headers,
            media_type=response.headers.get("content-type") or _guess_media_type(cache_path),
        )


@router.get("/recent", response_model=VodSearchResponse)
async def get_recent(
    limit: int = Query(20, ge=1, le=100),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_default_emby_server(db)
    return {"results": await _get_recent_cached(server, limit, page)}


@router.post("/availability")
async def get_availability(
    payload: AvailabilityRequest,
    db: AsyncSession = Depends(get_db),
):
    if not payload.items:
        return {"results": []}

    server = await _get_default_emby_server(db)
    if not server:
        return {
            "results": [
                {"tmdb_id": item.tmdb_id, "media_type": item.media_type, "available": False}
                for item in payload.items
            ]
        }

    base_url = server.base_url.rstrip("/")
    api_key = server.api_key

    unique_items: dict[tuple[str, int], AvailabilityItem] = {}
    for item in payload.items:
        key = (item.media_type.upper(), item.tmdb_id)
        if key not in unique_items:
            unique_items[key] = item

    cache_keys = [
        _availability_cache_key(server, media_type, tmdb_id) for media_type, tmdb_id in unique_items
    ]
    cached_values = await get_many_json(cache_keys)
    resolved: dict[tuple[str, int], bool] = {}
    uncached_items: list[AvailabilityItem] = []
    for ((media_type, tmdb_id), item), cached in zip(
        unique_items.items(), cached_values, strict=False
    ):
        if isinstance(cached, bool):
            resolved[(media_type, tmdb_id)] = cached
        else:
            uncached_items.append(item)

    async def _check_item(
        client: httpx.AsyncClient, item: AvailabilityItem, sem: asyncio.Semaphore
    ) -> tuple[tuple[str, int], bool]:
        async with sem:
            media_type = item.media_type.upper()
            include_types = "Movie" if media_type == "MOVIE" else "Series"
            params = {
                "api_key": api_key,
                "Recursive": "true",
                "IncludeItemTypes": include_types,
                "AnyProviderIdEquals": f"Tmdb.{item.tmdb_id}",
                "Limit": 1,
            }
            try:
                resp = await client.get(f"{base_url}/Items", params=params)
                if resp.status_code >= 400:
                    available = False
                else:
                    data = resp.json()
                    available = bool(data.get("Items"))
            except httpx.HTTPError:
                available = False
            return (media_type, item.tmdb_id), available

    if uncached_items:
        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=15) as client:
            tasks = [_check_item(client, item, sem) for item in uncached_items]
            for cache_identity, available in await asyncio.gather(*tasks):
                resolved[cache_identity] = available
                media_type, tmdb_id = cache_identity
                await set_json(
                    _availability_cache_key(server, media_type, tmdb_id),
                    available,
                    AVAILABILITY_CACHE_TTL,
                )

    return {
        "results": [
            {
                "tmdb_id": item.tmdb_id,
                "media_type": item.media_type,
                "available": resolved.get((item.media_type.upper(), item.tmdb_id), False),
            }
            for item in payload.items
        ]
    }


@router.get("/detail")
async def get_detail(
    request: Request,
    media_type: str = Query("movie", description="movie/tv"),
    tmdb_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
):
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的媒体类型")

    language = _request_language(request)
    cache_params = {
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "language": tmdb_service.resolve_tmdb_language(language),
        "schema": 5,
    }
    cached = await tmdb_service.get_cached_data(db, "detail", cache_params)
    if cached:
        cached_seasons = cached.get("seasons") or []
        cached_episode_seasons = [
            season
            for season in cached_seasons
            if (season.get("season_number") or 0) > 0 and (season.get("episode_count") or 0) > 0
        ]
        has_complete_episode_data = all(season.get("episodes") for season in cached_episode_seasons)
        if media_type != "tv" or not cached_episode_seasons or has_complete_episode_data:
            logger.debug(
                "Returning cached detail for tmdb_id=%d media_type=%s (seasons=%d episodes_ok=%s)",
                tmdb_id,
                media_type,
                len(cached_seasons),
                has_complete_episode_data,
            )
            return cached
        logger.info(
            "Cache incomplete for tmdb_id=%d — %d seasons lack episode data, re-fetching",
            tmdb_id,
            sum(1 for s in cached_episode_seasons if not s.get("episodes")),
        )

    base_url, api_key, proxy_url = await tmdb_service.get_config(db)
    if not api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TMDB 未配置")

    url = base_url.rstrip("/") + f"/{media_type}/{tmdb_id}"
    params = {
        "api_key": api_key,
        "language": tmdb_service.resolve_tmdb_language(language),
        "append_to_response": "credits,recommendations,images,keywords",
        "include_image_language": tmdb_service.build_image_language_preference(language),
    }
    try:
        resp = await tmdb_service.request_tmdb(url, params=params, proxy_url=proxy_url, timeout=15)
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="获取 TMDB 详情失败")
    if resp.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="获取 TMDB 详情失败")

    data = resp.json()
    title = data.get("title") or data.get("name") or "-"
    release_date = data.get("release_date") or data.get("first_air_date") or ""
    year = release_date.split("-")[0] if release_date else ""
    poster_path = data.get("poster_path")
    backdrop_path = data.get("backdrop_path")

    def _pick_localized_image_path(
        image_type: str, fallback_path: str | None, language_code: str
    ) -> str | None:
        images = (data.get("images") or {}).get(image_type) or []
        if not images:
            return fallback_path
        preferred_languages = ["en", None] if language_code == "en-US" else ["zh", None]
        for preferred in preferred_languages:
            for item in images:
                if item.get("file_path") and item.get("iso_639_1") == preferred:
                    return item.get("file_path")
        return fallback_path

    poster_path = _pick_localized_image_path("posters", poster_path, language)
    backdrop_path = _pick_localized_image_path("backdrops", backdrop_path, language)

    def _company(item: dict) -> dict:
        logo_path = item.get("logo_path")
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "logo_url": tmdb_service.build_image_url(logo_path, variant="logo"),
        }

    credits = data.get("credits") or {}
    cast = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "character": item.get("character") or item.get("roles", [{}])[0].get("character"),
            "profile_url": tmdb_service.build_image_url(
                item.get("profile_path"), variant="profile"
            ),
        }
        for item in (credits.get("cast") or [])
    ]
    crew = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "job": item.get("job"),
            "department": item.get("department"),
            "profile_url": tmdb_service.build_image_url(
                item.get("profile_path"), variant="profile"
            ),
        }
        for item in (credits.get("crew") or [])
    ]
    keyword_data = data.get("keywords") or {}
    keyword_items = (
        keyword_data.get("results")
        if isinstance(keyword_data.get("results"), list)
        else keyword_data.get("keywords")
    ) or []
    rec_items = []
    for item in (data.get("recommendations") or {}).get("results", []):
        item_title = item.get("title") or item.get("name") or "-"
        item_date = item.get("release_date") or item.get("first_air_date") or ""
        item_year = item_date.split("-")[0] if item_date else "-"
        item_poster = item.get("poster_path")
        rec_items.append(
            {
                "id": f"tmdb:{item.get('id')}",
                "title": item_title,
                "year": item_year,
                "overview": item.get("overview") or "",
                "media_type": "MOVIE"
                if (item.get("media_type") or media_type) == "movie"
                else "TV",
                "poster_url": tmdb_service.build_image_url(item_poster, variant="poster"),
            }
        )

    backdrop_items = []
    seen_backdrops = set()
    for item in (data.get("images") or {}).get("backdrops") or []:
        file_path = item.get("file_path")
        if not file_path or file_path in seen_backdrops:
            continue
        seen_backdrops.add(file_path)
        backdrop_items.append(
            {
                "id": file_path,
                "file_path": file_path,
                "image_url": tmdb_service.build_image_url(file_path, variant="backdrop"),
                "image_url_large": tmdb_service.build_image_url(file_path, variant="backdrop-lg"),
                "width": item.get("width"),
                "height": item.get("height"),
                "aspect_ratio": item.get("aspect_ratio"),
                "vote_average": item.get("vote_average"),
            }
        )

    season_fetch_max_retries = 2

    async def _fetch_season_episodes(season_number: int) -> tuple[int, list[dict]]:
        season_url = base_url.rstrip("/") + f"/tv/{tmdb_id}/season/{season_number}"
        season_params = {
            "api_key": api_key,
            "language": tmdb_service.resolve_tmdb_language(language),
        }
        last_error: str | None = None
        for attempt in range(1 + season_fetch_max_retries):
            try:
                season_resp = await tmdb_service.request_tmdb(
                    season_url, params=season_params, proxy_url=proxy_url, timeout=15
                )
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                if attempt < season_fetch_max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if season_resp.status_code >= 400:
                last_error = f"HTTP {season_resp.status_code}"
                is_retryable = (
                    season_resp.status_code in {408, 429} or season_resp.status_code >= 500
                )
                if is_retryable and attempt < season_fetch_max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                break
            season_data = season_resp.json()
            episodes = []
            for episode in season_data.get("episodes") or []:
                episodes.append(
                    {
                        "id": episode.get("id"),
                        "name": episode.get("name"),
                        "episode_number": episode.get("episode_number"),
                        "overview": episode.get("overview") or "",
                        "air_date": episode.get("air_date") or "",
                        "still_url": tmdb_service.build_image_url(
                            episode.get("still_path"), variant="still"
                        ),
                    }
                )
            logger.debug(
                "Fetched %d episodes for tmdb_id=%d season=%d (attempt %d)",
                len(episodes),
                tmdb_id,
                season_number,
                attempt + 1,
            )
            return season_number, episodes

        logger.warning(
            "Failed to fetch season %d episodes for tmdb_id=%d after %d retries: %s",
            season_number,
            tmdb_id,
            season_fetch_max_retries,
            last_error,
        )
        return season_number, []

    seasons = []
    season_episode_map: dict[int, list[dict]] = {}
    all_seasons_ok = True
    if media_type == "tv":
        raw_seasons = data.get("seasons") or []
        season_fetch_sem = asyncio.Semaphore(2)
        season_numbers = [
            season.get("season_number")
            for season in raw_seasons
            if isinstance(season.get("season_number"), int)
            and season.get("season_number") > 0
            and season.get("episode_count")
        ]

        logger.debug(
            "Fetching episodes for tmdb_id=%d seasons=%s (from %d raw seasons)",
            tmdb_id,
            season_numbers,
            len(raw_seasons),
        )

        async def _fetch_limited_season_episodes(season_number: int) -> tuple[int, list[dict]]:
            async with season_fetch_sem:
                return await _fetch_season_episodes(season_number)

        fetched_seasons = await asyncio.gather(
            *[_fetch_limited_season_episodes(number) for number in season_numbers]
        )
        season_episode_map = dict(fetched_seasons)

        missing_episode_seasons = [
            season_number
            for season_number in season_numbers
            if not season_episode_map.get(season_number)
        ]
        if missing_episode_seasons:
            all_seasons_ok = False
            logger.warning(
                "Missing episodes for tmdb_id=%d seasons=%s (fetched %d/%d seasons successfully)",
                tmdb_id,
                missing_episode_seasons,
                len(season_numbers) - len(missing_episode_seasons),
                len(season_numbers),
            )

        for season in data.get("seasons") or []:
            season_poster = season.get("poster_path")
            season_number = season.get("season_number")
            seasons.append(
                {
                    "id": season.get("id"),
                    "name": season.get("name"),
                    "season_number": season_number,
                    "episode_count": season.get("episode_count"),
                    "air_date": season.get("air_date"),
                    "poster_url": tmdb_service.build_image_url(season_poster, variant="poster"),
                    "episodes": season_episode_map.get(season_number, []),
                }
            )

    payload = {
        "id": tmdb_id,
        "title": title,
        "original_title": data.get("original_title") or data.get("original_name") or "",
        "overview": data.get("overview") or "",
        "tagline": data.get("tagline") or "",
        "year": year,
        "release_date": release_date,
        "runtime": data.get("runtime") or (data.get("episode_run_time") or [None])[0],
        "media_type": "MOVIE" if media_type == "movie" else "TV",
        "vote_average": data.get("vote_average"),
        "vote_count": data.get("vote_count"),
        "status": data.get("status"),
        "poster_url": tmdb_service.build_image_url(poster_path, variant="poster-lg"),
        "backdrop_url": tmdb_service.build_image_url(backdrop_path, variant="backdrop-lg"),
        "genres": data.get("genres") or [],
        "keywords": [
            {"id": item.get("id"), "name": item.get("name")}
            for item in keyword_items
            if item.get("id") and item.get("name")
        ],
        "production_companies": [
            _company(item) for item in (data.get("production_companies") or [])
        ],
        "production_countries": data.get("production_countries") or [],
        "credits": {"cast": cast, "crew": crew},
        "recommendations": rec_items,
        "backdrops": backdrop_items,
        "seasons": seasons,
    }
    if all_seasons_ok:
        await tmdb_service.set_cached_data(db, "detail", cache_params, payload)
    else:
        logger.info(
            "Skipping cache write for tmdb_id=%d — incomplete season episode data, will retry next request",
            tmdb_id,
        )
    return payload


@router.post("/requests", response_model=VodRequestOut, status_code=status.HTTP_201_CREATED)
async def create_vod_request(
    payload: VodRequestCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    user_stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(user_stmt)).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    media_type = _media_type_label(payload.media_type)
    active_subscription = await _require_active_subscription(db, user_id)
    await _check_quota(user, media_type)

    vod = VodRequest(
        user_id=user_id,
        title=payload.title,
        year=payload.year,
        media_type=media_type,
        tmdb_id=payload.tmdb_id,
        douban_id=payload.douban_id,
        status=VodRequestStatus.PENDING,
        cost_type="TIMES",
        cost_amount=1,
    )
    db.add(vod)
    await db.commit()
    await db.refresh(vod)

    settings_row = await _get_settings(db)
    if settings_row.auto_approve:
        try:
            server = await _get_moviepilot_server(db)
            resp = await _submit_moviepilot(vod, server)
            vod.moviepilot_subscribe_id = str(resp.get("id") or resp.get("subscribe_id") or "")
            vod.extra_data = {**(vod.extra_data or {}), "moviepilot_response": resp}
            # Use APPROVED to表明已通过审核，若moviepilot返回状态则优先使用
            mp_state = (resp.get("state") or resp.get("status") or "").upper()
            if mp_state in {
                VodRequestStatus.DOWNLOADING,
                VodRequestStatus.SUCCEEDED,
                VodRequestStatus.QUEUED,
            }:
                vod.status = mp_state
            else:
                vod.status = VodRequestStatus.APPROVED
            vod.subscription_id = active_subscription.id
            await _consume_quota(user, vod, media_type, db)
            db.add(vod)
            await create_telegram_notification(
                db,
                user_id=vod.user_id,
                notification_type="vod_approved",
                title="点播请求已通过",
                content=f"「{vod.title}」已开始处理",
                reference_id=str(vod.id),
            )
            await db.commit()
            await db.refresh(vod)
        except (MoviePilotError, HTTPException) as exc:
            vod.status = VodRequestStatus.FAILED
            vod.fail_reason = str(exc)
            db.add(vod)
            await db.commit()
            await db.refresh(vod)

    await _invalidate_user_vod_cache(user_id)
    return vod


@router.get("/requests/me", response_model=list[VodRequestOut])
async def get_my_vod_requests(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_user_requests_cached(db, current_user["user_id"])


@router.get("/favorites/me", response_model=VodSearchResponse)
async def get_my_favorites(
    limit: int = Query(24, ge=1, le=100),
    page: int = Query(1, ge=1),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"results": await _get_user_favorites_cached(db, current_user["user_id"], limit, page)}


@router.get("/favorites/check", response_model=VodFavoriteCheck)
async def check_favorite(
    tmdb_id: int = Query(..., ge=1),
    media_type: str = Query(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    normalized = _media_type_label(media_type)
    stmt = select(VodFavorite.id).where(
        VodFavorite.user_id == current_user["user_id"],
        VodFavorite.tmdb_id == tmdb_id,
        VodFavorite.media_type == normalized,
    )
    result = await db.execute(stmt)
    return {"favorited": result.scalar() is not None}


@router.post("/favorites", response_model=VodFavoriteOut, status_code=status.HTTP_201_CREATED)
async def add_favorite(
    payload: VodFavoriteCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    normalized = _media_type_label(payload.media_type)
    stmt = select(VodFavorite).where(
        VodFavorite.user_id == user_id,
        VodFavorite.tmdb_id == payload.tmdb_id,
        VodFavorite.media_type == normalized,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    favorite = VodFavorite(
        user_id=user_id,
        tmdb_id=payload.tmdb_id,
        media_type=normalized,
        title=payload.title,
        year=payload.year,
        overview=payload.overview,
        poster_url=payload.poster_url,
        backdrop_url=payload.backdrop_url,
    )
    db.add(favorite)
    await db.commit()
    await db.refresh(favorite)
    await _invalidate_user_vod_cache(user_id)
    return favorite


@router.delete("/favorites", response_model=VodFavoriteCheck)
async def remove_favorite(
    tmdb_id: int = Query(..., ge=1),
    media_type: str = Query(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    normalized = _media_type_label(media_type)
    stmt = select(VodFavorite).where(
        VodFavorite.user_id == user_id,
        VodFavorite.tmdb_id == tmdb_id,
        VodFavorite.media_type == normalized,
    )
    result = await db.execute(stmt)
    favorite = result.scalar_one_or_none()
    if not favorite:
        return {"favorited": False}
    await db.delete(favorite)
    await db.commit()
    await _invalidate_user_vod_cache(user_id)
    return {"favorited": False}


@router.delete("/requests/{vod_id}")
async def delete_vod_request(
    vod_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    stmt = select(VodRequest).where(VodRequest.id == vod_id, VodRequest.user_id == user_id)
    vod = (await db.execute(stmt)).scalar()
    if not vod:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="点播请求不存在")

    await db.delete(vod)
    await db.commit()
    await _invalidate_user_vod_cache(user_id)
    return {"success": True}


@router.get("/limits")
async def get_my_vod_limits(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    active_subscriptions = await get_effective_subscriptions_for_user(db, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return {
        "movie_limit": int(user.vod_movie_limit or 0) if active_subscriptions else 0,
        "movie_used": int(user.vod_movie_used or 0) if active_subscriptions else 0,
        "tv_limit": int(user.vod_tv_limit or 0) if active_subscriptions else 0,
        "tv_used": int(user.vod_tv_used or 0) if active_subscriptions else 0,
    }
