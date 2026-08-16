from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pro_extensions import get_backend_extension_state
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.emby import EmbyServer
from app.models.moviepilot import MoviePilotServer
from app.models.user import User, UserRole
from app.schemas.server_config import (
    ManagedEmbyServerOut,
    ManagedEmbyServerUpsert,
    ManagedMoviePilotServerOut,
    ManagedMoviePilotServerUpsert,
    ServerConfigData,
    ServerConfigSummary,
)

router = APIRouter(prefix="/admin/server-config", tags=["admin-server-config"])


def _response(data: ServerConfigData | dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def _load_emby_servers(db: AsyncSession) -> list[EmbyServer]:
    stmt = select(EmbyServer).order_by(EmbyServer.priority.asc(), EmbyServer.created_at.asc())
    return (await db.execute(stmt)).scalars().all()


async def _load_moviepilot_servers(db: AsyncSession) -> list[MoviePilotServer]:
    stmt = select(MoviePilotServer).order_by(MoviePilotServer.created_at.asc())
    return (await db.execute(stmt)).scalars().all()


def _serialize_emby(server: EmbyServer | None) -> ManagedEmbyServerOut | None:
    if not server:
        return None
    return ManagedEmbyServerOut(
        id=server.id,
        name=server.name,
        base_url=server.base_url,
        external_url=server.external_url,
        webhook_url=server.webhook_url,
        description=server.description,
        is_active=bool(server.is_active),
        created_at=server.created_at,
    )


def _serialize_moviepilot(server: MoviePilotServer | None) -> ManagedMoviePilotServerOut | None:
    if not server:
        return None
    return ManagedMoviePilotServerOut(
        id=server.id,
        name=server.name,
        base_url=server.base_url,
        is_active=bool(server.is_active),
        created_at=server.created_at,
    )


def _build_summary(
    emby_servers: list[EmbyServer], moviepilot_servers: list[MoviePilotServer]
) -> ServerConfigSummary:
    notices: list[str] = []
    primary_emby = emby_servers[0] if emby_servers else None
    extra_emby_servers = max(len(emby_servers) - 1, 0)
    extra_moviepilot_servers = max(len(moviepilot_servers) - 1, 0)
    emby_locked = extra_emby_servers > 0 or bool(primary_emby and primary_emby.backup_url)
    moviepilot_locked = extra_moviepilot_servers > 0

    if extra_emby_servers > 0:
        notices.append("检测到额外 Emby 服务器数据，基础版轻量配置已锁定 Emby 管理。")
    if primary_emby and primary_emby.backup_url:
        notices.append("检测到 Emby 备用线路配置，该能力属于专业版。")
    if extra_moviepilot_servers > 0:
        notices.append("检测到额外 MoviePilot 服务数据，基础版轻量配置已锁定 MoviePilot 管理。")

    return ServerConfigSummary(
        emby_count=len(emby_servers),
        moviepilot_count=len(moviepilot_servers),
        extra_emby_servers=extra_emby_servers,
        extra_moviepilot_servers=extra_moviepilot_servers,
        emby_locked=emby_locked,
        moviepilot_locked=moviepilot_locked,
        pro_data_detected=emby_locked or moviepilot_locked,
        notices=notices,
    )


async def _load_config_data(db: AsyncSession) -> ServerConfigData:
    emby_servers = await _load_emby_servers(db)
    moviepilot_servers = await _load_moviepilot_servers(db)
    summary = _build_summary(emby_servers, moviepilot_servers)
    extension_state = get_backend_extension_state()
    summary.pro_server_extension_loaded = any(
        "advanced-servers" in loaded.get("route_groups", []) for loaded in extension_state["loaded"]
    )
    summary.pro_server_admin_path = (
        "/admin/services" if summary.pro_server_extension_loaded else None
    )
    return ServerConfigData(
        emby_server=_serialize_emby(emby_servers[0] if emby_servers else None),
        moviepilot_server=_serialize_moviepilot(
            moviepilot_servers[0] if moviepilot_servers else None
        ),
        summary=summary,
    )


@router.get("")
async def get_server_config(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    return _response(await _load_config_data(db))


@router.put("/emby")
async def upsert_emby_server(
    payload: ManagedEmbyServerUpsert,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    emby_servers = await _load_emby_servers(db)
    summary = _build_summary(emby_servers, await _load_moviepilot_servers(db))
    if summary.emby_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="检测到专业版 Emby 数据，基础版轻量配置暂不可直接覆盖。",
        )

    server = emby_servers[0] if emby_servers else EmbyServer(priority=0, is_default=True)
    if not emby_servers and not payload.api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="首次创建 Emby 配置时必须提供 API 密钥",
        )
    server.name = payload.name
    server.base_url = str(payload.base_url)
    server.external_url = str(payload.external_url) if payload.external_url else None
    if payload.api_key:
        server.api_key = payload.api_key
    server.webhook_url = str(payload.webhook_url) if payload.webhook_url else None
    server.description = payload.description
    server.priority = 0
    server.backup_url = None
    server.is_default = True
    server.is_active = payload.is_active
    db.add(server)
    await db.commit()
    return _response(await _load_config_data(db), "Emby 配置已保存")


@router.delete("/emby", status_code=status.HTTP_200_OK)
async def delete_emby_server(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    emby_servers = await _load_emby_servers(db)
    summary = _build_summary(emby_servers, await _load_moviepilot_servers(db))
    if summary.emby_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="检测到专业版 Emby 数据，请先在专业版环境中完成清理或迁移。",
        )
    if not emby_servers:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到 Emby 配置")
    await db.delete(emby_servers[0])
    await db.commit()
    return _response(await _load_config_data(db), "Emby 配置已删除")


@router.put("/moviepilot")
async def upsert_moviepilot_server(
    payload: ManagedMoviePilotServerUpsert,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    moviepilot_servers = await _load_moviepilot_servers(db)
    summary = _build_summary(await _load_emby_servers(db), moviepilot_servers)
    if summary.moviepilot_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="检测到专业版 MoviePilot 数据，基础版轻量配置暂不可直接覆盖。",
        )

    server = (
        moviepilot_servers[0]
        if moviepilot_servers
        else MoviePilotServer(is_default=True, status="OFFLINE")
    )
    server.name = payload.name
    server.base_url = str(payload.base_url)
    server.api_token = payload.api_token
    server.is_active = payload.is_active
    server.is_default = True
    db.add(server)
    await db.commit()
    return _response(await _load_config_data(db), "MoviePilot 配置已保存")


@router.delete("/moviepilot", status_code=status.HTTP_200_OK)
async def delete_moviepilot_server(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    moviepilot_servers = await _load_moviepilot_servers(db)
    summary = _build_summary(await _load_emby_servers(db), moviepilot_servers)
    if summary.moviepilot_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="检测到专业版 MoviePilot 数据，请先在专业版环境中完成清理或迁移。",
        )
    if not moviepilot_servers:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到 MoviePilot 配置")
    await db.delete(moviepilot_servers[0])
    await db.commit()
    return _response(await _load_config_data(db), "MoviePilot 配置已删除")
