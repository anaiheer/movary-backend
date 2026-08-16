from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

# Ensure backend root is on sys.path so Alembic can import "app".
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except KeyError:
        # Ignore missing logging config in simple local runs.
        pass


def get_target_metadata():
    import app.models  # noqa: F401
    from app.db.session import Base

    return Base.metadata


target_metadata = get_target_metadata()


def run_migrations_offline() -> None:
    """Offline mode: generate SQL."""
    from app.core.config import settings

    url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online mode: run migrations against the database."""
    from app.db.session import engine

    connectable = engine

    async def process_migrations() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)

    asyncio.run(process_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
