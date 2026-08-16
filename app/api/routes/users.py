from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, hash_password
from app.db.session import get_db
from app.models.subscription import Subscription
from app.models.user import User, UserRole, UserStatus
from app.models.emby import EmbyServer, EmbyAccount
from app.core.config import settings
from app.schemas.user import (
    AdminUserCreate,
    BulkImportRequest,
    BulkImportResult,
    UserListItem,
    UserListResponse,
    UserStats,
    UserStatusUpdate,
    UserTrendPoint,
)

router = APIRouter(prefix="/users", tags=["users"])


async def _ensure_admin_user(db: AsyncSession) -> None:
    now = datetime.utcnow()

    admin_username = settings.ADMIN_USERNAME
    admin_password = settings.ADMIN_PASSWORD
    admin_email = settings.ADMIN_EMAIL

    admin_stmt = select(User).where(User.username == admin_username)
    admin_user = (await db.execute(admin_stmt)).scalar()
    if admin_user:
        admin_user.role = UserRole.ADMIN
        admin_user.status = UserStatus.ACTIVE
        if admin_email and not admin_user.email:
            admin_user.email = admin_email
        admin_user.email_verified = True
        admin_user.email_verified_at = admin_user.email_verified_at or now
        db.add(admin_user)
        await db.commit()
        return

    admin_user = User(
        email=admin_email,
        username=admin_username,
        password_hash=hash_password(admin_password),
        status=UserStatus.ACTIVE,
        role=UserRole.ADMIN,
        email_verified=True,
        email_verified_at=now,
        created_at=now,
        last_login_at=None,
    )
    db.add(admin_user)
    await db.commit()


async def _ensure_default_server(db: AsyncSession) -> EmbyServer:
    stmt = select(EmbyServer).order_by(EmbyServer.created_at.asc())
    server = (await db.execute(stmt)).scalars().first()
    if server:
        return server
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Emby 服务器不存在")


async def _load_expired_at(db: AsyncSession, user_ids: list) -> dict:
    if not user_ids:
        return {}
    sub_stmt = (
        select(Subscription.user_id, func.max(Subscription.end_at).label("expired_at"))
        .where(Subscription.user_id.in_(user_ids))
        .group_by(Subscription.user_id)
    )
    result = await db.execute(sub_stmt)
    return {row.user_id: row.expired_at for row in result}


def _build_trend(users: list[User]) -> list[UserTrendPoint]:
    buckets: dict[str, int] = {}
    for user in users:
        label = user.created_at.strftime("%m-%d") if user.created_at else "-"
        buckets[label] = buckets.get(label, 0) + 1
    ordered = sorted(buckets.items(), key=lambda kv: kv[0])
    return [UserTrendPoint(label=label, count=count) for label, count in ordered][-14:]


def _build_stats(users: list[User]) -> UserStats:
    return UserStats(
        total=len(users),
        active=len([u for u in users if u.status == UserStatus.ACTIVE]),
        banned=len([u for u in users if u.status == UserStatus.BANNED]),
        admins=len([u for u in users if u.role in {UserRole.ADMIN, UserRole.SUPERADMIN}]),
    )


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    result = await db.execute(stmt)
    actor = result.scalar()
    if not actor or actor.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return actor


@router.get("", response_model=UserListResponse)
async def list_users(
    keyword: str = Query("", description="用户名/邮箱 模糊匹配"),
    status: str | None = Query(None, description="用户状态"),
    role: str | None = Query(None, description="角色"),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户列表"""

    stmt = select(User)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.email.ilike(like)))
    if status:
        try:
            stmt = stmt.where(User.status == UserStatus(status))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的状态")
    if role:
        try:
            stmt = stmt.where(User.role == UserRole(role))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的角色")

    stmt = stmt.order_by(User.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    records = result.scalars().all()

    expired_map = await _load_expired_at(db, [u.id for u in records])

    items = [
        UserListItem(
            id=user.id,
            email=user.email,
            username=user.username,
            phone=user.phone,
            status=user.status.value,
            role=user.role.value,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            expired_at=expired_map.get(user.id),
        )
        for user in records
    ]

    return UserListResponse(items=items, stats=_build_stats(records), trend=_build_trend(records))


@router.post("", response_model=UserListItem, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    dup_stmt = select(User).where(
        (User.email == payload.email) | (User.username == payload.username),
        User.deleted_at.is_(None),
    )
    if (await db.execute(dup_stmt)).scalar():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱或用户名已存在")

    user = User(
        email=payload.email,
        username=payload.username,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        status=payload.status,
        role=payload.role,
        email_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return UserListItem(
        id=user.id,
        email=user.email,
        username=user.username,
        phone=user.phone,
        status=user.status.value,
        role=user.role.value,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        expired_at=None,
    )


@router.post("/import", response_model=BulkImportResult)
async def bulk_import_users(
    payload: BulkImportRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    created_items: list[UserListItem] = []
    skipped = 0

    for record in payload.users:
        dup_stmt = select(User).where(
            (User.email == record.email) | (User.username == record.username),
            User.deleted_at.is_(None),
        )
        if (await db.execute(dup_stmt)).scalar():
            skipped += 1
            continue

        user = User(
            email=record.email,
            username=record.username,
            phone=record.phone,
            password_hash=hash_password(record.password),
            status=record.status,
            role=record.role,
            email_verified=True,
            email_verified_at=datetime.utcnow(),
        )
        db.add(user)
        await db.flush()

        created_items.append(
            UserListItem(
                id=user.id,
                email=user.email,
                username=user.username,
                phone=user.phone,
                status=user.status.value,
                role=user.role.value,
                created_at=user.created_at,
                last_login_at=user.last_login_at,
                expired_at=None,
            )
        )

    await db.commit()

    return BulkImportResult(created=len(created_items), skipped=skipped, items=created_items)


@router.post("/sync-emby", response_model=dict)
async def sync_emby_accounts(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    server = await _ensure_default_server(db)

    users_stmt = select(User)
    users = (await db.execute(users_stmt)).scalars().all()

    existing_stmt = select(EmbyAccount.user_id)
    existing_ids = {row[0] for row in (await db.execute(existing_stmt)).all()}

    created = 0
    for user in users:
        if user.id in existing_ids:
            continue
        account = EmbyAccount(
            user_id=user.id,
            emby_server_id=server.id,
            emby_user_id=f"emby-{user.username}",
            emby_username=user.username,
            status="ENABLED" if user.status == UserStatus.ACTIVE else "DISABLED",
        )
        db.add(account)
        created += 1

    await db.commit()
    return {"synced": created, "server_id": str(server.id)}


@router.patch("/{user_id}/status", response_model=UserListItem)
async def update_user_status(
    user_id: str,
    payload: UserStatusUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新用户状态/角色（仅管理员）"""
    await _ensure_admin(current_user, db)

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    user.status = payload.status
    if payload.role:
        user.role = payload.role

    db.add(user)
    await db.commit()
    await db.refresh(user)

    expired_at = await db.scalar(
        select(func.max(Subscription.end_at)).where(Subscription.user_id == user.id)
    )

    return UserListItem(
        id=user.id,
        email=user.email,
        username=user.username,
        phone=user.phone,
        status=user.status.value,
        role=user.role.value,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        expired_at=expired_at,
    )
