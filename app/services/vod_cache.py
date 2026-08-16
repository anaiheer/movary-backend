from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.services.cache import delete_prefix


VOD_DATA_CACHE_PREFIXES = (
    "vod:overview:",
    "vod:recent:",
    "vod:requests:",
    "vod:favorites:",
    "vod:availability:",
    "vod:detail:",
)


def _uploads_root() -> Path:
    return Path(settings.AVATAR_UPLOAD_DIR).resolve().parent


def tmdb_image_cache_dir() -> Path:
    cache_dir = _uploads_root() / "tmdb_images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def emby_image_cache_dir() -> Path:
    cache_dir = _uploads_root() / "emby_images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _clear_cache_dir(cache_dir: Path) -> int:
    deleted = 0
    if not cache_dir.exists():
        return deleted
    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        entry.unlink(missing_ok=True)
        deleted += 1
    return deleted


async def clear_vod_data_cache() -> int:
    deleted = 0
    for prefix in VOD_DATA_CACHE_PREFIXES:
        deleted += await delete_prefix(prefix)
    return deleted


def clear_image_cache() -> dict[str, int]:
    return {
        "tmdb_images": _clear_cache_dir(tmdb_image_cache_dir()),
        "emby_images": _clear_cache_dir(emby_image_cache_dir()),
    }
