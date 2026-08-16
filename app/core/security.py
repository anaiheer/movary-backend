from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import InvalidTokenError
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User, UserStatus

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_token(
    data: dict, expires_delta: Optional[timedelta] = None, token_type: str = "access"
) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        if token_type == "refresh":
            expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
            )

    to_encode.update({"exp": expire, "type": token_type})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="认证凭据无效",
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_token(token: str, token_type: str = "access") -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != token_type:
            raise InvalidTokenError("Invalid token type")
        return payload
    except InvalidTokenError as exc:
        raise _invalid_credentials() from exc


async def ensure_token_user_is_active(
    user_id: UUID,
    db: AsyncSession,
    *,
    reject_statuses: tuple[UserStatus, ...] = (UserStatus.BANNED, UserStatus.DELETED),
) -> User:
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise _invalid_credentials()
    if user.status in reject_statuses:
        raise _invalid_credentials()
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = credentials.credentials
    payload = verify_token(token, "access")
    user_id = payload.get("sub")
    if user_id is None:
        raise _invalid_credentials()
    try:
        user_uuid = UUID(user_id)
    except ValueError as exc:
        raise _invalid_credentials() from exc

    user = await ensure_token_user_is_active(user_uuid, db)
    return {
        "user_id": user_uuid,
        "payload": payload,
        "user_status": user.status.value if hasattr(user.status, "value") else str(user.status),
        "user_role": user.role.value if hasattr(user.role, "value") else str(user.role),
    }
