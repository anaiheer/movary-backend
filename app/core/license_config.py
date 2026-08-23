from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LICENSE_PROVIDER_URL = "https://movary.top"
LICENSE_REQUEST_TIMEOUT_SECONDS = 10
_TRUST_DIR = Path(__file__).resolve().parent
LICENSE_PUBLIC_KEY = (_TRUST_DIR / "license_public.pem").read_text(encoding="ascii").strip()
LICENSE_KEY_ID = (_TRUST_DIR / "license_key_id.txt").read_text(encoding="ascii").strip()


class LicenseSettings(BaseSettings):
    """Runtime paths plus a hidden provider URL override for integration testing."""

    state_file: Optional[str] = Field(default=None, validation_alias="MOVARY_LICENSE_STATE_FILE")
    instance_file: Optional[str] = Field(
        default=None,
        validation_alias="MOVARY_LICENSE_INSTANCE_FILE",
    )
    provider_url: str = Field(
        default=DEFAULT_LICENSE_PROVIDER_URL,
        validation_alias="MOVARY_LICENSE_SERVER_URL",
    )

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


license_settings = LicenseSettings()
