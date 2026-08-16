from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from app.core.config import settings


class MoviePilotError(RuntimeError):
    pass


def _is_jwt(token: str) -> bool:
    return token.count(".") >= 2


def _build_headers(api_token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    token = api_token or settings.MOVIEPILOT_API_TOKEN
    if token:
        if _is_jwt(token):
            headers["Authorization"] = f"Bearer {token}"
        headers["X-Api-Key"] = token
        headers["X-API-KEY"] = token
        headers["X-Token"] = token
    return headers


def _build_subscribe_body(payload: dict) -> dict:
    tmdb_id = payload.get("tmdb_id") or payload.get("tmdbid")
    douban_id = payload.get("douban_id") or payload.get("doubanid")
    raw_type = (payload.get("media_type") or payload.get("type") or "movie").lower()
    type_map = {
        "tv": "电视剧",
        "series": "电视剧",
        "show": "电视剧",
        "tvshow": "电视剧",
        "电视剧": "电视剧",
        "movie": "电影",
        "film": "电影",
        "电影": "电影",
        "anime": "动漫",
        "动漫": "动漫",
        "variety": "综艺",
        "综艺": "综艺",
    }
    mp_type = type_map.get(raw_type, "电影")
    year = payload.get("year")
    if year is not None and year != "":
        year = str(year)
    body = {
        "tmdbid": tmdb_id,
        "doubanid": douban_id,
        "type": mp_type,
        "name": payload.get("title") or payload.get("name"),
        "year": year,
        "season": payload.get("season"),
        "mediaid": payload.get("mediaid") or (f"tmdb:{tmdb_id}" if tmdb_id else None),
    }
    # remove None values to avoid MoviePilot validation errors
    return {k: v for k, v in body.items() if v is not None}


async def subscribe_vod(
    payload: dict, base_url: str | None = None, api_token: str | None = None
) -> dict[str, Any]:
    target_base = base_url or settings.MOVIEPILOT_BASE_URL
    if not target_base:
        raise MoviePilotError("MoviePilot base url not configured")

    body = _build_subscribe_body(payload)
    if "tmdbid" not in body and "doubanid" not in body:
        raise MoviePilotError("MoviePilot subscribe requires tmdbid or doubanid")

    parsed = urlparse(target_base)
    base_path = (parsed.path or "").rstrip("/")
    if base_path.endswith("/subscribe"):
        subscribe_path = base_path
    elif base_path.endswith("/api/v1"):
        subscribe_path = f"{base_path}/subscribe"
    elif base_path.endswith("/api"):
        subscribe_path = f"{base_path}/v1/subscribe"
    else:
        subscribe_path = f"{base_path}/api/v1/subscribe"
    if not subscribe_path.endswith("/"):
        subscribe_path = f"{subscribe_path}/"
    url = urlunparse(parsed._replace(path=subscribe_path))
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        params = {}
        token = api_token or settings.MOVIEPILOT_API_TOKEN
        if token:
            params["token"] = token
        resp = await client.post(url, json=body, headers=_build_headers(api_token), params=params)

    if resp.status_code >= 400:
        raise MoviePilotError(f"MoviePilot subscribe failed: {resp.status_code} {resp.text}")

    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError:
        snippet = resp.text[:200]
        raise MoviePilotError(f"MoviePilot subscribe invalid response: {snippet}")
    if isinstance(data, dict):
        if data.get("success") is False:
            raise MoviePilotError(data.get("message") or "MoviePilot subscribe failed")
    return data
