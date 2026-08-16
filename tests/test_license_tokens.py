from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.services.license_tokens import verify_signed_license


def _signed_license(monkeypatch, *, instance_id: str = "instance-1") -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setattr(settings, "MOVARY_LICENSE_PUBLIC_KEY", public_key.decode())
    monkeypatch.setattr(settings, "MOVARY_LICENSE_KEY_ID", "test-key")
    return jwt.encode(
        {
            "edition": "pro",
            "instance_id": instance_id,
            "features": {"pro": True},
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_verify_signed_license_accepts_valid_pyjwt_token(monkeypatch):
    token = _signed_license(monkeypatch)

    payload = verify_signed_license(token, expected_instance_id="instance-1")

    assert payload["edition"] == "pro"
    assert payload["features"]["pro"] is True


def test_verify_signed_license_rejects_wrong_instance(monkeypatch):
    token = _signed_license(monkeypatch, instance_id="instance-2")

    with pytest.raises(ValueError, match="实例不匹配"):
        verify_signed_license(token, expected_instance_id="instance-1")
