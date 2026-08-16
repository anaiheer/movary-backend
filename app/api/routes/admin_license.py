from __future__ import annotations

import csv
import io
import json
import zipfile
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.license_status import build_license_overview
from app.core.pro_extensions import get_backend_extension_state
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.balance import BalanceTransaction
from app.models.emby import EmbyAccount, EmbyServer
from app.models.invitation import Invitation
from app.models.moviepilot import MoviePilotServer
from app.models.order import Order, OrderValueLink, PaymentTransaction
from app.models.social_account import SocialAccountBinding
from app.models.subscription import (
    Plan,
    PlanBillingCycle,
    PlanServerAssignment,
    Subscription,
    SubscriptionGroup,
)
from app.models.telegram import (
    TelegramNotification,
    TelegramNotificationPreference,
    TelegramUserBinding,
)
from app.models.user import User, UserRole
from app.schemas.admin import AdminLicenseProviderContract, AdminLicenseStatus
from app.services.license_contract import get_license_provider_contract
from app.services.license_provider import (
    activate_remote_license,
    deactivate_remote_license,
    get_license_provider_health,
    get_license_provider_status,
    refresh_remote_license,
)
from app.services.license_runtime import (
    cache_remote_license,
    clear_cached_license,
    evaluate_cached_license,
    get_or_create_instance_identity,
    load_license_state,
    update_cached_license_refresh,
)
from app.services.license_tokens import verify_signed_license
from app.services.pro_artifacts import get_public_artifact_state, sync_pro_artifacts_from_license

router = APIRouter(prefix="/admin/license", tags=["admin-license"])


class LicenseActivateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=255)


class LicenseRollbackRequest(BaseModel):
    confirm: bool = Field(default=False)


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


def _ensure_provider_artifact_payload(provider_payload: dict) -> None:
    backend_url = str(provider_payload.get("backend_artifact_url") or "").strip()
    frontend_url = str(provider_payload.get("frontend_artifact_url") or "").strip()
    artifact_version = str(provider_payload.get("artifact_version") or "").strip()
    if backend_url and frontend_url and artifact_version:
        return
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="管理员授权服务异常：未返回有效的 Artifact 分发配置",
    )


def _ensure_synced_artifacts_ready(artifacts_state: dict) -> None:
    backend_ready = str((artifacts_state.get("backend") or {}).get("status") or "") == "ready"
    frontend_ready = str((artifacts_state.get("frontend") or {}).get("status") or "") == "ready"
    if backend_ready and frontend_ready:
        return
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="管理员授权服务异常：Artifact 分发配置不可用或未生效",
    )


def _row_to_dict(row) -> dict:  # type: ignore[no-untyped-def]
    return {
        str(column.name): (
            None if getattr(row, column.name) is None else str(getattr(row, column.name))
        )
        for column in row.__table__.columns
    }


async def _build_export_zip(db: AsyncSession) -> bytes:
    datasets = {
        "users": [
            _row_to_dict(row)
            for row in (await db.execute(select(User).order_by(User.created_at.asc())))
            .scalars()
            .all()
        ],
        "subscriptions": [
            _row_to_dict(row)
            for row in (
                await db.execute(select(Subscription).order_by(Subscription.created_at.asc()))
            )
            .scalars()
            .all()
        ],
        "plans": [
            _row_to_dict(row)
            for row in (await db.execute(select(Plan).order_by(Plan.created_at.asc())))
            .scalars()
            .all()
        ],
        "subscription_groups": [
            _row_to_dict(row)
            for row in (
                await db.execute(
                    select(SubscriptionGroup).order_by(SubscriptionGroup.created_at.asc())
                )
            )
            .scalars()
            .all()
        ],
        "emby_servers": [
            _row_to_dict(row)
            for row in (await db.execute(select(EmbyServer).order_by(EmbyServer.created_at.asc())))
            .scalars()
            .all()
        ],
        "moviepilot_servers": [
            _row_to_dict(row)
            for row in (
                await db.execute(
                    select(MoviePilotServer).order_by(MoviePilotServer.created_at.asc())
                )
            )
            .scalars()
            .all()
        ],
        "orders": [
            _row_to_dict(row)
            for row in (await db.execute(select(Order).order_by(Order.created_at.asc())))
            .scalars()
            .all()
        ],
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "summary.json",
            json.dumps(
                {
                    "generated_at": str(uuid4()),
                    "tables": {key: len(value) for key, value in datasets.items()},
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        for name, rows in datasets.items():
            zf.writestr(f"{name}.json", json.dumps(rows, indent=2, ensure_ascii=False))
            csv_buffer = io.StringIO()
            if rows:
                writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            zf.writestr(f"{name}.csv", csv_buffer.getvalue())
    return buffer.getvalue()


async def _rollback_to_free(db: AsyncSession) -> dict[str, int]:
    admin_ids = [
        user.id
        for user in (
            await db.execute(
                select(User).where(User.role.in_([UserRole.ADMIN, UserRole.SUPERADMIN]))
            )
        )
        .scalars()
        .all()
    ]

    deleted = {
        "order_value_links": int((await db.execute(delete(OrderValueLink))).rowcount or 0),
        "payment_transactions": int((await db.execute(delete(PaymentTransaction))).rowcount or 0),
        "orders": int((await db.execute(delete(Order))).rowcount or 0),
        "plan_server_assignments": int(
            (await db.execute(delete(PlanServerAssignment))).rowcount or 0
        ),
        "plan_billing_cycles": int((await db.execute(delete(PlanBillingCycle))).rowcount or 0),
        "subscriptions": int((await db.execute(delete(Subscription))).rowcount or 0),
        "subscription_groups": int((await db.execute(delete(SubscriptionGroup))).rowcount or 0),
        "plans": int((await db.execute(delete(Plan))).rowcount or 0),
        "emby_accounts": int((await db.execute(delete(EmbyAccount))).rowcount or 0),
        "emby_servers": int((await db.execute(delete(EmbyServer))).rowcount or 0),
        "moviepilot_servers": int((await db.execute(delete(MoviePilotServer))).rowcount or 0),
        "balance_transactions": int((await db.execute(delete(BalanceTransaction))).rowcount or 0),
        "invitations": int((await db.execute(delete(Invitation))).rowcount or 0),
        "social_account_bindings": int(
            (await db.execute(delete(SocialAccountBinding))).rowcount or 0
        ),
        "telegram_notifications": int(
            (await db.execute(delete(TelegramNotification))).rowcount or 0
        ),
        "telegram_notification_preferences": int(
            (await db.execute(delete(TelegramNotificationPreference))).rowcount or 0
        ),
        "telegram_user_bindings": int(
            (await db.execute(delete(TelegramUserBinding))).rowcount or 0
        ),
    }

    if admin_ids:
        deleted["users"] = int(
            (await db.execute(delete(User).where(~User.id.in_(admin_ids)))).rowcount or 0
        )
    else:
        deleted["users"] = int((await db.execute(delete(User))).rowcount or 0)

    await db.commit()

    clear_cached_license()
    from app.core.pro_extensions import reset_backend_extensions
    from app.services.pro_artifacts import clear_active_pro_artifacts

    clear_active_pro_artifacts()
    reset_backend_extensions()
    return deleted


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


@router.get("/status")
async def get_admin_license_status(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    extension_state = get_backend_extension_state()
    runtime_state = evaluate_cached_license(load_license_state())
    provider_state = get_license_provider_status()
    provider_reachable, provider_health_message = await get_license_provider_health()
    identity = get_or_create_instance_identity()
    artifacts = get_public_artifact_state()
    overview = build_license_overview(extension_state, runtime_state)
    payload = AdminLicenseStatus(
        edition=overview["edition"],
        status=overview["status"],
        message=overview["message"],
        manage_url=overview["manage_url"],
        activation_mode="online_signed",
        provider_mode=provider_state["mode"],
        provider_ready=bool(provider_state["ready"]),
        provider_reachable=provider_reachable,
        provider_health_message=provider_health_message,
        provider_server_url=provider_state["server_url"],
        provider_key_id=provider_state["key_id"],
        provider_missing_fields=provider_state["missing_fields"],
        activation_present=bool(runtime_state["activation_present"]),
        activation_code_hint=runtime_state["activation_code_hint"],
        activated_at=runtime_state["activated_at"],
        last_refresh_at=runtime_state["last_refresh_at"],
        expires_at=runtime_state.get("expires_at"),
        instance_id=identity["instance_id"],
        instance_label=identity["instance_label"],
        license_id=runtime_state.get("license_id"),
        package_code=runtime_state.get("package_code"),
        package_name=runtime_state.get("package_name"),
        pro_effective=bool(extension_state.get("pro_effective")),
        artifact_version=artifacts.get("backend", {}).get("version")
        or artifacts.get("frontend", {}).get("version"),
        backend_artifact_status=artifacts.get("backend", {}).get("status"),
        backend_artifact_error=artifacts.get("backend", {}).get("error"),
        frontend_artifact_status=artifacts.get("frontend", {}).get("status"),
        frontend_artifact_error=artifacts.get("frontend", {}).get("error"),
        frontend_artifact_entry_url=artifacts.get("frontend", {}).get("local_entry_url"),
        frontend_artifact_style_url=artifacts.get("frontend", {}).get("local_style_url"),
        extension_enabled="pro" in extension_state["enabled"],
        extension_loaded=any(item.get("name") == "pro" for item in extension_state["loaded"]),
        extension_failed=any(item.get("name") == "pro" for item in extension_state["failed"]),
        loaded_extensions=extension_state["loaded"],
        failed_extensions=extension_state["failed"],
    )
    return _response(payload.model_dump())


@router.get("/provider-contract")
async def get_admin_license_provider_contract(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    payload = AdminLicenseProviderContract(**get_license_provider_contract())
    return _response(payload.model_dump())


@router.post("/activate")
async def activate_admin_license(
    payload: LicenseActivateRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    identity = get_or_create_instance_identity()
    try:
        provider_payload = await activate_remote_license(
            code=payload.code.strip(),
            instance_id=identity["instance_id"],
            edition="pro",
            instance_label=identity["instance_label"],
        )
        _ensure_provider_artifact_payload(provider_payload)
        license_token = str(provider_payload.get("license") or "").strip()
        if not license_token:
            raise ValueError("授权服务未返回许可证")
        claims = verify_signed_license(license_token, expected_instance_id=identity["instance_id"])
        artifacts_state = await sync_pro_artifacts_from_license(provider_payload)
        _ensure_synced_artifacts_ready(artifacts_state)
        state = cache_remote_license(
            code_hint=f"***{payload.code.strip()[-4:]}"
            if len(payload.code.strip()) >= 4
            else "***",
            license_token=license_token,
            expires_at=str(provider_payload.get("expires_at") or claims.get("expires_at") or ""),
            key_id=str(provider_payload.get("key_id") or ""),
            package_code=str(claims.get("package_code") or ""),
            package_name=str(
                claims.get("package_name") or provider_payload.get("package_name") or ""
            ),
            license_id=str(claims.get("license_id") or ""),
        )
        from app.main import app as fastapi_app
        from app.core.pro_extensions import include_extension_routes, reset_backend_extensions

        reset_backend_extensions()
        include_extension_routes(fastapi_app)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _response(state, "专业版授权已激活")


@router.post("/refresh")
async def refresh_admin_license(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    runtime_state = evaluate_cached_license(load_license_state())
    if not runtime_state.get("activation_present") or not runtime_state.get("license"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="当前没有可刷新的授权状态"
        )
    identity = get_or_create_instance_identity()
    try:
        provider_payload = await refresh_remote_license(
            license_token=str(runtime_state["license"]),
            instance_id=identity["instance_id"],
        )
        _ensure_provider_artifact_payload(provider_payload)
        license_token = str(provider_payload.get("license") or "").strip()
        if not license_token:
            raise ValueError("授权服务未返回许可证")
        claims = verify_signed_license(license_token, expected_instance_id=identity["instance_id"])
        artifacts_state = await sync_pro_artifacts_from_license(provider_payload)
        _ensure_synced_artifacts_ready(artifacts_state)
        state = update_cached_license_refresh(
            current_state=runtime_state,
            license_token=license_token,
            expires_at=str(provider_payload.get("expires_at") or claims.get("expires_at") or ""),
            key_id=str(provider_payload.get("key_id") or ""),
            package_code=str(claims.get("package_code") or ""),
            package_name=str(
                claims.get("package_name") or provider_payload.get("package_name") or ""
            ),
            license_id=str(claims.get("license_id") or ""),
        )
        from app.main import app as fastapi_app
        from app.core.pro_extensions import include_extension_routes, reset_backend_extensions

        reset_backend_extensions()
        include_extension_routes(fastapi_app)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _response(state, "专业版授权刷新成功")


@router.post("/deactivate")
async def deactivate_admin_license(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    runtime_state = load_license_state()
    identity = get_or_create_instance_identity()
    try:
        if runtime_state.get("license"):
            await deactivate_remote_license(
                license_token=str(runtime_state["license"]),
                instance_id=identity["instance_id"],
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    state = clear_cached_license()
    from app.services.pro_artifacts import clear_active_pro_artifacts
    from app.core.pro_extensions import reset_backend_extensions

    clear_active_pro_artifacts()
    reset_backend_extensions()
    return _response(state, "专业版授权已清除")


@router.get("/export")
async def export_license_downgrade_data(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    payload = await _build_export_zip(db)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="movary-pro-export.zip"'},
    )


@router.post("/rollback")
async def rollback_to_free(
    payload: LicenseRollbackRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请明确确认回退到基础版"
        )
    deleted = await _rollback_to_free(db)
    return _response({"deleted": deleted}, "已回退到基础版并清理专业版数据")
