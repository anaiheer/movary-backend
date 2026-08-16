import time
from datetime import datetime
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.emby import EmbyAccount, EmbyServer
from app.models.subscription import Subscription, SubscriptionStatus, Plan, PlanServerAssignment
from app.models.user import User, UserStatus
from app.schemas.emby import (
    EmbyAccountsResponse,
    EmbyProbeResponse,
    EmbyServerCreate,
    EmbyServerStats,
    EmbyServerOut,
    EmbyServerUpdate,
    EmbyServersResponse,
    EmbyUserAccountsResponse,
    EmbyUserServerOut,
    EmbyUserServersResponse,
    EmbyPasswordUpdate,
)
from app.services.emby_accounts import update_emby_password_for_user
from app.services.emby_passwords import decrypt_emby_password


router = APIRouter(prefix="/emby", tags=["emby"])


def _server_status(server: EmbyServer) -> str:
    return "ONLINE" if server.is_active else "OFFLINE"


async def _serialize_servers(rows) -> EmbyServersResponse:
    payload: list[EmbyServerOut] = []
    for server, user_count in rows:
        payload.append(
            EmbyServerOut(
                id=server.id,
                name=server.name,
                base_url=server.base_url,
                external_url=server.external_url,
                backup_url=server.backup_url,
                webhook_url=server.webhook_url,
                description=server.description,
                priority=server.priority,
                status=_server_status(server),
                latency=0,
                library=None,
                is_default=server.is_default,
                is_active=server.is_active,
                user_count=user_count or 0,
                created_at=server.created_at,
            )
        )
    return EmbyServersResponse(servers=payload)


async def _ensure_default(db: AsyncSession, new_default_id: str | None = None) -> None:
    if new_default_id is None:
        # 如果当前没有指定默认线路，则将优先级最高的服务器设为默认
        stmt = select(EmbyServer).order_by(EmbyServer.priority.asc(), EmbyServer.created_at.asc())
        servers = (await db.execute(stmt)).scalars().all()
        if servers:
            servers[0].is_default = True
            for s in servers[1:]:
                s.is_default = False
            db.add_all(servers)
            await db.commit()
        return

    # 将指定服务器设为默认，其余服务器取消默认状态
    stmt = select(EmbyServer)
    servers = (await db.execute(stmt)).scalars().all()
    for s in servers:
        s.is_default = s.id == new_default_id
    db.add_all(servers)
    await db.commit()


@router.get("", response_model=EmbyServersResponse)
async def list_emby_servers(
    q: str | None = Query(default=None, description="名称或 URL 模糊查询"),
    active_only: bool = Query(default=False, description="仅返回已启用的服务器"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q:
        like = f"%{q}%"
        filters.append((EmbyServer.name.ilike(like)) | (EmbyServer.base_url.ilike(like)))

    stmt = (
        select(EmbyServer, func.count(EmbyAccount.id))
        .outerjoin(EmbyAccount, EmbyAccount.emby_server_id == EmbyServer.id)
        .group_by(EmbyServer.id)
        .order_by(EmbyServer.priority.asc(), EmbyServer.created_at.asc())
    )
    if active_only:
        filters.append(EmbyServer.is_active.is_(True))
    if filters:
        stmt = stmt.where(*filters)

    rows = (await db.execute(stmt)).all()
    if not rows:
        return EmbyServersResponse(servers=[])
    return await _serialize_servers(rows)


def _build_subscription_status(
    sub_status: SubscriptionStatus | None, sub_end: datetime | None
) -> str:
    if sub_status is None or sub_end is None:
        return "NONE"
    if sub_end < datetime.utcnow():
        return "EXPIRED"
    return sub_status.value


async def _compute_stats(server_id: str, db: AsyncSession) -> EmbyServerStats:
    server_stmt = select(EmbyServer).where(EmbyServer.id == server_id)
    server = (await db.execute(server_stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    base_query = (
        select(
            func.count(EmbyAccount.id).label("user_total"),
            func.sum(case((EmbyAccount.status == "ENABLED", 1), else_=0)).label("enabled_accounts"),
            func.sum(case((EmbyAccount.status == "DISABLED", 1), else_=0)).label(
                "disabled_accounts"
            ),
            func.sum(case((User.status == UserStatus.ACTIVE, 1), else_=0)).label("active_users"),
            func.sum(case((User.status == UserStatus.BANNED, 1), else_=0)).label("banned_users"),
            func.sum(case((Subscription.end_at < datetime.utcnow(), 1), else_=0)).label(
                "expired_subscriptions"
            ),
        )
        .join(User, User.id == EmbyAccount.user_id)
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .where(EmbyAccount.emby_server_id == server_id)
    )

    stats_row = (await db.execute(base_query)).one()
    return EmbyServerStats(
        server_id=server.id,
        name=server.name,
        base_url=server.base_url,
        user_total=stats_row.user_total or 0,
        enabled_accounts=stats_row.enabled_accounts or 0,
        disabled_accounts=stats_row.disabled_accounts or 0,
        active_users=stats_row.active_users or 0,
        banned_users=stats_row.banned_users or 0,
        expired_subscriptions=stats_row.expired_subscriptions or 0,
        created_at=server.created_at,
        is_active=server.is_active,
        is_default=server.is_default,
        priority=server.priority,
    )


async def _compute_remote_stats(server_id: str, db: AsyncSession) -> EmbyServerStats:
    server_stmt = select(EmbyServer).where(EmbyServer.id == server_id)
    server = (await db.execute(server_stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    endpoint = urljoin(server.base_url.rstrip("/") + "/", "Users")
    params = {"api_key": server.api_key}
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.get(endpoint, params=params)
            res.raise_for_status()
            payload = res.json() or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"获取 Emby 统计失败：{exc}"
        ) from exc

    user_total = len(payload)
    disabled_accounts = 0
    banned_users = 0
    for user in payload:
        policy = user.get("Policy", {}) or {}
        if policy.get("IsDisabled", False):
            disabled_accounts += 1
        if policy.get("IsBanned", False):
            banned_users += 1

    enabled_accounts = max(user_total - disabled_accounts, 0)
    active_users = enabled_accounts

    return EmbyServerStats(
        server_id=server.id,
        name=server.name,
        base_url=server.base_url,
        user_total=user_total,
        enabled_accounts=enabled_accounts,
        disabled_accounts=disabled_accounts,
        active_users=active_users,
        banned_users=banned_users,
        expired_subscriptions=0,
        created_at=server.created_at,
        is_active=server.is_active,
        is_default=server.is_default,
        priority=server.priority,
    )


async def _fetch_remote_accounts(
    server: EmbyServer, keyword: str | None, page: int, size: int
) -> EmbyAccountsResponse:
    endpoint = urljoin(server.base_url.rstrip("/") + "/", "Users")
    params = {"api_key": server.api_key}
    try:
        async with httpx.AsyncClient(timeout=5, verify=False) as client:
            res = await client.get(endpoint, params=params)
            res.raise_for_status()
            payload = res.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"获取 Emby 用户失败：{exc}"
        ) from exc

    items = []
    for user in payload or []:
        if keyword:
            if keyword.lower() not in (user.get("Name", "").lower()):
                continue
        policy = user.get("Policy", {}) or {}
        is_disabled = policy.get("IsDisabled", False)
        last_login = user.get("LastLoginDate") or user.get("LastActivityDate")
        created_at = user.get("DateCreated")
        items.append(
            {
                "id": str(user.get("Id")),
                "user_id": str(user.get("Id")),
                "username": user.get("Name", ""),
                "email": user.get("Email") or None,
                "account_status": "DISABLED" if is_disabled else "ENABLED",
                "user_status": "ACTIVE",
                "emby_status": "NORMAL" if not is_disabled else "DISABLED",
                "subscription_status": "NONE",
                "last_login_at": last_login,
                "created_at": created_at,
            }
        )

    total = len(items)
    start = (page - 1) * size
    end = start + size
    return EmbyAccountsResponse(items=items[start:end], total=total)


@router.post("", response_model=EmbyServerOut, status_code=status.HTTP_201_CREATED)
async def create_emby_server(
    payload: EmbyServerCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = EmbyServer(
        name=payload.name,
        base_url=str(payload.base_url),
        external_url=str(payload.external_url) if payload.external_url else None,
        backup_url=str(payload.backup_url) if payload.backup_url else None,
        api_key=payload.api_key,
        webhook_url=str(payload.webhook_url) if payload.webhook_url else None,
        description=payload.description,
        priority=payload.priority,
        is_active=payload.is_active,
        is_default=payload.is_default,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    # 如果没有默认线路，则将首个设置为默认
    await _ensure_default(db, server.id if payload.is_default else None)
    await db.refresh(server)

    resp = await _serialize_servers([(server, 0)])
    return resp.servers[0]


@router.patch("/{server_id}", response_model=EmbyServerOut)
async def update_emby_server(
    server_id: str,
    payload: EmbyServerUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmbyServer).where(EmbyServer.id == server_id)
    server = (await db.execute(stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    if payload.name is not None:
        server.name = payload.name
    if payload.base_url is not None:
        server.base_url = str(payload.base_url)
    if payload.external_url is not None:
        server.external_url = str(payload.external_url) if payload.external_url else None
    if payload.backup_url is not None:
        server.backup_url = str(payload.backup_url) if payload.backup_url else None
    if payload.api_key is not None:
        server.api_key = payload.api_key
    if payload.webhook_url is not None:
        server.webhook_url = str(payload.webhook_url) if payload.webhook_url else None
    if payload.description is not None:
        server.description = payload.description
    if payload.priority is not None:
        server.priority = payload.priority
    if payload.is_active is not None:
        server.is_active = payload.is_active
    if payload.is_default is not None:
        server.is_default = payload.is_default

    db.add(server)
    await db.commit()
    await db.refresh(server)

    # 保证存在唯一默认线路
    if payload.is_default:
        await _ensure_default(db, server.id)
    elif payload.is_default is False:
        await _ensure_default(db, None)
    await db.refresh(server)

    user_count_stmt = select(func.count(EmbyAccount.id)).where(
        EmbyAccount.emby_server_id == server.id
    )
    user_count = (await db.execute(user_count_stmt)).scalar() or 0

    resp = await _serialize_servers([(server, user_count)])
    return resp.servers[0]


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_emby_server(
    server_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmbyServer).where(EmbyServer.id == server_id)
    server = (await db.execute(stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    await db.delete(server)
    await db.commit()

    # 确保仍有默认线路
    await _ensure_default(db, None)
    return None


@router.post("/{server_id}/probe", response_model=EmbyProbeResponse)
async def probe_emby_server(
    server_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmbyServer).where(EmbyServer.id == server_id)
    server = (await db.execute(stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    endpoint = urljoin(server.base_url.rstrip("/") + "/", "System/Ping")
    params = {"api_key": server.api_key}
    start = time.perf_counter()
    status_flag = _server_status(server)
    message = None
    try:
        async with httpx.AsyncClient(timeout=3.5, verify=False) as client:
            res = await client.get(endpoint, params=params)
            latency_ms = int((time.perf_counter() - start) * 1000)
            if res.status_code == 200:
                status_flag = "ONLINE"
            else:
                status_flag = "OFFLINE"
                message = f"Unexpected status {res.status_code}"
            return EmbyProbeResponse(
                id=server.id, status=status_flag, latency=latency_ms, message=message
            )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - start) * 1000)
        status_flag = "OFFLINE"
        message = str(exc)
        return EmbyProbeResponse(
            id=server.id, status=status_flag, latency=latency_ms, message=message
        )


@router.get("/me", response_model=EmbyServersResponse)
async def get_my_emby(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_emby_servers(q=None, current_user=current_user, db=db)


@router.get("/me/accounts", response_model=EmbyUserAccountsResponse)
async def get_my_emby_accounts(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    sub_stmt = (
        select(Subscription.plan_id)
        .where(
            Subscription.user_id == current_user["user_id"],
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > now,
        )
        .distinct()
    )
    plan_ids = [row[0] for row in (await db.execute(sub_stmt)).all()]
    if not plan_ids:
        return EmbyUserAccountsResponse(items=[])

    server_stmt = select(PlanServerAssignment.server_id).where(
        PlanServerAssignment.plan_id.in_(plan_ids)
    )
    server_ids = [row[0] for row in (await db.execute(server_stmt)).all()]
    if not server_ids:
        return EmbyUserAccountsResponse(items=[])

    stmt = (
        select(EmbyAccount, EmbyServer)
        .join(EmbyServer, EmbyServer.id == EmbyAccount.emby_server_id)
        .where(
            EmbyAccount.user_id == current_user["user_id"],
            EmbyAccount.emby_server_id.in_(server_ids),
        )
        .order_by(EmbyAccount.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    items = []
    for account, server in rows:
        items.append(
            {
                "server_id": server.id,
                "server_name": server.name,
                "base_url": server.base_url,
                "external_url": server.external_url or server.base_url,
                "backup_url": server.backup_url,
                "username": account.emby_username,
                "emby_password": decrypt_emby_password(account.emby_password),
                "status": account.status,
                "created_at": account.created_at,
            }
        )
    return EmbyUserAccountsResponse(items=items)


@router.patch("/me/password", response_model=dict)
async def update_my_emby_password(
    payload: EmbyPasswordUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.id == current_user["user_id"]))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    await update_emby_password_for_user(db, user, payload.password)
    await db.commit()
    return {"success": True, "message": "password updated"}


@router.get("/me/servers", response_model=EmbyUserServersResponse)
async def get_my_emby_servers(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    sub_stmt = (
        select(Subscription.plan_id)
        .where(
            Subscription.user_id == current_user["user_id"],
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > now,
        )
        .distinct()
    )
    plan_ids = [row[0] for row in (await db.execute(sub_stmt)).all()]
    if not plan_ids:
        return EmbyUserServersResponse(items=[])

    stmt = (
        select(PlanServerAssignment, EmbyServer, Plan)
        .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
        .join(Plan, Plan.id == PlanServerAssignment.plan_id)
        .where(PlanServerAssignment.plan_id.in_(plan_ids))
    )
    rows = (await db.execute(stmt)).all()
    items: list[EmbyUserServerOut] = []
    for assignment, server, plan in rows:
        items.append(
            EmbyUserServerOut(
                server_id=server.id,
                server_name=server.name,
                base_url=server.base_url,
                external_url=server.external_url or server.base_url,
                plan_id=plan.id,
                plan_name=plan.name,
            )
        )
    return EmbyUserServersResponse(items=items)


@router.get("/{server_id}/accounts", response_model=EmbyAccountsResponse)
async def list_emby_accounts(
    server_id: str,
    keyword: str = Query("", description="用户名或邮箱模糊匹配"),
    status_filter: str | None = Query(None, description="璐﹀彿鐘舵€?ENABLED/DISABLED"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    source: str = Query("local", description="local/remote"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server_stmt = select(EmbyServer).where(EmbyServer.id == server_id)
    server = (await db.execute(server_stmt)).scalar()
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")

    if source == "remote":
        return await _fetch_remote_accounts(server, keyword or None, page, size)

    stmt = (
        select(
            EmbyAccount,
            User,
            func.max(Subscription.end_at).label("sub_end"),
            func.max(Subscription.status).label("sub_status"),
        )
        .join(User, User.id == EmbyAccount.user_id)
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .where(EmbyAccount.emby_server_id == server_id)
        .group_by(EmbyAccount.id, User.id)
        .order_by(EmbyAccount.created_at.desc())
    )

    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where((User.username.ilike(like)) | (User.email.ilike(like)))

    if status_filter:
        stmt = stmt.where(EmbyAccount.status == status_filter)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    stmt = stmt.limit(size).offset((page - 1) * size)
    rows = (await db.execute(stmt)).all()

    items = []
    for account, user, sub_end, sub_status in rows:
        items.append(
            {
                "id": str(account.id),
                "user_id": str(user.id) if user else None,
                "username": user.username,
                "email": user.email,
                "account_status": account.status,
                "user_status": user.status.value
                if isinstance(user.status, UserStatus)
                else str(user.status),
                "emby_status": "NORMAL" if account.status == "ENABLED" else "DISABLED",
                "subscription_status": _build_subscription_status(
                    sub_status if isinstance(sub_status, SubscriptionStatus) else None, sub_end
                ),
                "last_login_at": user.last_login_at,
                "created_at": account.created_at,
            }
        )

    return EmbyAccountsResponse(items=items, total=total or 0)


@router.patch("/{server_id}/accounts/{account_id}", response_model=dict)
async def update_emby_account_status(
    server_id: str,
    account_id: str,
    is_disabled: bool = Query(False, description="设为禁用/启用"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmbyAccount).where(
        EmbyAccount.id == account_id, EmbyAccount.emby_server_id == server_id
    )
    account = (await db.execute(stmt)).scalar()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")

    account.status = "DISABLED" if is_disabled else "ENABLED"
    db.add(account)
    await db.commit()
    return {"status": account.status}


@router.delete("/{server_id}/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_emby_account(
    server_id: str,
    account_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmbyAccount).where(
        EmbyAccount.id == account_id, EmbyAccount.emby_server_id == server_id
    )
    account = (await db.execute(stmt)).scalar()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")

    await db.delete(account)
    await db.commit()
    return None


@router.get("/{server_id}/stats", response_model=EmbyServerStats)
async def get_emby_server_stats(
    server_id: str,
    source: str = Query("local", description="local/remote"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if source == "remote":
        return await _compute_remote_stats(server_id, db)
    return await _compute_stats(server_id, db)
