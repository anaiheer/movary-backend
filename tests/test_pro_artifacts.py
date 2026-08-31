import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import pro_extensions
from app.services import pro_artifacts


def _write_binary_manifest(root: Path, **runtime_overrides: str) -> None:
    runtime = {
        "implementation": "cpython",
        "python_abi": "cp311",
        "os": "linux",
        "architecture": "x86_64",
        **runtime_overrides,
    }
    (root / "movary-pro-artifact.json").write_text(
        json.dumps(
            {
                "artifact_type": "backend",
                "format": "cython-extension-zip",
                "runtime": runtime,
            }
        ),
        encoding="utf-8",
    )


def test_validate_backend_artifact_accepts_matching_binary_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = tmp_path / "movary_backend_pro"
    package_dir.mkdir()
    (package_dir / "registry.cpython-311-x86_64-linux-gnu.so").write_bytes(b"binary")
    _write_binary_manifest(tmp_path)
    monkeypatch.setattr(
        pro_artifacts,
        "_current_backend_runtime",
        lambda: {
            "implementation": "cpython",
            "python_abi": "cp311",
            "os": "linux",
            "architecture": "x86_64",
        },
    )

    manifest = pro_artifacts._validate_backend_artifact(tmp_path)

    assert manifest["format"] == "cython-extension-zip"


def test_validate_backend_artifact_rejects_source_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = tmp_path / "movary_backend_pro"
    package_dir.mkdir()
    (package_dir / "registry.py").write_text("secret = True\n", encoding="utf-8")
    _write_binary_manifest(tmp_path)
    monkeypatch.setattr(
        pro_artifacts,
        "_current_backend_runtime",
        lambda: {
            "implementation": "cpython",
            "python_abi": "cp311",
            "os": "linux",
            "architecture": "x86_64",
        },
    )

    with pytest.raises(ValueError, match="Python source"):
        pro_artifacts._validate_backend_artifact(tmp_path)


def test_backend_extension_contract_loads_registry_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []
    registry_module = SimpleNamespace(
        get_backend_pro_manifest=lambda: {"edition": "pro"},
        register_backend_extensions=lambda: {"route_modules": {}},
    )

    def fake_import(name: str):
        imported.append(name)
        return registry_module

    monkeypatch.setattr(pro_extensions.importlib, "import_module", fake_import)

    manifest, registry = pro_extensions._load_backend_extension_contract()

    assert imported == ["movary_backend_pro.registry"]
    assert manifest == {"edition": "pro"}
    assert registry == {"route_modules": {}}


@pytest.mark.asyncio
async def test_download_wraps_connect_error_and_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.zip"

    class FailingClient:
        async def __aenter__(self):
            raise __import__("httpx").ConnectError("connection refused")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        pro_artifacts.httpx,
        "AsyncClient",
        lambda **_kwargs: FailingClient(),
    )

    with pytest.raises(pro_artifacts.ProArtifactError, match="下载 Pro artifact 失败") as exc_info:
        await pro_artifacts._download("http://provider/artifact.zip", target)

    assert "http://provider/artifact.zip" not in str(exc_info.value)
    assert "connection refused" not in str(exc_info.value)
    assert not target.exists()
    assert not target.with_name(".artifact.zip.part").exists()


@pytest.mark.asyncio
async def test_sync_uses_independent_backend_and_frontend_pro_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pro_artifacts, "_state_file", lambda: tmp_path / "state.json")
    monkeypatch.setattr(pro_artifacts, "_backend_root", lambda: tmp_path / "backend")
    monkeypatch.setattr(pro_artifacts, "_frontend_root_fs", lambda: tmp_path / "frontend")
    monkeypatch.setattr(
        pro_artifacts,
        "_resolve_uploaded_url",
        lambda path: f"/uploads/{path.name}",
    )

    async def fake_download(_url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"artifact")

    def fake_extract(_archive_path: Path, extract_dir: Path) -> Path:
        extract_dir.mkdir(parents=True, exist_ok=True)
        return extract_dir

    monkeypatch.setattr(pro_artifacts, "_download", fake_download)
    monkeypatch.setattr(pro_artifacts, "_extract_backend_archive", fake_extract)
    monkeypatch.setattr(pro_artifacts, "_validate_backend_artifact", lambda _path: {})

    state = await pro_artifacts.sync_pro_artifacts_from_license(
        {
            "base_version": "1.0.2",
            "backend_artifact_version": "v2.1.0",
            "backend_artifact_url": "https://movary.top/artifacts/backend/v2.1.0/backend.zip",
            "frontend_artifact_version": "v3.4.0",
            "frontend_artifact_url": "https://movary.top/artifacts/frontend/v3.4.0/index.js",
        }
    )

    assert state["backend"]["version"] == "v2.1.0"
    assert "v2.1.0" in state["backend"]["archive_path"]
    assert state["frontend"]["version"] == "v3.4.0"
    assert "v3.4.0" in state["frontend"]["file_path"]
