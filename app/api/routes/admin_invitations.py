import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.public_urls import build_site_url
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.invitation import Invitation, InvitationStatus
from app.models.subscription import Plan
from app.models.system_settings import SystemSettings
from app.models.user import User, UserRole
from app.schemas.admin import (
    InvitationCreate,
    InvitationListItem,
    InvitationListResponse,
    InvitationResponse,
)
from app.services.email import SmtpConfig, send_email
from app.services.email_templates import (
    EmailTemplateKey,
    build_email_template_context,
    render_email_template,
)

router = APIRouter(prefix="/admin/invitations", tags=["admin-invitations"])
logger = logging.getLogger(__name__)

INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_CODE_LENGTH = 8


class InvitationDeleteRequest(BaseModel):
    ids: list[UUID]


def _is_admin(user: User) -> bool:
    return user.role in {UserRole.ADMIN, UserRole.SUPERADMIN}


async def _get_actor(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户不存在或无权访问")
    return user


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _get_system_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _build_optional_smtp_config(row: SystemSettings) -> SmtpConfig | None:
    if not row.smtp_host or not row.smtp_port or not row.smtp_from:
        return None
    if row.smtp_use_tls and row.smtp_use_ssl:
        logger.warning("Skip invitation email because SMTP TLS and SSL are both enabled")
        return None

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


async def _generate_invite_code(db: AsyncSession) -> str:
    for _ in range(10):
        code = "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
        exists_stmt = select(Invitation.id).where(Invitation.token == code)
        if not (await db.execute(exists_stmt)).scalar():
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="邀请码生成失败，请稍后重试",
    )


async def _normalize_invitation_token(invitation: Invitation, db: AsyncSession) -> None:
    if len(invitation.token or "") == INVITE_CODE_LENGTH:
        return
    invitation.token = await _generate_invite_code(db)
    db.add(invitation)


def _build_invite_url(inviter_username: str, invite_code: str) -> str:
    return f"/register?{urlencode({'invite': inviter_username, 'token': invite_code})}"


def _serialize_invitation_list_item(
    invitation: Invitation, inviter: User, plan: Plan | None
) -> InvitationListItem:
    return InvitationListItem(
        id=invitation.id,
        invitee_email=invitation.invitee_email,
        token=invitation.token,
        invite_url=_build_invite_url(inviter.username, invitation.token),
        inviter={
            "id": str(inviter.id),
            "username": inviter.username,
            "email": inviter.email,
        },
        plan={"id": str(plan.id), "name": plan.name} if plan else None,
        initial_balance=invitation.initial_balance,
        status=invitation.status.value,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
    )


async def _delete_invitations_by_ids(
    invitation_ids: list[UUID], actor: User, db: AsyncSession
) -> dict:
    requested_ids: list[UUID] = []
    seen_ids: set[UUID] = set()
    for invitation_id in invitation_ids:
        if invitation_id in seen_ids:
            continue
        seen_ids.add(invitation_id)
        requested_ids.append(invitation_id)

    if not requested_ids:
        return {
            "requested": 0,
            "deleted": 0,
            "missing": 0,
            "missing_ids": [],
            "failed_ids": [],
        }

    stmt = select(Invitation).where(Invitation.id.in_(requested_ids))
    if not _is_admin(actor):
        stmt = stmt.where(Invitation.inviter_user_id == actor.id)

    invitations = (await db.execute(stmt)).scalars().all()
    invitation_map = {invitation.id: invitation for invitation in invitations}
    missing_ids = [
        str(invitation_id) for invitation_id in requested_ids if invitation_id not in invitation_map
    ]
    failed_ids: list[str] = []
    deleted_count = 0

    for invitation_id in requested_ids:
        invitation = invitation_map.get(invitation_id)
        if not invitation:
            continue
        if invitation.status == InvitationStatus.ACCEPTED:
            failed_ids.append(str(invitation_id))
            continue
        try:
            async with db.begin_nested():
                await db.execute(sa_delete(Invitation).where(Invitation.id == invitation_id))
                await db.flush()
            deleted_count += 1
        except Exception:
            failed_ids.append(str(invitation_id))

    await db.commit()
    return {
        "requested": len(requested_ids),
        "deleted": deleted_count,
        "missing": len(missing_ids),
        "missing_ids": missing_ids,
        "failed_ids": failed_ids,
    }


@router.post("")
async def invite_user(
    payload: InvitationCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _get_actor(current_user, db)
    plan_id = payload.plan_id if _is_admin(actor) else None
    initial_balance = payload.initial_balance if _is_admin(actor) else None

    token = await _generate_invite_code(db)
    expires_at = datetime.utcnow() + timedelta(hours=12)

    invitation = Invitation(
        token=token,
        invitee_email=payload.invitee_email,
        inviter_user_id=actor.id,
        plan_id=plan_id,
        initial_balance=initial_balance,
        expires_at=expires_at,
        status=InvitationStatus.PENDING,
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)

    invite_url = _build_invite_url(actor.username, token)
    message = "邀请码已创建，可复制邀请链接手动发送"
    settings_row = await _get_system_settings(db)
    smtp_config = _build_optional_smtp_config(settings_row)
    if smtp_config:
        rendered = render_email_template(
            settings_row.email_templates,
            EmailTemplateKey.INVITATION,
            build_email_template_context(
                settings_row,
                inviter_username=actor.username,
                invitee_email=invitation.invitee_email,
                invite_url=build_site_url(settings_row, invite_url),
                invite_code=token,
                expires_at=invitation.expires_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            ),
        )
        try:
            await send_email(
                invitation.invitee_email,
                str(rendered["subject"]),
                str(rendered["html_body"]),
                str(rendered["text_body"]) if rendered["text_body"] is not None else None,
                smtp_config,
            )
            message = "邀请码已创建，邀请邮件已发送"
        except Exception:  # noqa: BLE001
            logger.exception("Invitation email failed for %s", invitation.id)
            message = "邀请码已创建，但邀请邮件发送失败，请检查 SMTP 配置"

    response = InvitationResponse(
        id=invitation.id,
        invitee_email=invitation.invitee_email,
        token=invitation.token,
        expires_at=invitation.expires_at,
        invite_url=invite_url,
    )
    return _response(response.model_dump(mode="json"), message)


@router.get("")
async def list_invitations(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _get_actor(current_user, db)

    stmt = (
        select(Invitation, User, Plan)
        .join(User, User.id == Invitation.inviter_user_id)
        .outerjoin(Plan, Plan.id == Invitation.plan_id)
        .order_by(Invitation.created_at.desc())
    )
    total_stmt = select(func.count()).select_from(Invitation)

    if not _is_admin(actor):
        stmt = stmt.where(Invitation.inviter_user_id == actor.id)
        total_stmt = total_stmt.where(Invitation.inviter_user_id == actor.id)

    accepted_stmt = (
        select(func.count())
        .select_from(Invitation)
        .where(Invitation.status == InvitationStatus.ACCEPTED)
    )
    if not _is_admin(actor):
        accepted_stmt = accepted_stmt.where(Invitation.inviter_user_id == actor.id)

    total = await db.scalar(total_stmt)
    accepted_count = await db.scalar(accepted_stmt)
    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    normalized = False
    for invitation, _inviter, _plan in rows:
        before = invitation.token
        await _normalize_invitation_token(invitation, db)
        normalized = normalized or invitation.token != before
    if normalized:
        await db.commit()

    payload = InvitationListResponse(
        items=[
            _serialize_invitation_list_item(invitation, inviter, plan)
            for invitation, inviter, plan in rows
        ],
        page=page,
        page_size=page_size,
        total=int(total or 0),
        accepted_count=int(accepted_count or 0),
    )
    return _response(payload.model_dump(mode="json"))


@router.post("/batch-delete")
async def batch_delete_invitations(
    payload: InvitationDeleteRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _get_actor(current_user, db)
    result = await _delete_invitations_by_ids(payload.ids, actor, db)
    return _response(result)


@router.post("/{invitation_id}/cancel")
async def cancel_invitation(
    invitation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = await _get_actor(current_user, db)

    stmt = (
        select(Invitation, User, Plan)
        .join(User, User.id == Invitation.inviter_user_id)
        .outerjoin(Plan, Plan.id == Invitation.plan_id)
    )
    stmt = stmt.where(Invitation.id == invitation_id)
    if not _is_admin(actor):
        stmt = stmt.where(Invitation.inviter_user_id == actor.id)

    row = (await db.execute(stmt)).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邀请码不存在")

    invitation, inviter, plan = row
    if invitation.status == InvitationStatus.CANCELED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请码已取消")
    if invitation.status == InvitationStatus.ACCEPTED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请码已被使用")
    if invitation.expires_at < datetime.utcnow():
        invitation.status = InvitationStatus.EXPIRED
        db.add(invitation)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请码已过期")

    invitation.status = InvitationStatus.CANCELED
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)

    item = _serialize_invitation_list_item(invitation, inviter, plan)
    return _response(item.model_dump(mode="json"), message="邀请码已取消")
