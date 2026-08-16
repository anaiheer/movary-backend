from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from uuid import UUID

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.models.emby import EmbyServer
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.system_settings import SystemSettings
from app.models.system_task import SystemTask, SystemTaskStatus
from app.models.system_task_log import SystemTaskLog
from app.models.user import User
from app.models.vod import VodRequest, VodRequestStatus
from app.services.subscriptions import cleanup_expired_subscription_data, expire_due_subscriptions
from app.services.telegram import (
    create_telegram_notification,
    telegram_notification_exists,
)
from app.services import tmdb as tmdb_service

logger = logging.getLogger(__name__)

DEFAULT_TASKS = [
    {
        "key": "vod_sync",
        "name": "点播入库检测",
        "description": "定时检测点播是否已入库并更新状态",
        "interval_seconds": 300,
        "enabled": True,
    },
    {
        "key": "tmdb_sync",
        "name": "TMDB 缓存同步",
        "description": "定时同步 TMDB 热门数据到本地缓存",
        "interval_seconds": 3600,
        "enabled": True,
    },
    {
        "key": "tmdb_cache_persist",
        "name": "TMDB Redis 缓存落库",
        "description": "定时将 Redis 中的 TMDB 缓存写入数据库",
        "interval_seconds": 1800,
        "enabled": True,
    },
    {
        "key": "subscription_expire_sync",
        "name": "订阅到期同步",
        "description": "定时将到期订阅标记为已过期，并同步禁用失效的 Emby 账号",
        "interval_seconds": 300,
        "enabled": True,
    },
    {
        "key": "subscription_expiry_warning",
        "name": "订阅到期提醒",
        "description": "定时为即将到期的订阅写入 Telegram 提醒",
        "interval_seconds": 3600,
        "enabled": True,
    },
]

scheduler = AsyncIOScheduler()
LOCKED_ENABLED_TASK_KEYS = {"tmdb_cache_persist"}


def _extract_tmdb_id(vod: VodRequest) -> int | None:
    if vod.tmdb_id:
        return int(vod.tmdb_id)
    extra = vod.extra_data or {}
    mp_resp = extra.get("moviepilot_response") or {}
    for key in ("tmdb_id", "tmdbid", "tmdb"):
        value = mp_resp.get(key) or extra.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    mediaid = mp_resp.get("mediaid") or extra.get("mediaid")
    if isinstance(mediaid, str) and mediaid.startswith("tmdb:"):
        try:
            return int(mediaid.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


def _task_registry() -> dict[str, Callable[[], Awaitable[dict]]]:
    return {
        "vod_sync": _run_vod_sync,
        "tmdb_sync": _run_tmdb_sync,
        "tmdb_cache_persist": _run_tmdb_cache_persist,
        "subscription_expire_sync": _run_subscription_expire_sync,
        "subscription_expiry_warning": _run_subscription_expiry_warning,
    }


async def _get_log_retention_days(db) -> int:
    settings = await db.scalar(select(SystemSettings))
    if settings and settings.task_log_retention_days:
        return settings.task_log_retention_days
    return 30


async def _prune_task_logs(db) -> None:
    retention_days = await _get_log_retention_days(db)
    if retention_days <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    await db.execute(delete(SystemTaskLog).where(SystemTaskLog.run_at < cutoff))


async def ensure_default_tasks() -> None:
    async with AsyncSessionLocal() as db:
        changed = False
        for task_def in DEFAULT_TASKS:
            existing = await db.scalar(select(SystemTask).where(SystemTask.key == task_def["key"]))
            if existing:
                if existing.key in LOCKED_ENABLED_TASK_KEYS and not existing.enabled:
                    existing.enabled = True
                    db.add(existing)
                    changed = True
                continue
            db.add(SystemTask(**task_def))
            changed = True
        if changed:
            await db.commit()


async def _get_default_emby_server(db) -> EmbyServer | None:
    stmt = (
        select(EmbyServer)
        .where(EmbyServer.is_active.is_(True))
        .order_by(
            EmbyServer.is_default.desc(),
            EmbyServer.priority.desc(),
            EmbyServer.created_at.asc(),
        )
    )
    return (await db.execute(stmt)).scalars().first()


async def _run_vod_sync() -> dict:
    checked = 0
    updated = 0
    async with AsyncSessionLocal() as db:
        server = await _get_default_emby_server(db)
        if not server:
            return {"checked": 0, "updated": 0, "message": "未配置 Emby 服务器"}
        if not server.api_key:
            return {"checked": 0, "updated": 0, "message": "Emby API Key 未配置"}

        stmt = select(VodRequest).where(
            VodRequest.status.in_(
                [
                    VodRequestStatus.APPROVED,
                    VodRequestStatus.QUEUED,
                    VodRequestStatus.DOWNLOADING,
                ]
            )
        )
        vods = (await db.execute(stmt)).scalars().all()
        if not vods:
            return {"checked": 0, "updated": 0, "message": "没有待入库的点播"}

        api_key = server.api_key
        base_urls = [server.base_url, server.external_url, server.backup_url]
        base_urls = [url.rstrip("/") for url in base_urls if url]
        async with httpx.AsyncClient(timeout=15) as client:
            for vod in vods:
                tmdb_id = _extract_tmdb_id(vod)
                if not tmdb_id:
                    continue
                checked += 1
                media_type = (vod.media_type or "").upper()
                if media_type == "MOVIE":
                    include_types = "Movie"
                elif media_type in {"TV", "SERIES"}:
                    include_types = "Series"
                else:
                    include_types = "Movie,Series"
                include_types_candidates = [include_types, "Movie,Series"]
                found = None
                for base_url in base_urls:
                    for include_value in include_types_candidates:
                        params = {
                            "api_key": api_key,
                            "Recursive": "true",
                            "IncludeItemTypes": include_value,
                            "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
                            "Limit": 1,
                        }
                        try:
                            resp = await client.get(f"{base_url}/Items", params=params)
                        except httpx.HTTPError:
                            continue
                        if resp.status_code >= 400:
                            continue
                        data = resp.json() if resp.content else {}
                        items = data.get("Items") or []
                        if items:
                            found = items[0]
                            break
                    if found:
                        break
                if found:
                    vod.status = VodRequestStatus.SUCCEEDED
                    vod.extra_data = {**(vod.extra_data or {}), "emby_item_id": found.get("Id")}
                    vod.updated_at = datetime.utcnow()
                    await create_telegram_notification(
                        db,
                        user_id=vod.user_id,
                        notification_type="vod_completed",
                        title="点播内容已就绪",
                        content=f"「{vod.title}」已可观看",
                        reference_id=str(vod.id),
                    )
                    updated += 1
        if updated:
            await db.commit()

    return {"checked": checked, "updated": updated, "message": "ok"}


async def _run_tmdb_sync() -> dict:
    async with AsyncSessionLocal() as db:
        try:
            result = await tmdb_service.warmup_defaults(db)
        except Exception as exc:  # noqa: BLE001
            return {"checked": 0, "updated": 0, "message": str(exc)}
    return {"checked": 0, "updated": result.get("updated", 0), "message": "ok"}


async def _run_tmdb_cache_persist() -> dict:
    async with AsyncSessionLocal() as db:
        try:
            result = await tmdb_service.sync_redis_cache_to_db(db)
        except Exception as exc:  # noqa: BLE001
            return {"checked": 0, "updated": 0, "message": str(exc)}

    checked = int(result.get("checked", 0))
    updated = int(result.get("updated", 0))
    message = "ok" if checked else "no redis cache found"
    return {"checked": checked, "updated": updated, "message": message}


async def _run_subscription_expire_sync() -> dict:
    async with AsyncSessionLocal() as db:
        expired, affected_users = await expire_due_subscriptions(db)
        purged, purged_users = await cleanup_expired_subscription_data(db)
    return {
        "checked": expired + purged,
        "updated": affected_users + purged_users,
        "message": "ok",
    }


async def _run_subscription_expiry_warning() -> dict:
    now = datetime.utcnow()
    deadline = now + timedelta(days=3)
    checked = 0
    created = 0

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Subscription, Plan, User)
            .join(Plan, Plan.id == Subscription.plan_id)
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.end_at > now,
                Subscription.end_at <= deadline,
            )
        )
        rows = (await db.execute(stmt)).all()
        for subscription, plan, user in rows:
            if not user.expire_remind:
                continue
            remaining_seconds = (subscription.end_at - now).total_seconds()
            remaining_days = max(0, math.ceil(remaining_seconds / 86400))
            if remaining_days not in {1, 3}:
                continue
            checked += 1
            reference_id = f"{subscription.id}:{remaining_days}"
            if await telegram_notification_exists(
                db,
                user_id=user.id,
                notification_type="subscription_expiry_warning",
                reference_id=reference_id,
            ):
                continue
            notification = await create_telegram_notification(
                db,
                user_id=user.id,
                notification_type="subscription_expiry_warning",
                title="订阅即将到期",
                content=f"您的{plan.name}将在{remaining_days}天后到期",
                reference_id=reference_id,
            )
            if notification is not None:
                created += 1
        await db.commit()

    return {"checked": checked, "updated": created, "message": "ok"}


async def run_task(task_id: str) -> dict:
    registry = _task_registry()
    started = datetime.utcnow()
    try:
        normalized_task_id = task_id if isinstance(task_id, UUID) else UUID(str(task_id))
    except (TypeError, ValueError):
        return {"checked": 0, "updated": 0, "message": "task not found"}

    async with AsyncSessionLocal() as db:
        task = await db.scalar(select(SystemTask).where(SystemTask.id == normalized_task_id))
        if not task:
            return {"checked": 0, "updated": 0, "message": "任务不存在"}
        handler = registry.get(task.key)
        if not handler:
            return {"checked": 0, "updated": 0, "message": "任务未实现"}
        task.last_status = SystemTaskStatus.RUNNING
        task.last_run_at = started
        db.add(task)
        await db.commit()

    try:
        result = await handler()
        status = SystemTaskStatus.SUCCESS
        message = result.get("message") or "ok"
    except Exception as exc:  # noqa: BLE001
        result = {"checked": 0, "updated": 0}
        status = SystemTaskStatus.FAILED
        message = str(exc)

    finished = datetime.utcnow()
    duration_ms = int((finished - started).total_seconds() * 1000)

    async with AsyncSessionLocal() as db:
        fresh = await db.scalar(select(SystemTask).where(SystemTask.id == normalized_task_id))
        if fresh:
            fresh.last_status = status
            fresh.last_message = message
            fresh.last_duration_ms = duration_ms
            fresh.run_count = (fresh.run_count or 0) + 1
            db.add(fresh)
            db.add(
                SystemTaskLog(
                    task_id=fresh.id,
                    task_key=fresh.key,
                    task_name=fresh.name,
                    status=status,
                    message=message,
                    run_at=started,
                    duration_ms=duration_ms,
                )
            )
            await _prune_task_logs(db)
            await db.commit()

    return {
        "checked": result.get("checked", 0),
        "updated": result.get("updated", 0),
        "message": message,
    }


async def run_task_by_id(task_id: str) -> dict:
    return await run_task(task_id)


def _schedule_task(task: SystemTask) -> None:
    if not task.enabled:
        return
    scheduler.add_job(
        run_task,
        IntervalTrigger(seconds=task.interval_seconds),
        id=task.key,
        replace_existing=True,
        args=[str(task.id)],
    )


def refresh_schedule(task: SystemTask) -> None:
    job = scheduler.get_job(task.key)
    if job:
        job.remove()
    if task.enabled:
        _schedule_task(task)


async def start_scheduler() -> None:
    # 数据库可能在启动瞬间尚未就绪（例如容器网络短暂抖动），重试三次再放弃，
    # 避免一次瞬时失败就让整个应用启动崩溃。
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await ensure_default_tasks()
            async with AsyncSessionLocal() as db:
                tasks = (await db.execute(select(SystemTask))).scalars().all()
                for task in tasks:
                    _schedule_task(task)
            if not scheduler.running:
                scheduler.start()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 2:
                logger.warning("Scheduler startup attempt %d/3 failed: %s", attempt + 1, exc)
                await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Scheduler startup failed after 3 attempts: {last_exc}") from last_exc


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
