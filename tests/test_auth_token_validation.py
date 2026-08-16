from datetime import datetime
import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_token, hash_password
from app.db.session import AsyncSessionLocal
from app.models.user import User, UserStatus


async def create_user(*, status: UserStatus = UserStatus.ACTIVE, deleted: bool = False) -> User:
    async with AsyncSessionLocal() as session:
        username = f"auth_state_{uuid.uuid4().hex[:8]}"
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("Test123456"),
            status=status,
            deleted_at=datetime.utcnow() if deleted else None,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.mark.asyncio
async def test_banned_user_login_rejected(async_client):
    user = await create_user(status=UserStatus.BANNED)

    response = await async_client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": "Test123456"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_banned_user_access_token_rejected(async_client):
    user = await create_user(status=UserStatus.BANNED)
    token = create_token({"sub": str(user.id)}, token_type="access")

    response = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "认证凭据无效"


@pytest.mark.asyncio
async def test_deleted_user_refresh_token_rejected(async_client):
    user = await create_user(status=UserStatus.DELETED, deleted=True)
    refresh_token = create_token({"sub": str(user.id)}, token_type="refresh")

    response = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "认证凭据无效"


@pytest.mark.asyncio
async def test_deleted_user_hidden_from_authenticated_profile(async_client):
    user = await create_user()
    access_token = create_token({"sub": str(user.id)}, token_type="access")

    async with AsyncSessionLocal() as session:
        db_user = (await session.execute(select(User).where(User.id == user.id))).scalar()
        assert db_user is not None
        db_user.status = UserStatus.DELETED
        db_user.deleted_at = datetime.utcnow()
        session.add(db_user)
        await session.commit()

    response = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "认证凭据无效"


@pytest.mark.asyncio
async def test_update_me_rejects_weak_password(async_client):
    user = await create_user()
    access_token = create_token({"sub": str(user.id)}, token_type="access")

    response = await async_client.patch(
        "/api/v1/auth/me",
        json={"password": "12345678"},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "password" for item in detail)
    assert any("密码至少 8 位，且需同时包含字母和数字" in item["msg"] for item in detail)
