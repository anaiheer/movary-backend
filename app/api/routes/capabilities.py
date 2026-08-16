from uuid import uuid4

from fastapi import APIRouter

from app.core.edition import apply_capability_overrides, get_default_capabilities
from app.core.pro_extensions import get_backend_extension_state
from app.services.license_runtime import evaluate_cached_license

router = APIRouter(prefix="/settings/capabilities", tags=["capabilities"])


@router.get("")
async def get_capabilities() -> dict:
    capabilities = get_default_capabilities()
    runtime = evaluate_cached_license()
    extension_state = get_backend_extension_state()
    capabilities.extensions = extension_state
    capabilities.license_status = str(runtime.get("license_status") or "inactive")
    if extension_state.get("pro_effective"):
        capabilities.edition = "pro"
        loaded = extension_state.get("loaded") or []
        pro_extension = next((item for item in loaded if item.get("name") == "pro"), None)
        capabilities = apply_capability_overrides(
            capabilities, pro_extension.get("capability_overrides") if pro_extension else {}
        )
    return {
        "success": True,
        "message": "",
        "data": capabilities.model_dump(),
        "request_id": str(uuid4()),
    }
