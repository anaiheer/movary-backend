from datetime import datetime, timedelta, timezone
import csv
import io
import logging
from urllib.parse import urljoin
from decimal import Decimal
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
import httpx
from starlette.responses import StreamingResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import validate_account_password
from app.core.public_urls import build_site_url
from app.core.security import get_current_user, hash_password
from app.db.session import get_db
from app.models.balance import BalanceTransaction
from app.models.emby import EmbyAccount, EmbyServer
from app.models.system_settings import SystemSettings
from app.models.subscription import (
    BillingCycle,
    Plan,
    PlanServerAssignment,
    Subscription,
    SubscriptionSource,
    SubscriptionStatus,
)
from app.models.telegram import TelegramNotificationPreference, TelegramUserBinding
from app.models.user import User, UserRole, UserStatus
from app.services.email import SmtpConfig, send_email
from app.services.email_templates import (
    EmailTemplateKey,
    build_email_template_context,
    render_email_template,
)
from app.services.emby_passwords import (
    decrypt_emby_password,
    encrypt_emby_password,
    migrate_emby_passwords,
)
from app.services.emby_accounts import (
    reconcile_emby_accounts_for_user,
    sync_emby_account_status_for_user,
)
from app.services.telegram import create_telegram_notification
from app.schemas.admin import (
    AdminUserCreate,
    AdminUserUpdate,
    AdminUsersResponse,
    BalanceAdjustRequest,
    BulkAssignSubscriptionRequest,
    BulkAssignSubscriptionResult,
    BulkExtendSubscriptionRequest,
    BulkExtendSubscriptionResult,
    EmbyImportRequest,
    EmbyImportResult,
    StatusUpdateRequest,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])
logger = logging.getLogger(__name__)


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="闇€瑕佺鐞嗗憳鏉冮檺")
    return user


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


def _current_active_subscription_exists(now: datetime):
    return (
        select(Subscription.id)
        .where(
            Subscription.user_id == User.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > now,
        )
        .exists()
    )


def _any_subscription_exists():
    return select(Subscription.id).where(Subscription.user_id == User.id).exists()


def _latest_subscription_status_subquery():
    return (
        select(Subscription.status)
        .where(Subscription.user_id == User.id)
        .order_by(Subscription.end_at.desc(), Subscription.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )


def _resolve_subscription_snapshot(
    user_subscription_rows: list[tuple[Subscription, Plan | None]], now: datetime
) -> dict:
    sorted_rows = sorted(
        user_subscription_rows,
        key=lambda row: ((row[0].end_at or datetime.min), (row[0].created_at or datetime.min)),
        reverse=True,
    )
    active_rows = [
        (sub, plan)
        for sub, plan in sorted_rows
        if sub.status == SubscriptionStatus.ACTIVE and sub.end_at > now
    ]
    effective_rows = [(sub, plan) for sub, plan in active_rows if sub.start_at <= now < sub.end_at]
    current_row = effective_rows[0] if effective_rows else (active_rows[0] if active_rows else None)
    latest_row = sorted_rows[0] if sorted_rows else None

    if active_rows:
        status_value = "ACTIVE"
        display_row = current_row
        latest_end_at = active_rows[0][0].end_at
    elif latest_row:
        latest_sub, latest_plan = latest_row
        status_value = (
            SubscriptionStatus.CANCELED.value
            if latest_sub.status == SubscriptionStatus.CANCELED
            else SubscriptionStatus.EXPIRED.value
        )
        display_row = (latest_sub, latest_plan)
        latest_end_at = latest_sub.end_at
    else:
        status_value = "NONE"
        display_row = None
        latest_end_at = None

    summary_items = [
        {
            "id": sub.id,
            "plan_id": sub.plan_id,
            "plan_name": plan.name if plan else None,
            "group_key": plan.group_key if plan else None,
            "group_name": plan.group_name if plan else None,
            "tier_level": int(plan.tier_level or 1) if plan else None,
            "status": sub.status.value,
            "billing_cycle": sub.billing_cycle.value
            if sub.billing_cycle
            else BillingCycle.UNSET.value,
            "start_at": sub.start_at,
            "end_at": sub.end_at,
            "is_current": sub.start_at <= now < sub.end_at,
            "is_trial": sub.billing_cycle == BillingCycle.TRIAL,
        }
        for sub, plan in active_rows
    ]

    if display_row:
        display_sub, display_plan = display_row
        subscription = {
            "status": status_value,
            "plan": {"id": display_plan.id, "name": display_plan.name} if display_plan else None,
            "billing_cycle": display_sub.billing_cycle.value
            if display_sub.billing_cycle
            else BillingCycle.UNSET.value,
            "start_at": display_sub.start_at,
            "end_at": latest_end_at,
        }
    else:
        display_sub = None
        subscription = {
            "status": "NONE",
            "plan": None,
            "billing_cycle": BillingCycle.UNSET.value,
            "start_at": None,
            "end_at": None,
        }

    return {
        "status": status_value,
        "display_sub": display_sub,
        "active_rows": active_rows,
        "latest_end_at": latest_end_at,
        "subscription": subscription,
        "summary_items": summary_items,
    }


def _map_status_to_admin(status: UserStatus) -> str:
    if status == UserStatus.ACTIVE:
        return "ENABLED"
    if status == UserStatus.BANNED:
        return "DISABLED"
    if status == UserStatus.ABNORMAL:
        return "ABNORMAL"
    return "DELETED"


def _map_status_to_model(status: str) -> UserStatus:
    if status == "ENABLED":
        return UserStatus.ACTIVE
    if status == "DISABLED":
        return UserStatus.BANNED
    if status == "ABNORMAL":
        return UserStatus.ABNORMAL
    if status == "DELETED":
        return UserStatus.DELETED
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的状态")


async def _get_system_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _build_smtp_config(row: SystemSettings) -> SmtpConfig:
    if not row.smtp_host or not row.smtp_port or not row.smtp_from:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SMTP 未配置")
    return SmtpConfig(
        host=row.smtp_host,
        port=row.smtp_port,
        username=row.smtp_user,
        password=row.smtp_password,
        sender=row.smtp_from,
        use_tls=row.smtp_use_tls,
        use_ssl=row.smtp_use_ssl,
    )


def _parse_dt(value):
    if isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        cleaned = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _normalize_account_status(value: str | None) -> str:
    if not value:
        return "DISABLED"
    return "ENABLED" if value.upper() == "ENABLED" else "DISABLED"


def _map_emby_user_status(account_status: str | None, user_status: str | None) -> UserStatus:
    if account_status:
        return (
            UserStatus.ACTIVE
            if _normalize_account_status(account_status) == "ENABLED"
            else UserStatus.BANNED
        )
    if user_status:
        normalized = user_status.upper()
        if normalized == "ACTIVE":
            return UserStatus.ACTIVE
        if normalized == "BANNED":
            return UserStatus.BANNED
        if normalized == "DELETED":
            return UserStatus.DELETED
    return UserStatus.ACTIVE


def _expected_emby_status(user_status: UserStatus) -> str:
    if user_status == UserStatus.ACTIVE:
        return "ENABLED"
    if user_status == UserStatus.BANNED:
        return "DISABLED"
    return "UNKNOWN"


def _billing_cycle_from_label(value: str) -> BillingCycle:
    mapping = {
        "月付": BillingCycle.MONTHLY,
        "季付": BillingCycle.QUARTERLY,
        "季度": BillingCycle.QUARTERLY,
        "年付": BillingCycle.YEARLY,
    }
    if value in mapping:
        return mapping[value]
    return BillingCycle(value)


def _billing_cycle_label(value: BillingCycle | None) -> str:
    if value == BillingCycle.MONTHLY:
        return "月付"
    if value == BillingCycle.QUARTERLY:
        return "季付"
    if value == BillingCycle.YEARLY:
        return "年付"
    return ""


def _parse_date_only(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    try:
        cleaned = cleaned.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _compute_end_at(start_at: datetime, cycle: BillingCycle) -> datetime:
    if cycle == BillingCycle.MONTHLY:
        return start_at + timedelta(days=30)
    if cycle == BillingCycle.QUARTERLY:
        return start_at + timedelta(days=90)
    if cycle == BillingCycle.YEARLY:
        return start_at + timedelta(days=365)
    return start_at


def _generate_emby_password() -> str:
    return uuid4().hex[:12]


async def _fetch_emby_user_map(server: EmbyServer) -> dict[str, dict]:
    endpoint = urljoin(server.base_url.rstrip("/") + "/", "Users")
    params = {"api_key": server.api_key}
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.get(endpoint, params=params)
            res.raise_for_status()
            payload = res.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"获取 Emby 用户失败：{exc}"
        ) from exc

    mapping: dict[str, dict] = {}
    for user in payload or []:
        name = user.get("Name") or ""
        policy = user.get("Policy", {}) or {}
        is_disabled = bool(policy.get("IsDisabled", False))
        mapping[name] = {"id": user.get("Id"), "status": "DISABLED" if is_disabled else "ENABLED"}
    return mapping


async def _ensure_emby_user(
    server: EmbyServer,
    username: str,
    password: str,
) -> tuple[str, str]:
    user_map = await _fetch_emby_user_map(server)
    emby_user_id = None
    if username in user_map and user_map[username].get("id"):
        emby_user_id = user_map[username]["id"]
    else:
        endpoint = urljoin(server.base_url.rstrip("/") + "/", "Users/New")
        params = {"api_key": server.api_key, "Name": username}
        try:
            async with httpx.AsyncClient(timeout=8, verify=False) as client:
                res = await client.post(endpoint, params=params)
                res.raise_for_status()
                payload = res.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"创建 Emby 用户失败：{exc}"
            ) from exc
        emby_user_id = payload.get("Id") or payload.get("User", {}).get("Id")
        if not emby_user_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="创建 Emby 用户返回结果无效"
            )

    password_endpoint = urljoin(server.base_url.rstrip("/") + "/", f"Users/{emby_user_id}/Password")
    params = {"api_key": server.api_key}
    body = {"NewPw": password, "ResetPw": False}
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.post(password_endpoint, params=params, json=body)
            res.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"设置 Emby 密码失败：{exc}"
        ) from exc

    return emby_user_id, username


async def _set_emby_password(server: EmbyServer, emby_user_id: str, password: str) -> None:
    password_endpoint = urljoin(server.base_url.rstrip("/") + "/", f"Users/{emby_user_id}/Password")
    params = {"api_key": server.api_key}
    body = {"NewPw": password, "ResetPw": False}
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.post(password_endpoint, params=params, json=body)
            res.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"设置 Emby 密码失败：{exc}"
        ) from exc


async def _ensure_emby_accounts(
    db: AsyncSession,
    user: User,
    plan_id,
    password: str | None,
) -> None:
    if not plan_id:
        return
    assign_stmt = (
        select(PlanServerAssignment, EmbyServer)
        .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
        .where(PlanServerAssignment.plan_id == plan_id, EmbyServer.is_active.is_(True))
    )
    assignments = (await db.execute(assign_stmt)).all()
    if not assignments:
        return

    existing_stmt = select(EmbyAccount).where(EmbyAccount.user_id == user.id)
    existing_accounts = (await db.execute(existing_stmt)).scalars().all()
    existing_password = next(
        (
            decrypt_emby_password(acc.emby_password)
            for acc in existing_accounts
            if acc.emby_password
        ),
        None,
    )
    resolved_password = password or existing_password or _generate_emby_password()

    account_map = {(acc.emby_server_id, acc.emby_username): acc for acc in existing_accounts}

    account_status = "DISABLED" if user.status == UserStatus.BANNED else "ENABLED"
    for assignment, server in assignments:
        emby_user_id, emby_username = await _ensure_emby_user(
            server, user.username, resolved_password
        )
        key = (server.id, emby_username)
        account = account_map.get(key)
        if account:
            account.emby_user_id = emby_user_id
            account.emby_password = encrypt_emby_password(resolved_password)
            account.status = account_status
            db.add(account)
        else:
            db.add(
                EmbyAccount(
                    user_id=user.id,
                    emby_server_id=server.id,
                    emby_user_id=emby_user_id,
                    emby_username=emby_username,
                    emby_password=encrypt_emby_password(resolved_password),
                    status=account_status,
                )
            )


async def _update_emby_password_for_user(db: AsyncSession, user: User, password: str) -> None:
    if not password:
        return
    if len(password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Emby 密码过短")

    account_stmt = (
        select(EmbyAccount, EmbyServer)
        .join(EmbyServer, EmbyServer.id == EmbyAccount.emby_server_id)
        .where(EmbyAccount.user_id == user.id, EmbyServer.is_active.is_(True))
    )
    accounts = (await db.execute(account_stmt)).all()
    if not accounts:
        sub = await db.scalar(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.end_at.desc())
        )
        if sub and sub.plan_id:
            await _ensure_emby_accounts(db, user, sub.plan_id, password)
            return
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Emby 账号不存在")

    for account, server in accounts:
        if not account.emby_user_id:
            continue
        await _set_emby_password(server, account.emby_user_id, password)
        account.emby_password = encrypt_emby_password(password)
        db.add(account)


async def _sync_emby_status_legacy(db: AsyncSession, user: User, target_status: UserStatus) -> None:
    if target_status not in {UserStatus.ACTIVE, UserStatus.BANNED}:
        return

    desired = "ENABLED" if target_status == UserStatus.ACTIVE else "DISABLED"
    account_stmt = (
        select(EmbyAccount, EmbyServer)
        .join(EmbyServer, EmbyServer.id == EmbyAccount.emby_server_id)
        .where(EmbyAccount.user_id == user.id, EmbyServer.is_active.is_(True))
    )
    accounts = (await db.execute(account_stmt)).all()
    if not accounts:
        return

    for account, server in accounts:
        if not account.emby_user_id:
            continue
        params = {"api_key": server.api_key}
        action = "Enable" if desired == "ENABLED" else "Disable"
        endpoint = urljoin(
            server.base_url.rstrip("/") + "/", f"Users/{account.emby_user_id}/{action}"
        )
        try:
            async with httpx.AsyncClient(timeout=8, verify=False) as client:
                res = await client.post(endpoint, params=params)
                if res.status_code == 404:
                    policy_endpoint = urljoin(
                        server.base_url.rstrip("/") + "/", f"Users/{account.emby_user_id}/Policy"
                    )
                    policy_payload = {"IsDisabled": desired != "ENABLED"}
                    policy_res = await client.post(
                        policy_endpoint, params=params, json=policy_payload
                    )
                    policy_res.raise_for_status()
                else:
                    res.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"同步 Emby 状态失败：{exc}",
            ) from exc
        account.status = desired
        db.add(account)


async def _sync_emby_status(db: AsyncSession, user: User, target_status: UserStatus) -> None:
    await sync_emby_account_status_for_user(db, user, target_status)


async def _fetch_emby_users(server: EmbyServer) -> dict[str, str]:
    endpoint = urljoin(server.base_url.rstrip("/") + "/", "Users")
    params = {"api_key": server.api_key}
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.get(endpoint, params=params)
            res.raise_for_status()
            payload = res.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"获取 Emby 用户失败：{exc}"
        ) from exc

    mapping: dict[str, str] = {}
    for user in payload or []:
        name = user.get("Name") or ""
        policy = user.get("Policy", {}) or {}
        is_disabled = policy.get("IsDisabled", False)
        mapping[name] = "DISABLED" if is_disabled else "ENABLED"
    return mapping


@router.get("")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    keyword: str = Query("", description="用户名/邮箱模糊匹配"),
    role: str | None = Query(None),
    plan_id: str | None = Query(None),
    subscription_status: str | None = Query(None),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    now = datetime.utcnow()
    active_subscription_exists = _current_active_subscription_exists(now)
    any_subscription_exists = _any_subscription_exists()
    latest_subscription_status = _latest_subscription_status_subquery()

    base_stmt = select(User).where(User.deleted_at.is_(None))
    if keyword:
        like = f"%{keyword}%"
        base_stmt = base_stmt.where(or_(User.username.ilike(like), User.email.ilike(like)))
    if role:
        try:
            base_stmt = base_stmt.where(User.role == UserRole(role))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的角色")

    if plan_id:
        subq = select(Subscription.user_id).where(Subscription.plan_id == plan_id)
        base_stmt = base_stmt.where(User.id.in_(subq))

    if subscription_status == "NONE":
        base_stmt = base_stmt.where(~any_subscription_exists)
        subscription_status = None
    elif subscription_status == SubscriptionStatus.ACTIVE.value:
        base_stmt = base_stmt.where(active_subscription_exists)
        subscription_status = None
    elif subscription_status == SubscriptionStatus.CANCELED.value:
        base_stmt = base_stmt.where(
            ~active_subscription_exists,
            any_subscription_exists,
            latest_subscription_status == SubscriptionStatus.CANCELED,
        )
        subscription_status = None
    elif subscription_status == SubscriptionStatus.EXPIRED.value:
        base_stmt = base_stmt.where(
            ~active_subscription_exists,
            any_subscription_exists,
            latest_subscription_status != SubscriptionStatus.CANCELED,
        )
        subscription_status = None
    elif subscription_status is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的订阅状态")

    if subscription_status:
        if subscription_status == "NONE":
            subq = select(Subscription.user_id)
            base_stmt = base_stmt.where(~User.id.in_(subq))
        else:
            try:
                status_enum = SubscriptionStatus(subscription_status)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="无效的订阅状态"
                )
            subq = select(Subscription.user_id).where(Subscription.status == status_enum)
            base_stmt = base_stmt.where(User.id.in_(subq))

    sub_end_stmt = (
        select(Subscription.user_id, func.max(Subscription.end_at).label("end_at"))
        .group_by(Subscription.user_id)
        .subquery()
    )

    if sort_by == "end_at":
        base_stmt = base_stmt.outerjoin(sub_end_stmt, sub_end_stmt.c.user_id == User.id)
        order_col = sub_end_stmt.c.end_at
    else:
        if sort_by != "created_at":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的排序字段")
        order_col = User.created_at

    if sort_order == "asc":
        base_stmt = base_stmt.order_by(order_col.asc().nulls_last())
    else:
        if sort_order != "desc":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的排序方式")
        base_stmt = base_stmt.order_by(order_col.desc().nulls_last())

    count_stmt = select(func.count()).select_from(base_stmt.order_by(None).subquery())
    total = await db.scalar(count_stmt)

    stats_stmt = (
        select(
            func.count().label("total_users"),
            func.count().filter(active_subscription_exists).label("active_users"),
            func.count()
            .filter(
                ~active_subscription_exists,
                any_subscription_exists,
                latest_subscription_status != SubscriptionStatus.CANCELED,
            )
            .label("expired_users"),
            func.count().filter(User.status == UserStatus.BANNED).label("disabled_users"),
        )
        .select_from(User)
        .where(User.deleted_at.is_(None))
    )
    stats_row = (await db.execute(stats_stmt)).one()

    stmt = base_stmt.offset((page - 1) * page_size).limit(page_size)
    users = (await db.execute(stmt)).scalars().all()

    user_ids = [user.id for user in users]

    subscriptions = []
    if user_ids:
        sub_stmt = (
            select(Subscription, Plan)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.user_id.in_(user_ids))
            .order_by(Subscription.user_id, Subscription.end_at.desc())
        )
        subscriptions = (await db.execute(sub_stmt)).all()

    subscription_rows_map: dict = {}
    for sub, plan in subscriptions:
        subscription_rows_map.setdefault(sub.user_id, []).append((sub, plan))

    inviter_map = {}
    inviter_ids = [user.inviter_user_id for user in users if user.inviter_user_id]
    if inviter_ids:
        inviter_stmt = select(User).where(User.id.in_(inviter_ids))
        inviters = (await db.execute(inviter_stmt)).scalars().all()
        inviter_map = {inviter.id: inviter for inviter in inviters}

    emby_password_map: dict = {}
    if user_ids:
        emby_stmt = (
            select(EmbyAccount.user_id, EmbyAccount.emby_password)
            .where(EmbyAccount.user_id.in_(user_ids), EmbyAccount.emby_password.is_not(None))
            .order_by(EmbyAccount.created_at.desc())
        )
        for user_id, emby_password in (await db.execute(emby_stmt)).all():
            if user_id not in emby_password_map:
                emby_password_map[user_id] = decrypt_emby_password(emby_password)

    plan_ids = {
        sub.plan_id
        for rows in subscription_rows_map.values()
        for sub, _ in rows
        if sub and sub.plan_id
    }
    assignments = []
    if plan_ids:
        assign_stmt = (
            select(PlanServerAssignment, EmbyServer)
            .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
            .where(PlanServerAssignment.plan_id.in_(plan_ids))
        )
        assignments = (await db.execute(assign_stmt)).all()

    plan_assignment_map: dict = {}
    for assignment, server in assignments:
        plan_assignment_map.setdefault(assignment.plan_id, []).append(
            {
                "server_id": server.id,
                "name": server.name,
                "base_url": server.base_url,
                "is_online": bool(server.is_active),
                "assigned_at": assignment.created_at,
            }
        )

    binding_rows = []
    if user_ids:
        binding_stmt = select(TelegramUserBinding).where(
            TelegramUserBinding.user_id.in_(user_ids),
            TelegramUserBinding.is_active.is_(True),
        )
        binding_rows = (await db.execute(binding_stmt)).scalars().all()
    binding_map = {binding.user_id: binding for binding in binding_rows}

    items = []
    for user in users:
        user_subscription_rows = subscription_rows_map.get(user.id, [])
        snapshot = _resolve_subscription_snapshot(user_subscription_rows, now)
        active_rows = snapshot["active_rows"]
        subscription_summary_items = snapshot["summary_items"]
        latest_end_at = snapshot["latest_end_at"]
        subscription_status = snapshot["status"]
        subscription = snapshot["subscription"]
        current_subscription = active_rows[0][0] if active_rows else None

        assignments_for_user = []
        if current_subscription:
            assignments_for_user = plan_assignment_map.get(current_subscription.plan_id, [])
        summary = {
            "total": len(assignments_for_user),
            "online": len([a for a in assignments_for_user if a["is_online"]]),
        }

        inviter = inviter_map.get(user.inviter_user_id) if user.inviter_user_id else None
        binding = binding_map.get(user.id)

        items.append(
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "avatar_url": user.avatar_url,
                "role": user.role.value,
                "status": _map_status_to_admin(user.status),
                "balance": f"{float(user.balance or 0):.2f}",
                "expire_remind": bool(user.expire_remind),
                "trial_used": bool(user.trial_used),
                "vod_movie_limit": int(user.vod_movie_limit or 0),
                "vod_tv_limit": int(user.vod_tv_limit or 0),
                "vod_movie_used": int(user.vod_movie_used or 0),
                "vod_tv_used": int(user.vod_tv_used or 0),
                "emby_password": emby_password_map.get(user.id),
                "telegram_binding": (
                    {
                        "telegram_user_id": binding.telegram_user_id,
                        "telegram_username": binding.telegram_username,
                        "telegram_first_name": binding.telegram_first_name,
                        "telegram_last_name": binding.telegram_last_name,
                        "telegram_language_code": binding.telegram_language_code,
                        "is_active": binding.is_active,
                        "bound_at": binding.bound_at,
                        "last_interaction_at": binding.last_interaction_at,
                    }
                    if binding
                    else None
                ),
                "inviter": {"id": inviter.id, "username": inviter.username} if inviter else None,
                "subscription": subscription,
                "subscription_summary": {
                    "status": subscription_status,
                    "active_count": len(active_rows),
                    "group_count": len(
                        {
                            item["group_key"]
                            for item in subscription_summary_items
                            if item.get("group_key")
                        }
                    ),
                    "future_count": len(
                        [item for item in subscription_summary_items if not item["is_current"]]
                    ),
                    "latest_end_at": latest_end_at,
                    "items": subscription_summary_items,
                },
                "server_assignment_summary": summary,
                "server_assignments": assignments_for_user,
                "created_at": user.created_at,
            }
        )

    payload = AdminUsersResponse(
        items=items,
        pagination={"page": page, "page_size": page_size, "total": int(total or 0)},
        stats={
            "total_users": int(stats_row.total_users or 0),
            "active_users": int(stats_row.active_users or 0),
            "expired_users": int(stats_row.expired_users or 0),
            "disabled_users": int(stats_row.disabled_users or 0),
        },
    )

    return _response(payload.model_dump(mode="json"))


@router.post("")
async def create_user(
    payload: AdminUserCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    conditions = [User.username == payload.username]
    if payload.email is not None:
        conditions.append(User.email == payload.email)
    dup_stmt = select(User).where(or_(*conditions), User.deleted_at.is_(None))
    if (await db.execute(dup_stmt)).scalar():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱或用户名已存在")

    try:
        role_value = UserRole(payload.role)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的角色")

    user = User(
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=role_value,
        balance=payload.balance,
        expire_remind=payload.expire_remind,
        vod_movie_limit=payload.vod_movie_limit,
        vod_tv_limit=payload.vod_tv_limit,
        status=UserStatus.ACTIVE,
        email_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return _response({"id": str(user.id)})


@router.put("/{user_id}")
async def update_user(
    user_id: UUID,
    payload: AdminUserUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar()
    if not user or user.deleted_at:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    settings_row: SystemSettings | None = None
    email_verification_enabled = False
    email_changed = False
    email_verification_sent = False
    target_email = payload.email
    if payload.email is not None:
        if payload.email and payload.email != user.email:
            dup_stmt = select(User).where(
                User.email == payload.email,
                User.id != user.id,
                User.deleted_at.is_(None),
            )
            if (await db.execute(dup_stmt)).scalar():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱已存在")
            email_changed = True
        user.email = payload.email
        if email_changed:
            settings_row = await _get_system_settings(db)
            email_verification_enabled = settings_row.email_verification_enabled
            if email_verification_enabled and payload.email:
                user.email_verified = False
                user.email_verified_at = None
                user.email_verification_token = uuid4().hex
                user.email_verification_expires_at = datetime.utcnow() + timedelta(hours=24)
            else:
                user.email_verified = True
                user.email_verified_at = datetime.utcnow() if payload.email else None
                user.email_verification_token = None
                user.email_verification_expires_at = None
    if payload.role:
        try:
            user.role = UserRole(payload.role)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的角色")
    status_changed = False
    if payload.status:
        new_status = _map_status_to_model(payload.status)
        status_changed = new_status != user.status
        user.status = new_status
    if payload.expire_remind is not None:
        user.expire_remind = payload.expire_remind
    if payload.trial_used is not None:
        user.trial_used = payload.trial_used
    set_vod_movie_limit = payload.vod_movie_limit is not None
    set_vod_tv_limit = payload.vod_tv_limit is not None
    password_changed = False
    if set_vod_movie_limit:
        user.vod_movie_limit = payload.vod_movie_limit
    if set_vod_tv_limit:
        user.vod_tv_limit = payload.vod_tv_limit
    if payload.vod_movie_used is not None:
        user.vod_movie_used = payload.vod_movie_used
    if payload.vod_tv_used is not None:
        user.vod_tv_used = payload.vod_tv_used
    if payload.reset_password:
        if not payload.new_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="必须提供新密码")
        user.password_hash = hash_password(payload.new_password)
        password_changed = True
    if payload.balance_set is not None:
        user.balance = payload.balance_set

    activation_notifications: list[tuple[UUID, str, datetime]] = []

    if payload.subscriptions is not None:
        now = datetime.utcnow()
        all_rows_stmt = select(Subscription).where(Subscription.user_id == user.id)
        all_rows = (await db.execute(all_rows_stmt)).scalars().all()
        managed_rows_stmt = select(Subscription).where(
            Subscription.user_id == user.id,
            or_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.end_at >= now),
        )
        managed_rows = (await db.execute(managed_rows_stmt)).scalars().all()

        desired_subscriptions = payload.subscriptions or []
        if not desired_subscriptions:
            for row in all_rows:
                await db.delete(row)
            if not set_vod_movie_limit:
                user.vod_movie_limit = 0
            if not set_vod_tv_limit:
                user.vod_tv_limit = 0
        else:
            plan_ids = {item.plan_id for item in desired_subscriptions}
            plan_rows = (
                (await db.execute(select(Plan).where(Plan.id.in_(plan_ids)))).scalars().all()
            )
            plan_map = {plan.id: plan for plan in plan_rows}
            missing_ids = [str(plan_id) for plan_id in plan_ids if plan_id not in plan_map]
            if missing_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"订阅计划不存在: {', '.join(missing_ids)}",
                )

            seen_groups: set[str] = set()
            normalized_subscriptions: list[tuple[Plan, BillingCycle, datetime, datetime]] = []
            for item in desired_subscriptions:
                plan_row = plan_map[item.plan_id]
                start_at = _parse_dt(item.start_at)
                end_at = _parse_dt(item.end_at)
                if not all([item.billing_cycle, start_at, end_at]):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="必须提供完整的订阅起止时间和周期",
                    )
                if item.billing_cycle == BillingCycle.UNSET.value:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="必须提供付款周期"
                    )
                if end_at <= start_at:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="结束时间必须晚于开始时间"
                    )
                if plan_row.group_key in seen_groups:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="同一服务组只能保留一个当前订阅",
                    )
                seen_groups.add(plan_row.group_key)
                try:
                    billing_value = BillingCycle(item.billing_cycle)
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="无效的付款周期"
                    )
                normalized_subscriptions.append((plan_row, billing_value, start_at, end_at))

            for row in managed_rows:
                await db.delete(row)

            for plan_row, billing_value, start_at, end_at in normalized_subscriptions:
                subscription = Subscription(
                    user_id=user.id,
                    plan_id=plan_row.id,
                    billing_cycle=billing_value,
                    start_at=start_at,
                    end_at=end_at,
                    status=(
                        SubscriptionStatus.ACTIVE if end_at > now else SubscriptionStatus.EXPIRED
                    ),
                    auto_renew=False,
                    source=SubscriptionSource.ADMIN,
                )
                db.add(subscription)
                await db.flush()
                if subscription.status == SubscriptionStatus.ACTIVE:
                    activation_notifications.append((subscription.id, plan_row.name, end_at))

            if not set_vod_movie_limit:
                user.vod_movie_limit = sum(
                    int(plan.vod_movie_times or 0) for plan, *_ in normalized_subscriptions
                )
            if not set_vod_tv_limit:
                user.vod_tv_limit = sum(
                    int(plan.vod_tv_times or 0) for plan, *_ in normalized_subscriptions
                )

            for plan_row, *_ in normalized_subscriptions:
                await _ensure_emby_accounts(db, user, plan_row.id, None)

    elif payload.subscription is not None:
        plan_id = payload.subscription.get("plan_id")
        billing_cycle = payload.subscription.get("billing_cycle")
        start_at = _parse_dt(payload.subscription.get("start_at"))
        end_at = _parse_dt(payload.subscription.get("end_at"))

        if not plan_id:
            await db.execute(Subscription.__table__.delete().where(Subscription.user_id == user.id))
        else:
            plan_exists = await db.scalar(
                select(func.count()).select_from(Plan).where(Plan.id == plan_id)
            )
            if not plan_exists:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="订阅计划不存在"
                )
            if not all([billing_cycle, start_at, end_at]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="必须提供订阅相关字段"
                )
            if billing_cycle == BillingCycle.UNSET.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="必须提供付款周期"
                )
            if end_at <= start_at:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="结束时间必须晚于开始时间"
                )

            sub_stmt = select(Subscription).where(Subscription.user_id == user.id)
            sub = (await db.execute(sub_stmt)).scalar()
            status_value = (
                SubscriptionStatus.ACTIVE
                if end_at > datetime.utcnow()
                else SubscriptionStatus.EXPIRED
            )

            try:
                billing_value = BillingCycle(billing_cycle)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="无效的付款周期"
                )
            plan_row = await db.scalar(select(Plan).where(Plan.id == plan_id))

            if sub:
                sub.plan_id = plan_id
                sub.billing_cycle = billing_value
                sub.start_at = start_at
                sub.end_at = end_at
                sub.status = status_value
            else:
                sub = Subscription(
                    user_id=user.id,
                    plan_id=plan_id,
                    billing_cycle=billing_value,
                    start_at=start_at,
                    end_at=end_at,
                    status=status_value,
                    auto_renew=False,
                )
                db.add(sub)
                await db.flush()
            if status_value == SubscriptionStatus.ACTIVE and plan_row:
                activation_notifications.append((sub.id, plan_row.name, end_at))
            if plan_row:
                if not set_vod_movie_limit:
                    user.vod_movie_limit = int(plan_row.vod_movie_times or 0)
                if not set_vod_tv_limit:
                    user.vod_tv_limit = int(plan_row.vod_tv_times or 0)
            await _ensure_emby_accounts(db, user, plan_id, None)

    if payload.emby_password:
        await _update_emby_password_for_user(db, user, payload.emby_password)
    if status_changed:
        await _sync_emby_status(db, user, user.status)
    if payload.subscriptions is not None or payload.subscription is not None:
        await reconcile_emby_accounts_for_user(db, user)
    for subscription_id, plan_name, end_at in activation_notifications:
        await create_telegram_notification(
            db,
            user_id=user.id,
            notification_type="subscription_activated",
            title="订阅已激活",
            content=f"您的{plan_name}已激活，有效期至{end_at.strftime('%Y-%m-%d')}",
            reference_id=str(subscription_id),
        )

    db.add(user)
    try:
        if email_changed and settings_row and email_verification_enabled and target_email:
            smtp_config = _build_smtp_config(settings_row)
            verify_url = build_site_url(
                settings_row, f"/verify-email?token={user.email_verification_token}"
            )
            rendered = render_email_template(
                settings_row.email_templates,
                EmailTemplateKey.EMAIL_VERIFICATION,
                build_email_template_context(
                    settings_row,
                    username=user.username,
                    verify_url=verify_url,
                ),
            )
            await send_email(
                user.email,
                str(rendered["subject"]),
                str(rendered["html_body"]),
                str(rendered["text_body"]) if rendered["text_body"] is not None else None,
                smtp_config,
            )
            email_verification_sent = True

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        if email_changed and settings_row and email_verification_enabled and target_email:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="新邮箱验证邮件发送失败，修改未生效，请稍后重试",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="更新失败，请稍后重试",
        ) from exc

    if password_changed and user.email:
        settings_row = settings_row or await _get_system_settings(db)
        try:
            smtp_config = _build_smtp_config(settings_row)
            rendered = render_email_template(
                settings_row.email_templates,
                EmailTemplateKey.PASSWORD_CHANGED,
                build_email_template_context(
                    settings_row,
                    username=user.username,
                    changed_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            await send_email(
                user.email,
                str(rendered["subject"]),
                str(rendered["html_body"]),
                str(rendered["text_body"]) if rendered["text_body"] is not None else None,
                smtp_config,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Password change notice failed for user %s", user.id)

    if email_verification_sent:
        return _response({"id": str(user.id)}, "用户已更新，验证邮件已发送到新邮箱")
    return _response({"id": str(user.id)})


@router.post("/import-from-emby")
async def import_from_emby(
    payload: EmbyImportRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    server = await db.scalar(select(EmbyServer).where(EmbyServer.id == payload.server_id))
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="服务器不存在")
    if not server.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="服务器已禁用")

    users_payload = payload.users or []
    if not users_payload:
        result = EmbyImportResult(imported=0, updated=0, skipped=0, errors=[])
        return _response(result.model_dump())

    usernames = [u.username for u in users_payload if u.username]
    emails = [u.email for u in users_payload if u.email]
    existing_users = []
    if usernames or emails:
        stmt = select(User).where(
            or_(User.username.in_(usernames), User.email.in_(emails)),
            User.deleted_at.is_(None),
        )
        existing_users = (await db.execute(stmt)).scalars().all()

    by_username = {user.username: user for user in existing_users}
    by_email = {user.email: user for user in existing_users if user.email}

    imported = 0
    updated = 0
    skipped = 0
    errors: list[str] = []

    for item in users_payload:
        if not item.username:
            skipped += 1
            errors.append("Missing username")
            continue

        account_status = _normalize_account_status(item.account_status)
        mapped_status = _map_emby_user_status(item.account_status, item.user_status)
        user = by_username.get(item.username) or (by_email.get(item.email) if item.email else None)

        if user:
            if user.deleted_at is not None:
                user.deleted_at = None
            user.status = mapped_status
            if not user.email and item.email:
                user.email = item.email
            db.add(user)
            updated += 1
        else:
            user = User(
                email=item.email,
                username=item.username,
                password_hash=hash_password(uuid4().hex),
                role=UserRole.USER,
                balance=Decimal("0.00"),
                expire_remind=True,
                vod_movie_limit=0,
                vod_tv_limit=0,
                status=mapped_status,
                email_verified=True,
                email_verified_at=datetime.utcnow(),
            )
            db.add(user)
            await db.flush()
            imported += 1

        account_stmt = select(EmbyAccount).where(
            EmbyAccount.user_id == user.id, EmbyAccount.emby_server_id == server.id
        )
        account = (await db.execute(account_stmt)).scalar()
        if account:
            account.emby_user_id = item.emby_user_id
            account.emby_username = item.username
            account.status = account_status
            db.add(account)
        else:
            db.add(
                EmbyAccount(
                    user_id=user.id,
                    emby_server_id=server.id,
                    emby_user_id=item.emby_user_id,
                    emby_username=item.username,
                    status=account_status,
                )
            )

    await db.commit()
    result = EmbyImportResult(imported=imported, updated=updated, skipped=skipped, errors=errors)
    return _response(result.model_dump())


@router.post("/{user_id}/balance-adjust")
async def adjust_balance(
    user_id: str,
    payload: BalanceAdjustRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _ensure_admin(current_user, db)

    stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await db.execute(stmt)).scalar()
    if not user or user.deleted_at:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    before = Decimal(user.balance or 0)
    after = before + payload.delta
    if after < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="余额不能为负数")

    user.balance = after
    db.add(
        BalanceTransaction(
            user_id=user.id,
            operator_user_id=actor.id,
            delta=payload.delta,
            before_balance=before,
            after_balance=after,
            reason=payload.reason,
        )
    )
    db.add(user)
    await db.commit()

    return _response({"balance": f"{float(after):.2f}"})


@router.post("/bulk-assign-subscription")
async def bulk_assign_subscription(
    payload: BulkAssignSubscriptionRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    if not payload.user_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="必须提供用户 ID 列表")

    plan = await db.scalar(select(Plan).where(Plan.id == payload.plan_id))
    if not plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="订阅计划不存在")

    try:
        billing_value = BillingCycle(payload.billing_cycle)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的付款周期")

    start_at = _parse_dt(payload.start_at)
    end_at = _parse_dt(payload.end_at)
    if not start_at or not end_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="蹇呴』鎻愪緵寮€濮嬫椂闂村拰缁撴潫鏃堕棿",
        )
    if end_at <= start_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="结束时间必须晚于开始时间"
        )

    users_stmt = select(User).where(User.id.in_(payload.user_ids), User.deleted_at.is_(None))
    users = (await db.execute(users_stmt)).scalars().all()
    user_map = {user.id: user for user in users}

    sub_stmt = select(Subscription).where(Subscription.user_id.in_(payload.user_ids))
    subscriptions = (await db.execute(sub_stmt)).scalars().all()
    sub_map = {sub.user_id: sub for sub in subscriptions}

    conflicts = []
    if payload.mode != "REPLACE":
        for user_id, sub in sub_map.items():
            user = user_map.get(user_id)
            if user and sub:
                conflicts.append({"id": str(user.id), "username": user.username})
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": "HAS_SUBSCRIPTION", "users": conflicts},
            )

    assigned = 0
    skipped = 0
    now = datetime.utcnow()
    status_value = SubscriptionStatus.ACTIVE if end_at > now else SubscriptionStatus.EXPIRED

    for user_id in payload.user_ids:
        user = user_map.get(user_id)
        if not user:
            skipped += 1
            continue

        sub = sub_map.get(user_id)
        if sub:
            if payload.mode != "REPLACE":
                skipped += 1
                continue
            sub.plan_id = plan.id
            sub.billing_cycle = billing_value
            sub.start_at = start_at
            sub.end_at = end_at
            sub.status = status_value
        else:
            sub = Subscription(
                user_id=user.id,
                plan_id=plan.id,
                billing_cycle=billing_value,
                start_at=start_at,
                end_at=end_at,
                status=status_value,
                auto_renew=False,
            )
            db.add(sub)
            await db.flush()

        user.vod_movie_limit = int(plan.vod_movie_times or 0)
        user.vod_tv_limit = int(plan.vod_tv_times or 0)
        db.add(user)
        await _ensure_emby_accounts(db, user, plan.id, payload.emby_password)
        await reconcile_emby_accounts_for_user(db, user)
        if status_value == SubscriptionStatus.ACTIVE and sub is not None:
            await create_telegram_notification(
                db,
                user_id=user.id,
                notification_type="subscription_activated",
                title="订阅已激活",
                content=f"您的{plan.name}已激活，有效期至{end_at.strftime('%Y-%m-%d')}",
                reference_id=str(sub.id),
            )
        assigned += 1

    await db.commit()
    result = BulkAssignSubscriptionResult(assigned=assigned, skipped=skipped, conflicts=conflicts)
    return _response(result.model_dump())


@router.post("/bulk-extend-subscription")
async def bulk_extend_subscription(
    payload: BulkExtendSubscriptionRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    if not payload.user_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="必须提供用户 ID 列表")
    if payload.extend_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="寤堕暱澶╂暟蹇呴』澶т簬 0"
        )

    sub_stmt = select(Subscription).where(Subscription.user_id.in_(payload.user_ids))
    subscriptions = (await db.execute(sub_stmt)).scalars().all()
    sub_map = {sub.user_id: sub for sub in subscriptions}

    extended = 0
    skipped = 0
    now = datetime.utcnow()
    delta = payload.extend_days

    for user_id in payload.user_ids:
        sub = sub_map.get(user_id)
        if not sub or not sub.end_at:
            skipped += 1
            continue
        sub.end_at = sub.end_at + timedelta(days=delta)
        sub.status = SubscriptionStatus.ACTIVE if sub.end_at > now else SubscriptionStatus.EXPIRED
        db.add(sub)
        extended += 1

    await db.commit()
    result = BulkExtendSubscriptionResult(extended=extended, skipped=skipped)
    return _response(result.model_dump())


@router.post("/anomaly-check")
async def anomaly_check(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    users = (await db.execute(select(User).where(User.deleted_at.is_(None)))).scalars().all()
    if not users:
        return _response({"report": []})

    sub_stmt = select(Subscription).where(Subscription.user_id.in_([u.id for u in users]))
    subscriptions = (await db.execute(sub_stmt)).scalars().all()
    sub_map = {sub.user_id: sub for sub in subscriptions}
    plan_ids = {sub.plan_id for sub in subscriptions if sub and sub.plan_id}

    assignments = []
    if plan_ids:
        assign_stmt = (
            select(PlanServerAssignment, EmbyServer)
            .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
            .where(PlanServerAssignment.plan_id.in_(plan_ids))
        )
        assignments = (await db.execute(assign_stmt)).all()

    plan_servers: dict = {}
    server_map: dict = {}
    for assignment, server in assignments:
        plan_servers.setdefault(assignment.plan_id, []).append(server)
        server_map[server.id] = server

    remote_cache: dict = {}
    for server_id, server in server_map.items():
        if not server.is_active:
            continue
        remote_cache[server_id] = await _fetch_emby_users(server)

    report = []
    for user in users:
        sub = sub_map.get(user.id)
        if not sub:
            continue
        servers = plan_servers.get(sub.plan_id, [])
        if not servers:
            continue
        expected = _expected_emby_status(user.status)
        issues = []
        for server in servers:
            if not server.is_active:
                continue
            remote_users = remote_cache.get(server.id)
            if remote_users is None:
                issues.append(
                    {
                        "server_id": str(server.id),
                        "server_name": server.name,
                        "issue": "EMBY_FETCH_FAILED",
                    }
                )
                continue
            remote_status = remote_users.get(user.username)
            if remote_status is None:
                issues.append(
                    {
                        "server_id": str(server.id),
                        "server_name": server.name,
                        "issue": "EMBY_USER_MISSING",
                    }
                )
                continue
            if expected != "UNKNOWN" and remote_status != expected:
                issues.append(
                    {
                        "server_id": str(server.id),
                        "server_name": server.name,
                        "issue": "STATUS_MISMATCH",
                        "expected": expected,
                        "actual": remote_status,
                    }
                )
        if issues:
            if user.status != UserStatus.DELETED and user.status != UserStatus.ABNORMAL:
                user.status = UserStatus.ABNORMAL
                db.add(user)
            report.append(
                {
                    "user_id": str(user.id),
                    "username": user.username,
                    "status": user.status.value,
                    "issues": issues,
                }
            )

    await db.commit()
    return _response({"report": report})


@router.post("/emby-passwords/migrate")
async def migrate_emby_passwords_endpoint(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    try:
        result = await migrate_emby_passwords(db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _response(result, "Emby passwords migrated")


@router.get("/export")
async def export_users(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    users = (await db.execute(select(User).where(User.deleted_at.is_(None)))).scalars().all()
    user_ids = [u.id for u in users]

    sub_stmt = (
        select(Subscription, Plan)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.user_id.in_(user_ids))
        .order_by(Subscription.user_id, Subscription.end_at.desc())
    )
    subs = (await db.execute(sub_stmt)).all() if user_ids else []
    sub_map = {}
    for sub, plan in subs:
        if sub.user_id in sub_map:
            continue
        sub_map[sub.user_id] = (sub, plan)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["用户名", "邮箱", "订阅计划", "付款周期", "开始时间", "结束时间"])
    for user in users:
        sub, plan = sub_map.get(user.id, (None, None))
        plan_name = plan.name if plan else ""
        billing_label = _billing_cycle_label(sub.billing_cycle) if sub else ""
        start_at = sub.start_at.strftime("%Y-%m-%d") if sub and sub.start_at else ""
        end_at = sub.end_at.strftime("%Y-%m-%d") if sub and sub.end_at else ""
        writer.writerow(
            [user.username, user.email or "", plan_name, billing_label, start_at, end_at]
        )

    output.seek(0)
    filename = f"users_export_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/import-template")
async def import_template(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["用户名", "邮箱", "订阅计划", "付款周期", "开始时间", "结束时间"])
    writer.writerow(
        ["testuser1", "test1@example.com", "标准套餐", "月付", "2025-01-15", "2025-02-15"]
    )
    writer.writerow(["testuser2", "", "VIP套餐", "季度", "2025/01/15", "2025/04/15"])
    writer.writerow(["testuser3", "test3@example.com", "永久套餐", "年付", "2025-01-15", ""])
    writer.writerow(["testuser4", "test4@example.com", "", "", "", ""])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=用户导入模板.csv"},
    )


@router.post("/import")
async def import_users(
    file: UploadFile = File(...),
    default_password: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    try:
        validate_account_password(default_password, field_name="默认密码")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="ignore")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or len(rows) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV 文件为空")
    if len(rows) - 1 > 2000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV 行数不能超过 2000")

    header = [c.strip() for c in rows[0]]
    if header[:6] != ["用户名", "邮箱", "订阅计划", "付款周期", "开始时间", "结束时间"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV 表头无效")

    items = rows[1:]
    usernames = [r[0].strip() for r in items if r and len(r) >= 1 and r[0].strip()]
    emails = [r[1].strip() for r in items if len(r) >= 2 and r[1].strip()]
    existing = []
    if usernames or emails:
        existing = (
            (
                await db.execute(
                    select(User).where(
                        or_(User.username.in_(usernames), User.email.in_(emails)),
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    existing_usernames = {u.username for u in existing}
    existing_emails = {u.email for u in existing if u.email}

    plan_names = {r[2].strip() for r in items if len(r) >= 3 and r[2].strip()}
    plans = []
    if plan_names:
        plans = (await db.execute(select(Plan).where(Plan.name.in_(plan_names)))).scalars().all()
    plan_map = {p.name: p for p in plans}

    created = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in enumerate(items, start=2):
        if len(row) < 6:
            skipped += 1
            errors.append(f"第{idx}行列数不足")
            continue
        username = row[0].strip()
        email = row[1].strip() or None
        plan_name = row[2].strip()
        billing_label = row[3].strip()
        start_at_raw = row[4].strip()
        end_at_raw = row[5].strip()

        if not username:
            skipped += 1
            errors.append(f"第{idx}行用户名为空")
            continue
        if username in existing_usernames or (email and email in existing_emails):
            skipped += 1
            continue

        sub_payload = None
        if plan_name:
            plan = plan_map.get(plan_name)
            if not plan:
                skipped += 1
                errors.append(f"第{idx}行订阅计划不存在")
                continue
            if not billing_label or not start_at_raw:
                skipped += 1
                errors.append(f"第{idx}行订阅信息不完整")
                continue
            try:
                billing_cycle = _billing_cycle_from_label(billing_label)
            except ValueError:
                skipped += 1
                errors.append(f"第{idx}行付款周期无效")
                continue
            start_at = _parse_date_only(start_at_raw)
            if not start_at:
                skipped += 1
                errors.append(f"第{idx}行开始时间无效")
                continue
            end_at = (
                _parse_date_only(end_at_raw)
                if end_at_raw
                else _compute_end_at(start_at, billing_cycle)
            )
            sub_payload = {
                "plan": plan,
                "billing_cycle": billing_cycle,
                "start_at": start_at,
                "end_at": end_at,
            }

        user = User(
            email=email,
            username=username,
            password_hash=hash_password(default_password),
            role=UserRole.USER,
            balance=Decimal("0.00"),
            expire_remind=True,
            vod_movie_limit=int(sub_payload["plan"].vod_movie_times or 0) if sub_payload else 0,
            vod_tv_limit=int(sub_payload["plan"].vod_tv_times or 0) if sub_payload else 0,
            status=UserStatus.ACTIVE,
            email_verified=True,
            email_verified_at=datetime.utcnow(),
        )
        db.add(user)
        await db.flush()

        if sub_payload:
            end_at = sub_payload["end_at"]
            status_value = (
                SubscriptionStatus.ACTIVE
                if end_at > datetime.utcnow()
                else SubscriptionStatus.EXPIRED
            )
            db.add(
                Subscription(
                    user_id=user.id,
                    plan_id=sub_payload["plan"].id,
                    billing_cycle=sub_payload["billing_cycle"],
                    start_at=sub_payload["start_at"],
                    end_at=end_at,
                    status=status_value,
                    auto_renew=False,
                )
            )

        created += 1

    await db.commit()
    return _response({"created": created, "skipped": skipped, "errors": errors})


@router.post("/{user_id}/status")
async def update_user_status(
    user_id: str,
    payload: StatusUpdateRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar()
    if not user or user.deleted_at:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    user.status = _map_status_to_model(payload.status)
    await _sync_emby_status(db, user, user.status)
    db.add(user)
    await db.commit()

    return _response({"status": _map_status_to_admin(user.status)})


@router.delete("/{user_id}/telegram-binding")
async def delete_user_telegram_binding(
    user_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar()
    if not user or user.deleted_at:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    binding = await db.scalar(
        select(TelegramUserBinding).where(
            TelegramUserBinding.user_id == user.id,
            TelegramUserBinding.is_active.is_(True),
        )
    )
    if not binding:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram 绑定不存在")

    binding.is_active = False
    binding.unbound_at = datetime.utcnow()
    binding.last_interaction_at = binding.unbound_at
    db.add(binding)
    await db.execute(
        delete(TelegramNotificationPreference).where(
            TelegramNotificationPreference.user_id == user.id
        )
    )
    await db.commit()
    return _response({"id": str(user.id), "message": "telegram_unbound"})


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)

    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar()
    if not user or user.deleted_at:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    user.status = UserStatus.DELETED
    user.deleted_at = datetime.utcnow()
    db.add(user)
    await db.commit()

    return _response({"id": str(user.id)})
