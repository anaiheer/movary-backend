from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.doc import Doc
from app.schemas.doc import DocOut, UserDocsListResponse
from app.services.cache import get_json, set_json


router = APIRouter(prefix="/docs", tags=["docs"])

VISIBLE_DOCS_CACHE_KEY = "docs:visible:v1"
VISIBLE_DOCS_CACHE_TTL = 300


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


@router.get("", response_model=dict)
async def list_visible_docs(
    _current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cached = await get_json(VISIBLE_DOCS_CACHE_KEY)
    if isinstance(cached, dict):
        return _response(cached)

    stmt = (
        select(Doc)
        .where(Doc.is_visible.is_(True))
        .order_by(Doc.sort_order.asc(), Doc.updated_at.desc(), Doc.created_at.desc())
    )
    docs = (await db.execute(stmt)).scalars().all()
    payload = UserDocsListResponse(items=[DocOut.model_validate(item) for item in docs]).model_dump(
        mode="json"
    )
    await set_json(VISIBLE_DOCS_CACHE_KEY, payload, VISIBLE_DOCS_CACHE_TTL)
    return _response(payload)
