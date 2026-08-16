from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis
from fastapi.encoders import jsonable_encoder
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

from app.core.config import settings


logger = logging.getLogger(__name__)

_redis_client: Redis | None = None


async def init_cache() -> None:
    global _redis_client
    if _redis_client is not None:
        return

    client = redis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        await client.ping()
    except RedisError as exc:
        logger.warning("Redis cache unavailable: %s", exc)
        await client.aclose()
        return

    _redis_client = client


async def close_cache() -> None:
    global _redis_client
    if _redis_client is None:
        return
    await _redis_client.aclose()
    _redis_client = None


def get_cache_client() -> Redis | None:
    return _redis_client


async def get_json(key: str) -> Any | None:
    if _redis_client is None:
        return None
    try:
        raw = await _redis_client.get(key)
    except RedisError as exc:
        logger.warning("Redis get failed for %s: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        await delete(key)
        return None


async def get_many_json(keys: list[str]) -> list[Any | None]:
    if _redis_client is None:
        return [None for _ in keys]
    if not keys:
        return []
    try:
        raw_values = await _redis_client.mget(keys)
    except RedisError as exc:
        logger.warning("Redis mget failed: %s", exc)
        return [None for _ in keys]

    values: list[Any | None] = []
    for key, raw in zip(keys, raw_values, strict=False):
        if raw is None:
            values.append(None)
            continue
        try:
            values.append(json.loads(raw))
        except json.JSONDecodeError:
            values.append(None)
            await delete(key)
    return values


async def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> bool:
    if _redis_client is None:
        return False
    payload = json.dumps(
        jsonable_encoder(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        if ttl_seconds is None:
            await _redis_client.set(key, payload)
        elif ttl_seconds > 0:
            await _redis_client.set(key, payload, ex=int(ttl_seconds))
        else:
            await _redis_client.delete(key)
            return False
    except RedisError as exc:
        logger.warning("Redis set failed for %s: %s", key, exc)
        return False
    return True


async def delete(key: str) -> int:
    if _redis_client is None:
        return 0
    try:
        return int(await _redis_client.delete(key))
    except RedisError as exc:
        logger.warning("Redis delete failed for %s: %s", key, exc)
        return 0


async def delete_many(keys: list[str]) -> int:
    if _redis_client is None or not keys:
        return 0
    try:
        return int(await _redis_client.delete(*keys))
    except RedisError as exc:
        logger.warning("Redis multi-delete failed: %s", exc)
        return 0


async def delete_prefix(prefix: str) -> int:
    if _redis_client is None:
        return 0

    deleted = 0
    cursor = 0
    pattern = f"{prefix}*"
    try:
        while True:
            cursor, keys = await _redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                deleted += int(await _redis_client.delete(*keys))
            if cursor == 0:
                break
    except RedisError as exc:
        logger.warning("Redis prefix delete failed for %s: %s", prefix, exc)
        return deleted
    return deleted
