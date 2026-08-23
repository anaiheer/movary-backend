from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.license_config import LICENSE_REQUEST_TIMEOUT_SECONDS
from app.services.license_runtime import evaluate_cached_license

LOCAL_PRO_BACKEND_PATH = Path("/app/movary-backend-pro")


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _state_file() -> Path:
    return (_base_dir() / "runtime" / "pro-artifacts.json").resolve()


def _backend_root() -> Path:
    return (_base_dir() / "runtime" / "extensions" / "pro-backend").resolve()


def _frontend_root_fs() -> Path:
    uploads_root = (
        Path(settings.AVATAR_UPLOAD_DIR).resolve().parent / "runtime" / "pro-frontend"
    ).resolve()
    return uploads_root


def _resolve_uploaded_url(path: Path) -> str | None:
    try:
        relative = path.relative_to(Path(settings.AVATAR_UPLOAD_DIR).resolve().parent)
        return f"/uploads/{relative.as_posix()}"
    except ValueError:
        return None


def _default_state() -> dict[str, Any]:
    return {
        "backend": {
            "status": "inactive",
            "version": None,
            "source_url": None,
            "sha256": None,
            "signature": None,
            "archive_path": None,
            "extracted_path": None,
            "error": None,
        },
        "frontend": {
            "status": "inactive",
            "version": None,
            "source_url": None,
            "sha256": None,
            "signature": None,
            "file_path": None,
            "local_entry_url": None,
            "style_file_path": None,
            "local_style_url": None,
            "error": None,
        },
    }


def load_pro_artifact_state() -> dict[str, Any]:
    state_file = _state_file()
    if not state_file.exists():
        return _default_state()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    state = _default_state()
    state["backend"].update(payload.get("backend") or {})
    state["frontend"].update(payload.get("frontend") or {})
    return state


def save_pro_artifact_state(state: dict[str, Any]) -> dict[str, Any]:
    state_file = _state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return state


def clear_active_pro_artifacts() -> dict[str, Any]:
    state = load_pro_artifact_state()
    for key in ("backend", "frontend"):
        state[key]["status"] = "inactive"
        state[key]["error"] = None
    return save_pro_artifact_state(state)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        timeout=LICENSE_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with target.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    fh.write(chunk)


def _extract_backend_archive(archive_path: Path, extract_dir: Path) -> Path:
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.suffix == ".zip" or archive_path.suffix == ".whl":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
        return extract_dir
    if archive_path.suffixes[-2:] == [".tar", ".gz"] or archive_path.suffix == ".tgz":
        with tarfile.open(archive_path) as tf:
            tf.extractall(extract_dir)
        return extract_dir
    raise ValueError("不支持的 backend artifact 格式")


def _normalize_architecture(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"amd64", "x86_64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "aarch64"
    return normalized


def _current_backend_runtime() -> dict[str, str]:
    return {
        "implementation": sys.implementation.name,
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "os": platform.system().lower(),
        "architecture": _normalize_architecture(platform.machine()),
    }


def _validate_backend_artifact(extract_dir: Path) -> dict[str, Any]:
    manifest_path = extract_dir / "movary-pro-artifact.json"
    if not manifest_path.is_file():
        raise ValueError("backend artifact 缺少二进制制品清单")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("artifact_type") != "backend":
        raise ValueError("backend artifact 类型无效")
    if manifest.get("format") != "cython-extension-zip":
        raise ValueError("backend artifact 不是受支持的编译制品")

    runtime = manifest.get("runtime") or {}
    expected_runtime = _current_backend_runtime()
    for key, expected in expected_runtime.items():
        actual = str(runtime.get(key) or "").strip().lower()
        if actual != expected:
            raise ValueError(
                f"backend artifact 运行时不兼容：{key}={actual or 'missing'}，需要 {expected}"
            )

    source_files = sorted(extract_dir.rglob("*.py")) + sorted(extract_dir.rglob("*.pyc"))
    if source_files:
        raise ValueError("backend artifact 包含 Python source 或 bytecode")
    extension_files = sorted(extract_dir.rglob("*.so"))
    if not extension_files:
        raise ValueError("backend artifact 未包含编译扩展")
    has_registry = any(
        path.parent.name == "movary_backend_pro" and path.name.startswith("registry.")
        for path in extension_files
    )
    if not has_registry:
        raise ValueError("backend artifact 缺少 registry 编译扩展")
    return manifest


def _resolve_frontend_entry(path: Path) -> str | None:
    if path.is_file() and path.suffix == ".js":
        return _resolve_uploaded_url(path)
    candidates = ["index.js", "entry.js", "manifest.js"]
    for name in candidates:
        candidate = path / name
        if candidate.exists():
            return _resolve_uploaded_url(candidate)
    return None


async def sync_pro_artifacts_from_license(provider_payload: dict[str, Any]) -> dict[str, Any]:
    state = load_pro_artifact_state()
    backend_version = str(provider_payload.get("backend_artifact_version") or "").strip() or None
    frontend_version = str(provider_payload.get("frontend_artifact_version") or "").strip() or None

    backend_url = str(provider_payload.get("backend_artifact_url") or "").strip()
    backend_sha = str(provider_payload.get("backend_sha256") or "").strip() or None
    if backend_url and backend_version:
        backend_filename = (
            Path(urlparse(backend_url).path).name or f"pro-backend-{backend_version}.zip"
        )
        archive_path = _backend_root() / backend_version / backend_filename
        extract_dir = _backend_root() / backend_version / "extracted"
        await _download(backend_url, archive_path)
        actual_sha = _sha256(archive_path)
        if backend_sha and actual_sha != backend_sha:
            raise ValueError("backend artifact 校验失败")
        extracted_path = _extract_backend_archive(archive_path, extract_dir)
        _validate_backend_artifact(extracted_path)
        state["backend"].update(
            {
                "status": "ready",
                "version": backend_version,
                "source_url": backend_url,
                "sha256": actual_sha,
                "signature": None,
                "archive_path": str(archive_path),
                "extracted_path": str(extracted_path),
                "error": None,
            }
        )
    elif backend_url:
        state["backend"].update({"status": "error", "error": "backend artifact 缺少版本号"})

    frontend_url = str(provider_payload.get("frontend_artifact_url") or "").strip()
    frontend_sha = str(provider_payload.get("frontend_sha256") or "").strip() or None
    if frontend_url and frontend_version:
        frontend_filename = (
            Path(urlparse(frontend_url).path).name or f"pro-frontend-{frontend_version}.js"
        )
        frontend_target = _frontend_root_fs() / frontend_version / frontend_filename
        await _download(frontend_url, frontend_target)
        actual_sha = _sha256(frontend_target)
        if frontend_sha and actual_sha != frontend_sha:
            raise ValueError("frontend artifact 校验失败")
        style_filename = str(provider_payload.get("frontend_style_filename") or "").strip()
        style_sha = str(provider_payload.get("frontend_style_sha256") or "").strip() or None
        style_target = None
        local_style_url = None
        if style_filename:
            style_target = frontend_target.with_name(style_filename)
            style_url = frontend_url.rsplit("/", 1)[0] + f"/{style_filename}"
            await _download(style_url, style_target)
            if style_sha and _sha256(style_target) != style_sha:
                raise ValueError("frontend style artifact 校验失败")
            local_style_url = _resolve_uploaded_url(style_target)
        local_entry_url = _resolve_frontend_entry(frontend_target)
        state["frontend"].update(
            {
                "status": "ready" if local_entry_url else "cached",
                "version": frontend_version,
                "source_url": frontend_url,
                "sha256": actual_sha,
                "signature": None,
                "file_path": str(frontend_target),
                "local_entry_url": local_entry_url,
                "style_file_path": str(style_target) if style_target else None,
                "local_style_url": local_style_url,
                "error": None if local_entry_url else "未找到可加载的 frontend entry",
            }
        )
    elif frontend_url:
        state["frontend"].update({"status": "error", "error": "frontend artifact 缺少版本号"})

    return save_pro_artifact_state(state)


def get_effective_backend_extension_path() -> str | None:
    if (LOCAL_PRO_BACKEND_PATH / "movary_backend_pro").is_dir():
        return str(LOCAL_PRO_BACKEND_PATH)
    runtime = evaluate_cached_license()
    if runtime.get("license_status") not in {"active", "expired"}:
        return None
    state = load_pro_artifact_state()
    extracted_path = str(state["backend"].get("extracted_path") or "").strip()
    if state["backend"].get("status") == "ready" and extracted_path:
        return extracted_path
    return None


def get_public_artifact_state() -> dict[str, Any]:
    return load_pro_artifact_state()
