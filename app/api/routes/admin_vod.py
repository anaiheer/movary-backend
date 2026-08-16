from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.user import User, UserRole
from app.models.moviepilot import MoviePilotServer
from app.models.vod import VodRequest, VodRequestStatus
from app.models.vod_settings import VodSettings
from app.schemas.vod import (
    VodRequestAdminOut,
    VodRequestListResponse,
    VodSettingsOut,
    VodSettingsUpdate,
    VodRejectRequest,
)
from app.services.moviepilot import MoviePilotError, subscribe_vod
from app.services.telegram import create_telegram_notification
from app.models.subscription import Subscription
from app.services.subscriptions import (
    get_active_subscription_for_user,
    get_vod_quota_subscription_for_user,
)
from app.api.routes.vod import _invalidate_user_vod_cache


router = APIRouter(prefix="/admin/vod", tags=["admin-vod"])


class VodBatchDeleteRequest(BaseModel):
    ids: list[UUID]


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _delete_vod_requests_by_ids(vod_ids: list[UUID], db: AsyncSession) -> dict:
    requested_ids: list[UUID] = []
    seen_ids: set[UUID] = set()
    for vod_id in vod_ids:
        if vod_id in seen_ids:
            continue
        seen_ids.add(vod_id)
        requested_ids.append(vod_id)

    if not requested_ids:
        return {
            "requested": 0,
            "deleted": 0,
            "missing": 0,
            "missing_ids": [],
            "failed_ids": [],
        }

    requests = (
        (
            await db.execute(
                select(VodRequest)
                .where(VodRequest.id.in_(requested_ids))
                .order_by(VodRequest.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    request_map = {request.id: request for request in requests}
    missing_ids = [str(vod_id) for vod_id in requested_ids if vod_id not in request_map]
    failed_ids: list[str] = []
    invalidated_user_ids: set[UUID] = set()
    deleted_count = 0

    for vod_id in requested_ids:
        vod = request_map.get(vod_id)
        if not vod:
            continue
        try:
            async with db.begin_nested():
                await db.execute(delete(VodRequest).where(VodRequest.id == vod_id))
                await db.flush()
            invalidated_user_ids.add(vod.user_id)
            deleted_count += 1
        except Exception:
            failed_ids.append(str(vod_id))

    await db.commit()

    for user_id in invalidated_user_ids:
        await _invalidate_user_vod_cache(user_id)

    return {
        "requested": len(requested_ids),
        "deleted": deleted_count,
        "missing": len(missing_ids),
        "missing_ids": missing_ids,
        "failed_ids": failed_ids,
    }


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


async def _apply_quota(user: User, media_type: str) -> None:
    if media_type == "MOVIE":
        user.vod_movie_used += 1
    else:
        user.vod_tv_used += 1


async def _attach_quota_meta(vod: VodRequest, subscription: Subscription, db: AsyncSession) -> None:
    sub = subscription
    if sub:
        vod.subscription_id = sub.id
    vod.quota_consumed = True
    db.add(vod)


def _status_label(status: str | None) -> str:
    normalized = (status or "").upper()
    if normalized in {VodRequestStatus.PENDING, VodRequestStatus.CREATED}:
        return "待审核"
    if normalized == VodRequestStatus.REJECTED:
        return "已拒绝"
    if normalized == VodRequestStatus.FAILED:
        return "失败"
    if normalized == VodRequestStatus.CANCELED:
        return "已取消"
    if normalized in {
        VodRequestStatus.APPROVED,
        VodRequestStatus.QUEUED,
        VodRequestStatus.DOWNLOADING,
    }:
        return "待入库"
    if normalized == VodRequestStatus.SUCCEEDED:
        return "已入库"
    return "待审核"


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


@router.get("/requests", response_model=dict)
async def list_vod_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    keyword: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    media_type: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    filters = []
    if keyword:
        like = f"%{keyword}%"
        filters.append(
            or_(VodRequest.title.ilike(like), User.username.ilike(like), User.email.ilike(like))
        )
    if status_filter:
        normalized = status_filter.upper()
        if normalized == "PENDING":
            filters.append(
                VodRequest.status.in_([VodRequestStatus.PENDING, VodRequestStatus.CREATED])
            )
        elif normalized in {"PROCESSING", "APPROVED"}:
            filters.append(
                VodRequest.status.in_(
                    [
                        VodRequestStatus.APPROVED,
                        VodRequestStatus.QUEUED,
                        VodRequestStatus.DOWNLOADING,
                    ]
                )
            )
        elif normalized == "SUCCEEDED":
            filters.append(VodRequest.status == VodRequestStatus.SUCCEEDED)
        else:
            filters.append(VodRequest.status == normalized)
    if media_type:
        filters.append(VodRequest.media_type == media_type)

    base_stmt = (
        select(VodRequest, User)
        .join(User, User.id == VodRequest.user_id)
        .order_by(VodRequest.created_at.desc())
    )
    if filters:
        base_stmt = base_stmt.where(*filters)

    count_stmt = select(func.count()).select_from(
        select(VodRequest.id).join(User, User.id == VodRequest.user_id).where(*filters).subquery()
    )
    total = await db.scalar(count_stmt)

    rows = (await db.execute(base_stmt.offset((page - 1) * page_size).limit(page_size))).all()
    items = [
        VodRequestAdminOut(
            id=vod.id,
            user={"id": user.id, "username": user.username, "email": user.email},
            title=vod.title,
            media_type=vod.media_type,
            status=vod.status,
            status_label=_status_label(vod.status),
            year=vod.year,
            tmdb_id=vod.tmdb_id,
            cost_amount=vod.cost_amount,
            fail_reason=vod.fail_reason,
            created_at=vod.created_at,
            updated_at=vod.updated_at,
        )
        for vod, user in rows
    ]

    payload = VodRequestListResponse(
        items=items, page=page, page_size=page_size, total=int(total or 0)
    )
    return _response(payload.model_dump(mode="json"))


@router.post("/requests/{vod_id}/approve", response_model=dict)
async def approve_vod_request(
    vod_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = (
        select(VodRequest, User)
        .join(User, User.id == VodRequest.user_id)
        .where(VodRequest.id == vod_id)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="点播请求不存在")
    vod, user = row
    if vod.status not in {
        VodRequestStatus.PENDING,
        VodRequestStatus.CREATED,
        VodRequestStatus.FAILED,
    }:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的状态")

    if await get_active_subscription_for_user(db, user.id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该用户已无有效订阅",
        )

    await _check_quota(user, vod.media_type)
    quota_subscription = await get_vod_quota_subscription_for_user(db, user.id, vod.media_type)
    if quota_subscription is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该用户当前无可用订阅额度",
        )

    payload = {
        "title": vod.title,
        "tmdb_id": vod.tmdb_id,
        "douban_id": vod.douban_id,
        "media_type": "movie" if vod.media_type == "MOVIE" else "tv",
        "year": vod.year,
    }

    try:
        server = await _get_moviepilot_server(db)
        resp = await subscribe_vod(payload, base_url=server.base_url, api_token=server.api_token)
        vod.moviepilot_subscribe_id = str(resp.get("id") or resp.get("subscribe_id") or "")
        vod.extra_data = {**(vod.extra_data or {}), "moviepilot_response": resp}
        mp_state = (resp.get("state") or resp.get("status") or "").upper()
        if mp_state in {
            VodRequestStatus.DOWNLOADING,
            VodRequestStatus.SUCCEEDED,
            VodRequestStatus.QUEUED,
        }:
            vod.status = mp_state
        else:
            vod.status = VodRequestStatus.APPROVED
        vod.fail_reason = None
        await _apply_quota(user, vod.media_type)
        await _attach_quota_meta(vod, quota_subscription, db)
    except MoviePilotError as exc:
        vod.status = VodRequestStatus.FAILED
        vod.fail_reason = str(exc)

    vod.updated_at = datetime.utcnow()
    db.add(user)
    db.add(vod)
    if vod.status in {
        VodRequestStatus.APPROVED,
        VodRequestStatus.QUEUED,
        VodRequestStatus.DOWNLOADING,
        VodRequestStatus.SUCCEEDED,
    }:
        await create_telegram_notification(
            db,
            user_id=vod.user_id,
            notification_type="vod_approved",
            title="点播请求已通过",
            content=f"「{vod.title}」已开始处理",
            reference_id=str(vod.id),
        )
    await db.commit()
    await _invalidate_user_vod_cache(vod.user_id)
    return _response({"id": str(vod.id), "status": vod.status})


@router.post("/requests/{vod_id}/reject", response_model=dict)
async def reject_vod_request(
    vod_id: UUID,
    payload: VodRejectRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = select(VodRequest).where(VodRequest.id == vod_id)
    vod = (await db.execute(stmt)).scalar()
    if not vod:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="点播请求不存在")
    vod.status = VodRequestStatus.REJECTED
    vod.fail_reason = payload.reason
    vod.updated_at = datetime.utcnow()
    db.add(vod)
    await create_telegram_notification(
        db,
        user_id=vod.user_id,
        notification_type="vod_rejected",
        title="点播请求已拒绝",
        content=f"「{vod.title}」未通过审核",
        reference_id=str(vod.id),
    )
    await db.commit()
    await _invalidate_user_vod_cache(vod.user_id)
    return _response({"id": str(vod.id), "status": vod.status})


@router.post("/requests/batch-delete", response_model=dict)
async def batch_delete_vod_requests(
    payload: VodBatchDeleteRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    result = await _delete_vod_requests_by_ids(payload.ids, db)
    return _response(result)


@router.get("/settings", response_model=dict)
async def get_vod_settings(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    settings_row = await _get_settings(db)
    return _response(VodSettingsOut(auto_approve=settings_row.auto_approve).model_dump(mode="json"))


@router.put("/settings", response_model=dict)
async def update_vod_settings(
    payload: VodSettingsUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    settings_row = await _get_settings(db)
    settings_row.auto_approve = payload.auto_approve
    db.add(settings_row)
    await db.commit()
    return _response(VodSettingsOut(auto_approve=settings_row.auto_approve).model_dump(mode="json"))
