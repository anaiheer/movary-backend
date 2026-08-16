import time
from datetime import datetime
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.moviepilot import MoviePilotServer
from app.models.user import User, UserRole
from app.schemas.moviepilot import (
    MoviePilotProbeResponse,
    MoviePilotServerCreate,
    MoviePilotServerOut,
    MoviePilotServersResponse,
    MoviePilotServerUpdate,
)


router = APIRouter(prefix="/admin/moviepilot-servers", tags=["admin-moviepilot"])


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def _build_headers(api_token: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-Api-Key"] = api_token
        headers["X-API-KEY"] = api_token
        headers["X-Token"] = api_token
    return headers


async def _ensure_default(db: AsyncSession, new_default_id: str | None = None) -> None:
    if new_default_id is None:
        stmt = select(MoviePilotServer).order_by(MoviePilotServer.created_at.asc())
        servers = (await db.execute(stmt)).scalars().all()
        if servers:
            servers[0].is_default = True
            for s in servers[1:]:
                s.is_default = False
            db.add_all(servers)
            await db.commit()
        return

    stmt = select(MoviePilotServer)
    servers = (await db.execute(stmt)).scalars().all()
    for s in servers:
        s.is_default = s.id == new_default_id
    db.add_all(servers)
    await db.commit()


@router.get("", response_model=MoviePilotServersResponse)
async def list_moviepilot_servers(
    q: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    stmt = select(MoviePilotServer).order_by(MoviePilotServer.created_at.asc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (MoviePilotServer.name.ilike(like)) | (MoviePilotServer.base_url.ilike(like))
        )
    servers = (await db.execute(stmt)).scalars().all()
    items = [
        MoviePilotServerOut(
            id=row.id,
            name=row.name,
            base_url=row.base_url,
            is_active=row.is_active,
            is_default=row.is_default,
            status=row.status,
            latency=row.latency,
            last_check_at=row.last_check_at,
            created_at=row.created_at,
        )
        for row in servers
    ]
    return MoviePilotServersResponse(servers=items)


@router.post("", response_model=MoviePilotServerOut, status_code=status.HTTP_201_CREATED)
async def create_moviepilot_server(
    payload: MoviePilotServerCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    server = MoviePilotServer(
        name=payload.name,
        base_url=str(payload.base_url),
        api_token=payload.api_token,
        is_active=payload.is_active,
        is_default=payload.is_default,
        status="OFFLINE",
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    await _ensure_default(db, server.id if payload.is_default else None)
    await db.refresh(server)
    return MoviePilotServerOut(
        id=server.id,
        name=server.name,
        base_url=server.base_url,
        is_active=server.is_active,
        is_default=server.is_default,
        status=server.status,
        latency=server.latency,
        last_check_at=server.last_check_at,
        created_at=server.created_at,
    )


@router.patch("/{server_id}", response_model=MoviePilotServerOut)
async def update_moviepilot_server(
    server_id: str,
    payload: MoviePilotServerUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    stmt = select(MoviePilotServer).where(MoviePilotServer.id == server_id)
    server = (await db.execute(stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    if payload.name is not None:
        server.name = payload.name
    if payload.base_url is not None:
        server.base_url = str(payload.base_url)
    if payload.api_token is not None:
        server.api_token = payload.api_token
    if payload.is_active is not None:
        server.is_active = payload.is_active
    if payload.is_default is not None:
        server.is_default = payload.is_default

    db.add(server)
    await db.commit()
    await db.refresh(server)
    if payload.is_default:
        await _ensure_default(db, server.id)
    elif payload.is_default is False:
        await _ensure_default(db, None)
    await db.refresh(server)

    return MoviePilotServerOut(
        id=server.id,
        name=server.name,
        base_url=server.base_url,
        is_active=server.is_active,
        is_default=server.is_default,
        status=server.status,
        latency=server.latency,
        last_check_at=server.last_check_at,
        created_at=server.created_at,
    )


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_moviepilot_server(
    server_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    stmt = select(MoviePilotServer).where(MoviePilotServer.id == server_id)
    server = (await db.execute(stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")
    await db.delete(server)
    await db.commit()
    await _ensure_default(db, None)
    return None


@router.post("/{server_id}/probe", response_model=MoviePilotProbeResponse)
async def probe_moviepilot_server(
    server_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    stmt = select(MoviePilotServer).where(MoviePilotServer.id == server_id)
    server = (await db.execute(stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    base = server.base_url.rstrip("/")
    endpoints = [
        "/api/v1/dashboard/statistic2",
        "/api/v3/system/status",
        "/api/v1/system/global",
    ]
    status_flag = "OFFLINE"
    message = None
    auth_failed_seen = False
    start = time.perf_counter()
    for path in endpoints:
        url = base + path
        params = {}
        if server.api_token:
            params["token"] = server.api_token
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                resp = await client.get(
                    url, headers=_build_headers(server.api_token), params=params
                )
            if resp.status_code == 200:
                status_flag = "ONLINE"
                message = None
                break
            if resp.status_code in {401, 403}:
                auth_failed_seen = True
                message = "AUTH_FAILED"
                continue
            message = f"Unexpected status {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            message = str(exc)

    if status_flag != "ONLINE" and auth_failed_seen:
        status_flag = "AUTH_FAILED"
        message = "请检查 MoviePilot API Token"

    latency_ms = int((time.perf_counter() - start) * 1000)
    server.status = status_flag
    server.latency = latency_ms
    server.last_check_at = datetime.utcnow()
    db.add(server)
    await db.commit()

    return MoviePilotProbeResponse(
        id=server.id, status=status_flag, latency=latency_ms, message=message
    )
