import sys
from pathlib import Path

from app.core import pro_extensions
from app.core.config import settings


def _reset_extension_state() -> None:
    pro_extensions._extension_state = None


def test_backend_extensions_disabled_by_default() -> None:
    previous = list(settings.MOVARY_BACKEND_EXTENSIONS)
    previous_path = settings.MOVARY_BACKEND_PRO_PATH
    settings.MOVARY_BACKEND_EXTENSIONS = []
    settings.MOVARY_BACKEND_PRO_PATH = None
    _reset_extension_state()
    try:
        state = pro_extensions.get_backend_extension_state()
        assert state["enabled"] == []
        assert state["loaded"] == []
        assert state["failed"] == []
    finally:
        settings.MOVARY_BACKEND_EXTENSIONS = previous
        settings.MOVARY_BACKEND_PRO_PATH = previous_path
        _reset_extension_state()


def test_backend_extensions_can_load_fake_pro_package(tmp_path: Path) -> None:
    package_dir = tmp_path / "movary_backend_pro"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        "def get_backend_pro_manifest():\n"
        "    return {'edition': 'pro'}\n\n"
        "def register_backend_extensions():\n"
        "    return {'route_groups': ['advanced-servers'], 'route_modules': {}}\n",
        encoding="utf-8",
    )
    previous = list(settings.MOVARY_BACKEND_EXTENSIONS)
    previous_path = settings.MOVARY_BACKEND_PRO_PATH
    sys.modules.pop("movary_backend_pro", None)
    settings.MOVARY_BACKEND_EXTENSIONS = ["pro"]
    settings.MOVARY_BACKEND_PRO_PATH = str(tmp_path)
    _reset_extension_state()
    try:
        state = pro_extensions.get_backend_extension_state()
        assert state["enabled"] == ["pro"]
        assert state["loaded"][0]["name"] == "pro"
        assert state["loaded"][0]["route_groups"] == ["advanced-servers"]
        assert state["failed"] == []
    finally:
        settings.MOVARY_BACKEND_EXTENSIONS = previous
        settings.MOVARY_BACKEND_PRO_PATH = previous_path
        sys.modules.pop("movary_backend_pro", None)
        _reset_extension_state()
