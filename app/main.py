import asyncio
import logging
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    auth,
    capabilities,
    docs,
    emby,
    server_config,
    subscription_config,
    user_plans,
    google,
    orders,
    payments,
    plans,
    settings as settings_routes,
    telegram,
    tickets,
    users,
    vod,
)
from app.api.routes import (
    admin_dashboard,
    admin_docs,
    admin_invitations,
    admin_license,
    admin_orders,
    admin_settings,
    admin_tasks,
    admin_tickets,
    admin_users,
    admin_vod,
)
from app.core.config import settings
from app.core.pro_extensions import (
    get_backend_extension_state,
    include_extension_routes,
    initialize_backend_extensions,
)
from app.services.cache import close_cache, init_cache
from app.services.emby_passwords import validate_emby_password_key
from app.services.system_tasks import shutdown_scheduler, start_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await init_cache()
    try:
        validate_emby_password_key()
    except ValueError as exc:
        logger.error("Emby password encryption check failed: %s", exc)
    try:
        await asyncio.to_thread(_run_migrations)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            logger.info("Auto migration skipped: %s", exc)
        else:
            logger.warning("Auto migration failed: %s", exc)
    await start_scheduler()
    yield
    shutdown_scheduler()
    await close_cache()


app = FastAPI(title=settings.PROJECT_NAME, debug=settings.DEBUG, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def guard_pro_routes(request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith(f"{settings.API_V1_STR}/pro/"):
        extension_state = get_backend_extension_state()
        if extension_state.get("pro_effective"):
            return await call_next(request)
        if extension_state.get("pro_readonly") and request.method in {"GET", "HEAD", "OPTIONS"}:
            return await call_next(request)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=403 if request.method not in {"GET", "HEAD", "OPTIONS"} else 404,
            content={"detail": "专业版功能未授权、已过期或当前仅允许只读访问"},
        )
    return await call_next(request)


app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(docs.router, prefix=settings.API_V1_STR)
app.include_router(plans.router, prefix=settings.API_V1_STR)
app.include_router(orders.router, prefix=settings.API_V1_STR)
app.include_router(payments.router, prefix=settings.API_V1_STR)
app.include_router(emby.router, prefix=settings.API_V1_STR)
app.include_router(vod.router, prefix=settings.API_V1_STR)
app.include_router(users.router, prefix=settings.API_V1_STR)
app.include_router(tickets.router, prefix=settings.API_V1_STR)
app.include_router(telegram.router, prefix=settings.API_V1_STR)
app.include_router(google.router, prefix=settings.API_V1_STR)
app.include_router(capabilities.router, prefix=settings.API_V1_STR)
app.include_router(server_config.router, prefix=settings.API_V1_STR)
app.include_router(subscription_config.router, prefix=settings.API_V1_STR)
app.include_router(user_plans.router, prefix=settings.API_V1_STR)
app.include_router(settings_routes.router, prefix=settings.API_V1_STR)
app.include_router(admin_dashboard.router, prefix=settings.API_V1_STR)
app.include_router(admin_users.router, prefix=settings.API_V1_STR)
app.include_router(admin_invitations.router, prefix=settings.API_V1_STR)
app.include_router(admin_orders.router, prefix=settings.API_V1_STR)
app.include_router(admin_docs.router, prefix=settings.API_V1_STR)
app.include_router(admin_vod.router, prefix=settings.API_V1_STR)
app.include_router(admin_license.router, prefix=settings.API_V1_STR)
app.include_router(admin_tickets.router, prefix=settings.API_V1_STR)
app.include_router(admin_tasks.router, prefix=settings.API_V1_STR)
app.include_router(admin_settings.router, prefix=settings.API_V1_STR)

initialize_backend_extensions()
include_extension_routes(app)

extension_state = get_backend_extension_state()
if extension_state["loaded"]:
    logger.info("Loaded backend extensions: %s", extension_state["loaded"])
if extension_state["failed"]:
    logger.warning("Backend extension load failures: %s", extension_state["failed"])

avatar_upload_dir = Path(settings.AVATAR_UPLOAD_DIR).resolve()
avatar_upload_dir.mkdir(parents=True, exist_ok=True)
uploads_root = avatar_upload_dir.parent
uploads_root.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_root)), name="uploads")


def _run_migrations() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    alembic_ini = base_dir / "alembic.ini"
    for attempt in range(3):
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
                check=True,
            )
            return
        except subprocess.CalledProcessError as exc:
            if attempt < 2:
                logger.warning("Auto migration attempt %d/3 failed: %s", attempt + 1, exc)
                time.sleep(5 * (attempt + 1))
            else:
                raise


@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.PROJECT_NAME}"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
