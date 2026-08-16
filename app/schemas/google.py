from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class GoogleCredentialRequest(BaseModel):
    credential: str = Field(min_length=1)

    @field_validator("credential", mode="before")
    @classmethod
    def _strip_credential(cls, value: str) -> str:
        return str(value or "").strip()


class GoogleBindingOut(BaseModel):
    provider: str
    provider_user_id: str
    provider_email: str | None = None
    provider_name: str | None = None
    provider_avatar_url: str | None = None
    is_active: bool
    bound_at: datetime
    last_interaction_at: datetime | None = None

    class Config:
        from_attributes = True
