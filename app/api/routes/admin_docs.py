from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.doc import Doc
from app.models.user import User, UserRole
from app.schemas.doc import AdminDocsListResponse, DocCreate, DocOut, DocUpdate
from app.services.cache import delete


router = APIRouter(prefix="/admin/docs", tags=["admin-docs"])

VISIBLE_DOCS_CACHE_KEY = "docs:visible:v1"


class AdminDocDeleteRequest(BaseModel):
    ids: list[UUID]


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _invalidate_visible_docs_cache() -> None:
    await delete(VISIBLE_DOCS_CACHE_KEY)


async def _ensure_admin(current_user: dict, db: AsyncSession) -> User:
    stmt = select(User).where(User.id == current_user["user_id"])
    user = (await db.execute(stmt)).scalar()
    if not user or user.role not in {UserRole.ADMIN, UserRole.SUPERADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Need admin permission")
    return user


async def _delete_docs_by_ids(doc_ids: list[UUID], db: AsyncSession) -> dict:
    requested_ids: list[UUID] = []
    seen_ids: set[UUID] = set()
    for doc_id in doc_ids:
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        requested_ids.append(doc_id)

    if not requested_ids:
        return {
            "requested": 0,
            "deleted": 0,
            "missing": 0,
            "missing_ids": [],
            "failed_ids": [],
        }

    docs = (
        (
            await db.execute(
                select(Doc).where(Doc.id.in_(requested_ids)).order_by(Doc.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    doc_map = {doc.id: doc for doc in docs}
    missing_ids = [str(doc_id) for doc_id in requested_ids if doc_id not in doc_map]
    failed_ids: list[str] = []
    deleted_count = 0

    for doc_id in requested_ids:
        doc = doc_map.get(doc_id)
        if not doc:
            continue
        try:
            async with db.begin_nested():
                await db.execute(sa_delete(Doc).where(Doc.id == doc_id))
                await db.flush()
            deleted_count += 1
        except Exception:
            failed_ids.append(str(doc_id))

    await db.commit()
    if deleted_count > 0:
        await _invalidate_visible_docs_cache()

    return {
        "requested": len(requested_ids),
        "deleted": deleted_count,
        "missing": len(missing_ids),
        "missing_ids": missing_ids,
        "failed_ids": failed_ids,
    }


def _build_doc_query(keyword: str | None = None, visible: bool | None = None):
    stmt = select(Doc)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where((Doc.title.ilike(like)) | (Doc.content.ilike(like)))
    if visible is not None:
        stmt = stmt.where(Doc.is_visible.is_(visible))
    return stmt.order_by(Doc.sort_order.asc(), Doc.updated_at.desc(), Doc.created_at.desc())


@router.get("", response_model=dict)
async def list_docs(
    keyword: str | None = Query(default=None),
    visible: bool | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    docs = (await db.execute(_build_doc_query(keyword=keyword, visible=visible))).scalars().all()
    payload = AdminDocsListResponse(items=[DocOut.model_validate(item) for item in docs])
    return _response(payload.model_dump(mode="json"))


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_doc(
    payload: DocCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    doc = Doc(
        title=payload.title.strip(),
        content=payload.content,
        is_visible=payload.is_visible,
        sort_order=payload.sort_order,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    await _invalidate_visible_docs_cache()
    return _response(DocOut.model_validate(doc).model_dump(mode="json"), "Document created")


@router.get("/{doc_id}", response_model=dict)
async def get_doc(
    doc_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    doc = await db.get(Doc, doc_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _response(DocOut.model_validate(doc).model_dump(mode="json"))


@router.patch("/{doc_id}", response_model=dict)
async def update_doc(
    doc_id: UUID,
    payload: DocUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    doc = await db.get(Doc, doc_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if payload.title is not None:
        doc.title = payload.title.strip()
    if payload.content is not None:
        doc.content = payload.content
    if payload.is_visible is not None:
        doc.is_visible = payload.is_visible
    if payload.sort_order is not None:
        doc.sort_order = payload.sort_order

    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    await _invalidate_visible_docs_cache()
    return _response(DocOut.model_validate(doc).model_dump(mode="json"), "Document updated")


@router.delete("/{doc_id}", response_model=dict)
async def delete_doc(
    doc_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    doc = await db.get(Doc, doc_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    await db.delete(doc)
    await db.commit()
    await _invalidate_visible_docs_cache()
    return _response({"id": str(doc_id)}, "Document deleted")


@router.post("/batch-delete", response_model=dict)
async def batch_delete_docs(
    payload: AdminDocDeleteRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(current_user, db)
    result = await _delete_docs_by_ids(payload.ids, db)
    return _response(result)
