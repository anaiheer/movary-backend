import sys
from pathlib import Path

from app.core import pro_extensions
from app.services import pro_artifacts


def _reset_extension_state() -> None:
    pro_extensions._extension_state = None


def test_backend_extensions_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pro_artifacts, "LOCAL_PRO_BACKEND_PATH", tmp_path / "missing")
    _reset_extension_state()
    state = pro_extensions.get_backend_extension_state()
    assert state["enabled"] == []
    assert state["loaded"] == []
    assert state["failed"] == []


def test_backend_extensions_can_load_discovered_pro_package(tmp_path: Path, monkeypatch) -> None:
    package_dir = tmp_path / "movary_backend_pro"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "registry.py").write_text(
        "def get_backend_pro_manifest():\n"
        "    return {'edition': 'pro'}\n\n"
        "def register_backend_extensions():\n"
        "    return {'route_groups': ['advanced-servers'], 'route_modules': {}}\n",
        encoding="utf-8",
    )
    sys.modules.pop("movary_backend_pro", None)
    sys.modules.pop("movary_backend_pro.registry", None)
    monkeypatch.setattr(pro_artifacts, "LOCAL_PRO_BACKEND_PATH", tmp_path)
    _reset_extension_state()
    try:
        state = pro_extensions.get_backend_extension_state()
        assert state["enabled"] == ["pro"]
        assert state["loaded"][0]["name"] == "pro"
        assert state["loaded"][0]["route_groups"] == ["advanced-servers"]
        assert state["failed"] == []
    finally:
        sys.modules.pop("movary_backend_pro", None)
        sys.modules.pop("movary_backend_pro.registry", None)
        _reset_extension_state()
