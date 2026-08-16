from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.emby import EmbyAccount, EmbyServer
from app.models.subscription import PlanServerAssignment, Subscription, SubscriptionStatus
from app.models.user import User, UserStatus
from app.services.emby_passwords import decrypt_emby_password, encrypt_emby_password
from app.services.telegram import create_telegram_notification


def generate_emby_password() -> str:
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
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Emby fetch users failed: {exc}"
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
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Emby create user failed: {exc}"
            ) from exc
        emby_user_id = payload.get("Id") or payload.get("User", {}).get("Id")
        if not emby_user_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Emby create user response invalid"
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
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Emby set password failed: {exc}"
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
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Emby set password failed: {exc}"
        ) from exc


async def _set_emby_user_status(server: EmbyServer, emby_user_id: str, desired_status: str) -> None:
    if not emby_user_id:
        return

    params = {"api_key": server.api_key}
    action = "Enable" if desired_status == "ENABLED" else "Disable"
    endpoint = urljoin(server.base_url.rstrip("/") + "/", f"Users/{emby_user_id}/{action}")
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.post(endpoint, params=params)
            if res.status_code == status.HTTP_404_NOT_FOUND:
                policy_endpoint = urljoin(
                    server.base_url.rstrip("/") + "/", f"Users/{emby_user_id}/Policy"
                )
                policy_payload = {"IsDisabled": desired_status != "ENABLED"}
                policy_res = await client.post(policy_endpoint, params=params, json=policy_payload)
                policy_res.raise_for_status()
            else:
                res.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Emby sync status failed: {exc}",
        ) from exc


async def _delete_emby_user(server: EmbyServer, emby_user_id: str) -> None:
    if not emby_user_id:
        return

    endpoint = urljoin(server.base_url.rstrip("/") + "/", f"Users/{emby_user_id}")
    params = {"api_key": server.api_key}
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as client:
            res = await client.delete(endpoint, params=params)
            if res.status_code == status.HTTP_404_NOT_FOUND:
                return
            res.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Emby delete user failed: {exc}"
        ) from exc


async def ensure_emby_accounts_for_user(
    db: AsyncSession,
    user: User,
    plan_id,
    password: str | None,
) -> str | None:
    if not plan_id:
        return None
    assign_stmt = (
        select(PlanServerAssignment, EmbyServer)
        .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
        .where(PlanServerAssignment.plan_id == plan_id, EmbyServer.is_active.is_(True))
    )
    assignments = (await db.execute(assign_stmt)).all()
    if not assignments:
        return None

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
    resolved_password = password or existing_password or generate_emby_password()

    account_map = {(acc.emby_server_id, acc.emby_username): acc for acc in existing_accounts}

    account_status = "DISABLED" if user.status == UserStatus.BANNED else "ENABLED"
    for assignment, server in assignments:
        emby_user_id, emby_username = await _ensure_emby_user(
            server, user.username, resolved_password
        )
        await _set_emby_user_status(server, emby_user_id, account_status)
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

    return resolved_password


async def sync_emby_account_status_for_user(
    db: AsyncSession, user: User, target_status: UserStatus | str
) -> None:
    desired = target_status
    if isinstance(target_status, UserStatus):
        if target_status == UserStatus.ACTIVE:
            desired = "ENABLED"
        elif target_status == UserStatus.BANNED:
            desired = "DISABLED"
        else:
            return

    if desired not in {"ENABLED", "DISABLED"}:
        return

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
        await _set_emby_user_status(server, account.emby_user_id, desired)
        account.status = desired
        db.add(account)


async def disable_orphaned_emby_accounts_for_user(db: AsyncSession, user: User) -> int:
    now = datetime.utcnow()
    entitled_stmt = (
        select(PlanServerAssignment.server_id)
        .join(Subscription, Subscription.plan_id == PlanServerAssignment.plan_id)
        .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
        .where(
            Subscription.user_id == user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > now,
            EmbyServer.is_active.is_(True),
        )
        .distinct()
    )
    entitled_server_ids = set((await db.execute(entitled_stmt)).scalars().all())

    account_stmt = (
        select(EmbyAccount, EmbyServer)
        .join(EmbyServer, EmbyServer.id == EmbyAccount.emby_server_id)
        .where(EmbyAccount.user_id == user.id)
    )
    accounts = (await db.execute(account_stmt)).all()
    disabled = 0
    for account, server in accounts:
        if account.emby_server_id in entitled_server_ids:
            continue
        if server.is_active and account.emby_user_id and account.status != "DISABLED":
            await _set_emby_user_status(server, account.emby_user_id, "DISABLED")
        account.status = "DISABLED"
        db.add(account)
        disabled += 1
    return disabled


async def reconcile_emby_accounts_for_user(db: AsyncSession, user: User) -> None:
    now = datetime.utcnow()
    entitled_stmt = (
        select(PlanServerAssignment.server_id)
        .join(Subscription, Subscription.plan_id == PlanServerAssignment.plan_id)
        .join(EmbyServer, EmbyServer.id == PlanServerAssignment.server_id)
        .where(
            Subscription.user_id == user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.end_at > now,
            EmbyServer.is_active.is_(True),
        )
        .distinct()
    )
    entitled_server_ids = set((await db.execute(entitled_stmt)).scalars().all())

    account_stmt = (
        select(EmbyAccount, EmbyServer)
        .join(EmbyServer, EmbyServer.id == EmbyAccount.emby_server_id)
        .where(EmbyAccount.user_id == user.id)
    )
    accounts = (await db.execute(account_stmt)).all()

    for account, server in accounts:
        if account.emby_server_id in entitled_server_ids:
            continue
        if server.is_active and account.emby_user_id:
            await _delete_emby_user(server, account.emby_user_id)
        await db.delete(account)


async def update_emby_password_for_user(db: AsyncSession, user: User, password: str) -> None:
    if not password:
        return
    if len(password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Emby password too short"
        )

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
        if (
            sub
            and sub.plan_id
            and sub.status == SubscriptionStatus.ACTIVE
            and sub.end_at > datetime.utcnow()
        ):
            await ensure_emby_accounts_for_user(db, user, sub.plan_id, password)
            await db.commit()
            await create_telegram_notification(
                db,
                user_id=user.id,
                notification_type="emby_password_reset",
                title="Emby 密码已重置",
                content="您的 Emby 密码已重置",
                reference_id=str(user.id),
                commit=True,
            )
            return
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Emby account not found"
        )

    for account, server in accounts:
        if not account.emby_user_id:
            continue
        await _set_emby_password(server, account.emby_user_id, password)
        account.emby_password = encrypt_emby_password(password)
        db.add(account)

    await create_telegram_notification(
        db,
        user_id=user.id,
        notification_type="emby_password_reset",
        title="Emby 密码已重置",
        content="您的 Emby 密码已重置",
        reference_id=str(user.id),
    )
