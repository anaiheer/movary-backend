import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.system_settings import SystemSettings
from app.models.system_task import SystemTask
from app.models.user import User, UserRole
from app.schemas.system_settings import SmtpTestRequest, SystemSettingsOut, SystemSettingsUpdate
from app.services import tmdb as tmdb_service
from app.services.client_apps import normalize_client_app_configs
from app.services.email import SmtpConfig, send_email
from app.services.email_templates import (
    EmailTemplateKey,
    build_email_template_context,
    list_email_template_configs,
    normalize_email_templates,
    render_email_template,
)
from app.services.social_auth import normalize_social_auth_providers
from app.services.site_languages import normalize_site_languages, resolve_request_language
from app.services.system_tasks import refresh_schedule
from app.services.vod_cache import clear_image_cache, clear_vod_data_cache

router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])
logger = logging.getLogger(__name__)


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def _get_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _get_or_create_tmdb_task(
    db: AsyncSession, enabled: bool, interval_seconds: int | None
) -> SystemTask:
    task = await db.scalar(select(SystemTask).where(SystemTask.key == "tmdb_sync"))
    if task:
        return task

    task = SystemTask(
        key="tmdb_sync",
        name="TMDB 数据缓存自动刷新",
        description="定时刷新并同步首页常用的 TMDB 数据缓存",
        interval_seconds=interval_seconds or 3600,
        enabled=enabled,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _reset_tmdb_caches(
    db: AsyncSession,
    *,
    rebuild_defaults: bool,
) -> tuple[int, dict[str, int], int]:
    await tmdb_service.clear_cache(db)
    data_deleted = await clear_vod_data_cache()
    image_deleted = clear_image_cache()
    reloaded = 0
    if rebuild_defaults:
        result = await tmdb_service.refresh_defaults(db)
        reloaded = int(result.get("updated") or 0)
    return data_deleted, image_deleted, reloaded


def _build_smtp_config(row: SystemSettings) -> SmtpConfig:
    if not row.smtp_host or not row.smtp_port or not row.smtp_from:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SMTP 未配置")
    if row.smtp_use_tls and row.smtp_use_ssl:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="TLS 和 SSL 不能同时启用"
        )
    host = row.smtp_host
    if host == "smtp.google.com":
        host = "smtp.gmail.com"
    return SmtpConfig(
        host=host,
        port=row.smtp_port,
        username=row.smtp_user,
        password=row.smtp_password,
        sender=row.smtp_from,
        use_tls=row.smtp_use_tls,
        use_ssl=row.smtp_use_ssl,
    )


def _serialize_settings(row: SystemSettings, language: str | None = None) -> dict:
    data = {column.name: getattr(row, column.name) for column in SystemSettings.__table__.columns}
    data["enabled_languages"], data["default_language"] = normalize_site_languages(
        row.enabled_languages, row.default_language
    )
    data["emby_client_apps"] = normalize_client_app_configs(row.emby_client_apps)
    data["social_auth_providers"] = normalize_social_auth_providers(row.social_auth_providers)
    data["email_templates"] = list_email_template_configs(row.email_templates, language)
    return SystemSettingsOut.model_validate(data).model_dump(mode="json")


@router.get("", response_model=dict)
async def get_settings(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    row = await _get_settings(db)
    language = resolve_request_language(
        request.headers.get("X-Site-Language"),
        request.headers.get("Accept-Language"),
    )
    return _response(_serialize_settings(row, language))


@router.put("", response_model=dict)
async def update_settings(
    payload: SystemSettingsUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    row = await _get_settings(db)

    prev_warmup_enabled = bool(row.tmdb_warmup_enabled)
    prev_interval = row.tmdb_warmup_interval_seconds
    tmdb_config_fields = {"tmdb_base_url", "tmdb_api_key", "tmdb_proxy_url"}
    payload_data = payload.model_dump(mode="python")
    payload_data["emby_client_apps"] = normalize_client_app_configs(
        payload_data.get("emby_client_apps")
    )
    payload_data["social_auth_providers"] = normalize_social_auth_providers(
        payload_data.get("social_auth_providers")
    )
    (
        payload_data["enabled_languages"],
        payload_data["default_language"],
    ) = normalize_site_languages(
        payload_data.get("enabled_languages"), payload_data.get("default_language")
    )
    payload_data["email_templates"] = normalize_email_templates(payload_data.get("email_templates"))
    should_clear_data_cache = any(
        key in tmdb_config_fields and getattr(row, key) != value
        for key, value in payload_data.items()
    )

    for key, value in payload_data.items():
        setattr(row, key, value)

    db.add(row)
    await db.commit()
    await db.refresh(row)

    if should_clear_data_cache:
        await _reset_tmdb_caches(db, rebuild_defaults=bool(row.tmdb_warmup_enabled))

    new_interval = row.tmdb_warmup_interval_seconds
    new_warmup_enabled = bool(row.tmdb_warmup_enabled)
    task = await _get_or_create_tmdb_task(db, new_warmup_enabled, new_interval)
    if task and (
        new_interval != prev_interval
        or new_warmup_enabled != prev_warmup_enabled
        or task.enabled != new_warmup_enabled
    ):
        task.enabled = new_warmup_enabled
        if new_interval:
            task.interval_seconds = new_interval
        db.add(task)
        await db.commit()
        refresh_schedule(task)

    language = resolve_request_language(
        request.headers.get("X-Site-Language"),
        request.headers.get("Accept-Language"),
    )
    return _response(_serialize_settings(row, language))


@router.post("/cache/refresh", response_model=dict)
async def refresh_cache(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    data_deleted, image_deleted, reloaded = await _reset_tmdb_caches(db, rebuild_defaults=True)

    return _response(
        {
            "data_deleted": data_deleted,
            "image_deleted": image_deleted,
            "reloaded": reloaded,
        },
        "缓存已刷新",
    )


@router.post("/smtp/test", response_model=dict)
async def smtp_test(
    payload: SmtpTestRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    row = await _get_settings(db)
    smtp_config = _build_smtp_config(row)
    rendered = render_email_template(
        row.email_templates,
        EmailTemplateKey.SMTP_TEST,
        build_email_template_context(
            row,
            to_email=payload.to_email,
            sent_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    try:
        await send_email(
            payload.to_email,
            str(rendered["subject"]),
            str(rendered["html_body"]),
            str(rendered["text_body"]) if rendered["text_body"] is not None else None,
            smtp_config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("SMTP test failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _response({"to": payload.to_email}, "测试邮件已发送")


@router.post("/logo", response_model=dict)
async def upload_site_logo(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的站点 Logo 文件")

    suffix = Path(file.filename or "").suffix or ".png"
    filename = f"{uuid4().hex}{suffix}"
    upload_dir = Path(settings.AVATAR_UPLOAD_DIR).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / filename

    with dest.open("wb") as buffer:
        buffer.write(await file.read())

    row = await _get_settings(db)
    row.site_logo_url = f"{settings.AVATAR_URL_PREFIX}/{filename}"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _response({"site_logo_url": row.site_logo_url})
