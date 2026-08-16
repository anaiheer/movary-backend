from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.core.config import settings
from app.services.pro_artifacts import (
    get_effective_backend_extension_path,
    get_public_artifact_state,
)
from app.services.license_runtime import evaluate_cached_license

logger = logging.getLogger(__name__)

_extension_state: dict[str, Any] | None = None
_registered_route_modules: set[str] = set()


def _safe_json(data: Any) -> Any:
    if isinstance(data, dict):
        return {str(key): _safe_json(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_safe_json(item) for item in data]
    return data


def _route_specs_from_route_modules(route_modules: dict[str, str]) -> list[dict[str, Any]]:
    route_specs: list[dict[str, Any]] = []
    for name, module_path in route_modules.items():
        module = importlib.import_module(module_path)
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            raise TypeError(f"{module_path} does not expose a FastAPI APIRouter named 'router'")
        route_specs.append(
            {
                "name": name,
                "module_path": module_path,
                "mount_prefix": "/pro",
                "router": router,
            }
        )
    return route_specs


def initialize_backend_extensions() -> dict[str, Any]:
    global _extension_state
    if _extension_state is not None:
        return _extension_state

    configured = list(settings.MOVARY_BACKEND_EXTENSIONS)
    loaded: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    route_specs: list[dict[str, Any]] = []
    effective_pro_path = get_effective_backend_extension_path()
    should_try_pro = "pro" in configured or bool(effective_pro_path)
    enabled = list(configured)
    if should_try_pro and "pro" not in enabled:
        enabled.append("pro")

    if should_try_pro:
        try:
            if effective_pro_path:
                pro_path = str(Path(effective_pro_path).resolve())
                if pro_path not in sys.path:
                    sys.path.insert(0, pro_path)

            extension_module = importlib.import_module("movary_backend_pro")
            manifest = getattr(extension_module, "get_backend_pro_manifest", lambda: {})()
            registry = getattr(extension_module, "register_backend_extensions", lambda: {})()
            current_route_specs = _route_specs_from_route_modules(registry.get("route_modules", {}))
            route_specs.extend(current_route_specs)
            loaded.append(
                {
                    "name": "pro",
                    "manifest": _safe_json(manifest),
                    "route_groups": list(registry.get("route_groups", [])),
                    "route_modules": list(registry.get("route_modules", {}).keys()),
                    "capability_overrides": _safe_json(registry.get("capability_overrides", {})),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to initialize backend extension 'pro': %s", exc)
            failed.append({"name": "pro", "error": str(exc)})

    _extension_state = {
        "enabled": enabled,
        "loaded": loaded,
        "failed": failed,
        "route_specs": route_specs,
    }
    return _extension_state


def get_backend_extension_state() -> dict[str, Any]:
    state = initialize_backend_extensions()
    runtime = evaluate_cached_license()
    artifacts = get_public_artifact_state()
    return {
        "enabled": list(state["enabled"]),
        "loaded": _safe_json(state["loaded"]),
        "failed": _safe_json(state["failed"]),
        "pro_effective": bool(runtime.get("is_active"))
        and any(item.get("name") == "pro" for item in state["loaded"]),
        "pro_readonly": str(runtime.get("license_status") or "") == "expired"
        and any(item.get("name") == "pro" for item in state["loaded"]),
        "pro_backend_artifact": _safe_json(artifacts.get("backend")),
        "pro_frontend_artifact": _safe_json(artifacts.get("frontend")),
    }


def get_backend_extension_route_specs() -> list[dict[str, Any]]:
    state = initialize_backend_extensions()
    return list(state["route_specs"])


def reset_backend_extensions() -> None:
    global _extension_state
    _extension_state = None


def include_extension_routes(app) -> None:  # type: ignore[no-untyped-def]
    state = initialize_backend_extensions()
    for route_spec in state["route_specs"]:
        module_path = str(route_spec["module_path"])
        if module_path in _registered_route_modules:
            continue
        app.include_router(
            route_spec["router"], prefix=f"{settings.API_V1_STR}{route_spec['mount_prefix']}"
        )
        _registered_route_modules.add(module_path)
