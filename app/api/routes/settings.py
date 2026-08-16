from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.system_settings import SystemSettings
from app.services.client_apps import normalize_client_app_configs
from app.services.social_auth import normalize_social_auth_providers
from app.services.site_languages import normalize_site_languages

router = APIRouter(prefix="/settings", tags=["settings"])


def _response(data: dict, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data, "request_id": str(uuid4())}


async def _get_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/public", response_model=dict)
async def get_public_settings(db: AsyncSession = Depends(get_db)):
    row = await _get_settings(db)
    enabled_languages, default_language = normalize_site_languages(
        row.enabled_languages, row.default_language
    )
    return _response(
        {
            "site_name": row.site_name or "Movary",
            "site_url": row.site_url,
            "site_logo_url": row.site_logo_url,
            "enabled_languages": enabled_languages,
            "default_language": default_language,
            "email_verification_enabled": row.email_verification_enabled,
            "invite_registration_enabled": row.invite_registration_enabled,
            "emby_client_apps": normalize_client_app_configs(
                row.emby_client_apps, only_enabled=True
            ),
            "social_auth_providers": normalize_social_auth_providers(
                row.social_auth_providers, public=True
            ),
        }
    )
