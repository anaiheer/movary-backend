import logging
from fastapi import APIRouter, Depends, HTTPException, status, Body, UploadFile, File, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from app.db.session import get_db
from app.models.user import User, UserStatus
from app.models.invitation import Invitation, InvitationStatus
from app.models.subscription import (
    Plan,
    Subscription,
    SubscriptionSource,
    SubscriptionStatus,
    BillingCycle,
)
from app.models.balance import BalanceTransaction
from app.models.system_settings import SystemSettings
from app.schemas.user import (
    UserCreate,
    UserLogin,
    UserResponse,
    TokenResponse,
    RefreshTokenRequest,
    UserSelfUpdate,
    EmailVerificationRequest,
)
from app.api.routes.users import _ensure_admin_user
from app.core.security import (
    ensure_token_user_is_active,
    hash_password,
    verify_password,
    create_token,
    verify_token,
    get_current_user,
)
from app.core.config import settings
from app.core.public_urls import build_site_url
from app.services.email import send_email, SmtpConfig
from app.services.email_templates import (
    EmailTemplateKey,
    build_email_template_context,
    render_email_template,
)
from app.services.telegram import create_telegram_notification

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


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


def _ensure_login_user_allowed(user: User) -> None:
    if user.deleted_at is not None or user.status == UserStatus.DELETED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    if user.status == UserStatus.BANNED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )


@router.post("/register", response_model=UserResponse)
async def register(user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    """用户注册"""
    settings_row = await _get_system_settings(db)
    email_verification_enabled = settings_row.email_verification_enabled
    if email_verification_enabled and not user_data.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="必须填写邮箱")
    if settings_row.invite_registration_enabled and not user_data.invite_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="必须填写邀请码")

    invitation: Invitation | None = None
    if user_data.invite_token:
        inv_stmt = select(Invitation).where(Invitation.token == user_data.invite_token)
        invitation = (await db.execute(inv_stmt)).scalar()
        if not invitation:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的邀请令牌")
        if invitation.status != InvitationStatus.PENDING:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请已被使用")
        if invitation.expires_at < datetime.utcnow():
            invitation.status = InvitationStatus.EXPIRED
            db.add(invitation)
            await db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请已过期")
        if user_data.email and invitation.invitee_email != user_data.email:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请邮箱不匹配")

    stmt = select(User).where(
        (User.email == user_data.email) | (User.username == user_data.username)
    )
    result = await db.execute(stmt)
    existing_users = result.scalars().all()

    for u in existing_users:
        if not u.deleted_at:
            if u.email == user_data.email:
                raise HTTPException(status_code=400, detail="该邮箱已被注册")
            if u.username == user_data.username:
                raise HTTPException(status_code=400, detail="该用户名已被注册")

    user = User(
        email=user_data.email,
        username=user_data.username,
        phone=user_data.phone,
        password_hash=hash_password(user_data.password),
        email_verified=not email_verification_enabled,
    )
    if invitation:
        user.inviter_user_id = invitation.inviter_user_id
    if email_verification_enabled:
        user.email_verification_token = uuid4().hex
        user.email_verification_expires_at = datetime.utcnow() + timedelta(hours=24)

    db.add(user)
    try:
        await db.flush()

        if invitation:
            if invitation.initial_balance is not None:
                before = Decimal(str(user.balance or 0))
                delta = Decimal(str(invitation.initial_balance))
                user.balance = before + delta
                db.add(
                    BalanceTransaction(
                        user_id=user.id,
                        operator_user_id=invitation.inviter_user_id,
                        delta=float(delta),
                        before_balance=float(before),
                        after_balance=float(before + delta),
                        reason="INVITATION",
                    )
                )

            if invitation.plan_id:
                plan = (
                    await db.execute(select(Plan).where(Plan.id == invitation.plan_id))
                ).scalar()
                if plan:
                    now = datetime.utcnow()
                    subscription = Subscription(
                        user_id=user.id,
                        plan_id=plan.id,
                        status=SubscriptionStatus.ACTIVE,
                        billing_cycle=BillingCycle.UNSET,
                        start_at=now,
                        end_at=now + timedelta(days=int(plan.duration_days or 0)),
                        auto_renew=False,
                        source=SubscriptionSource.ADMIN,
                    )
                    user.vod_movie_limit = int(plan.vod_movie_times or 0)
                    user.vod_tv_limit = int(plan.vod_tv_times or 0)
                    db.add(subscription)
                    await db.flush()
                    await create_telegram_notification(
                        db,
                        user_id=user.id,
                        notification_type="subscription_activated",
                        title="订阅已激活",
                        content=f"您的{plan.name}已激活，有效期至{subscription.end_at.strftime('%Y-%m-%d')}",
                        reference_id=str(subscription.id),
                    )

            invitation.status = InvitationStatus.ACCEPTED
            db.add(invitation)
            db.add(user)

        if email_verification_enabled:
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

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="注册失败，信息冲突")
    except Exception as exc:
        await db.rollback()
        if email_verification_enabled:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="验证邮件发送失败，请稍后重试",
            ) from exc
        raise HTTPException(status_code=400, detail="注册失败，请稍后重试") from exc

    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(credentials: UserLogin, db: AsyncSession = Depends(get_db)):
    """用户登录"""
    await _ensure_admin_user(db)
    # 查询用户
    stmt = select(User).where(
        or_(User.username == credentials.username, User.email == credentials.username),
        User.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    user = result.scalar()

    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    settings_row = await _get_system_settings(db)
    if settings_row.email_verification_enabled and not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="邮箱尚未验证")

    _ensure_login_user_allowed(user)

    # 更新最后登录时间
    user.last_login_at = datetime.utcnow()
    db.add(user)
    await db.commit()

    # 生成 tokens
    access_token = create_token({"sub": str(user.id)}, token_type="access")
    refresh_token = create_token({"sub": str(user.id)}, token_type="refresh")

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(request: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """刷新 access token"""
    payload = verify_token(request.refresh_token, "refresh")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证凭据无效",
        )
    try:
        user_uuid = UUID(str(user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证凭据无效",
        ) from exc
    await ensure_token_user_is_active(user_uuid, db)

    access_token = create_token({"sub": str(user_uuid)}, token_type="access")
    new_refresh_token = create_token({"sub": str(user_uuid)}, token_type="refresh")

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """获取当前用户信息"""
    user_id = current_user["user_id"]
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    return user


@router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UserSelfUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户信息"""
    user_id = current_user["user_id"]
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    if payload.email and payload.email != user.email:
        dup_stmt = select(User).where(User.email == payload.email, User.id != user_id)
        dup = await db.execute(dup_stmt)
        if dup.scalar():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱已被注册")
        user.email = payload.email
        settings_row = await _get_system_settings(db)
        if settings_row.email_verification_enabled:
            user.email_verified = False
            user.email_verified_at = None
            user.email_verification_token = uuid4().hex
            user.email_verification_expires_at = datetime.utcnow() + timedelta(hours=24)

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

    if payload.phone is not None:
        user.phone = payload.phone

    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url

    if payload.password:
        user.password_hash = hash_password(payload.password)

    db.add(user)
    await db.commit()
    await db.refresh(user)

    if payload.password and user.email:
        settings_row = await _get_system_settings(db)
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

    return user


@router.post("/me/avatar", response_model=UserResponse)
async def update_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传并更新头像"""
    user_id = current_user["user_id"]
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的头像文件")

    suffix = Path(file.filename or "").suffix or ".png"
    filename = f"{uuid4().hex}{suffix}"
    upload_dir = Path(settings.AVATAR_UPLOAD_DIR).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / filename

    with dest.open("wb") as buffer:
        buffer.write(await file.read())

    user.avatar_url = f"{settings.AVATAR_URL_PREFIX}/{filename}"
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    """用户登出（可选，JWT 无状态）"""
    return {"message": "Logged out successfully"}


@router.get("/verify-email")
async def verify_email(token: str = Query(..., min_length=10), db: AsyncSession = Depends(get_db)):
    """验证邮箱"""
    stmt = select(User).where(User.email_verification_token == token)
    user = (await db.execute(stmt)).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的令牌")

    if (
        not user.email_verification_expires_at
        or user.email_verification_expires_at < datetime.utcnow()
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="令牌已过期")

    user.email_verified = True
    user.email_verified_at = datetime.utcnow()
    user.email_verification_token = None
    user.email_verification_expires_at = None
    db.add(user)
    await db.commit()
    return {"success": True, "message": "Email verified"}


@router.post("/verify-email/resend")
async def resend_verification(
    payload: EmailVerificationRequest, db: AsyncSession = Depends(get_db)
):
    """重新发送验证邮件"""
    settings_row = await _get_system_settings(db)
    if not settings_row.email_verification_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱验证功能未启用")

    stmt = select(User).where(User.email == payload.email)
    user = (await db.execute(stmt)).scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.email_verified:
        return {"success": True, "message": "Email already verified"}

    user.email_verification_token = uuid4().hex
    user.email_verification_expires_at = datetime.utcnow() + timedelta(hours=24)
    db.add(user)
    await db.commit()
    await db.refresh(user)

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
    return {"success": True, "message": "Verification email sent"}
