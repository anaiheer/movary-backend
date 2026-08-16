import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles

# Ensure backend root is on sys.path so tests can import "app".
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

TEST_DB_URL = f"sqlite+aiosqlite:///{Path(BASE_DIR, 'test.db').as_posix()}"

os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("MAIL_SUPPRESS_SEND", "1")


@compiles(UUID, "sqlite")
def compile_uuid_sqlite(element, compiler, **kw):
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(element, compiler, **kw):
    return "JSON"


def get_app_dependencies():
    from app.core.security import create_token, hash_password
    from app.db.session import AsyncSessionLocal, Base, engine
    from app.main import app
    from app.models.user import User, UserRole

    return app, AsyncSessionLocal, Base, engine, create_token, hash_password, User, UserRole


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def prepare_database():
    _, _, Base, engine, _, _, _, _ = get_app_dependencies()
    test_db_path = Path(BASE_DIR, "test.db")
    await engine.dispose()
    if test_db_path.exists():
        test_db_path.unlink()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture()
async def admin_token():
    _, AsyncSessionLocal, _, _, create_token, hash_password, User, UserRole = get_app_dependencies()
    async with AsyncSessionLocal() as session:
        username = f"admin_test_{uuid.uuid4().hex[:8]}"
        email = f"{username}@example.com"
        user = User(
            username=username,
            email=email,
            password_hash=hash_password("Test123456"),
            role=UserRole.ADMIN,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id
        token = create_token({"sub": str(user_id)}, token_type="access")

    yield token

    async with AsyncSessionLocal() as session:
        obj = await session.get(User, user_id)
        if obj:
            await session.delete(obj)
        await session.commit()


@pytest_asyncio.fixture()
async def async_client():
    app, _, _, _, _, _, _, _ = get_app_dependencies()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
