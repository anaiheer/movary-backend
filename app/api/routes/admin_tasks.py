from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.user import User, UserRole
from app.models.system_task import SystemTask
from app.models.system_task_log import SystemTaskLog
from app.schemas.system_task import (
    SystemTaskOut,
    SystemTaskUpdate,
    SystemTaskRunResult,
    SystemTaskLogItem,
)
from app.services.system_tasks import (
    LOCKED_ENABLED_TASK_KEYS,
    ensure_default_tasks,
    refresh_schedule,
    run_task_by_id,
)

router = APIRouter(prefix="/admin/tasks", tags=["admin-tasks"])


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data}


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


@router.get("", response_model=dict)
async def list_tasks(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    await ensure_default_tasks()
    tasks = (
        (await db.execute(select(SystemTask).order_by(SystemTask.created_at.asc()))).scalars().all()
    )
    items = [SystemTaskOut.model_validate(task).model_dump(mode="json") for task in tasks]
    return _response({"items": items})


@router.put("/{task_id}", response_model=dict)
async def update_task(
    task_id: UUID,
    payload: SystemTaskUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    task = await db.scalar(select(SystemTask).where(SystemTask.id == task_id))
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    task.interval_seconds = payload.interval_seconds
    task.enabled = True if task.key in LOCKED_ENABLED_TASK_KEYS else payload.enabled
    db.add(task)
    await db.commit()
    await db.refresh(task)

    refresh_schedule(task)
    return _response(SystemTaskOut.model_validate(task).model_dump(mode="json"))


@router.post("/{task_id}/run", response_model=dict)
async def run_task(
    task_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    task = await db.scalar(select(SystemTask).where(SystemTask.id == task_id))
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    result = await run_task_by_id(str(task_id))
    payload = SystemTaskRunResult(
        task_id=task.id,
        status="SUCCESS",
        message=result.get("message", "ok"),
        updated=int(result.get("updated", 0)),
        checked=int(result.get("checked", 0)),
    ).model_dump(mode="json")
    return _response(payload)


@router.get("/logs", response_model=dict)
async def list_task_logs(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
):
    await _ensure_admin(current_user, db)
    total = await db.scalar(select(func.count()).select_from(SystemTaskLog))
    stmt = (
        select(SystemTaskLog)
        .order_by(SystemTaskLog.run_at.desc())
        .limit(size)
        .offset((page - 1) * size)
    )
    logs = (await db.execute(stmt)).scalars().all()
    items = [
        SystemTaskLogItem(
            task_id=log.task_id,
            task_key=log.task_key,
            task_name=log.task_name,
            status=log.status,
            message=log.message,
            run_at=log.run_at,
            duration_ms=log.duration_ms,
        ).model_dump(mode="json")
        for log in logs
    ]
    return _response({"items": items, "total": int(total or 0)})
