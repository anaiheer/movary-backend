import hashlib
import base64
import time
from typing import Any, Dict, Tuple

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _sign_string(params: Dict[str, Any], key: str) -> str:
    pieces = []
    for k in sorted(params.keys()):
        if k in {"sign", "sign_type"}:
            continue
        v = params.get(k)
        if v is None or v == "":
            continue
        pieces.append(f"{k}={v}")
    return "&".join(pieces) + key


def sign_params(params: Dict[str, Any], key: str) -> str:
    raw = _sign_string(params, key)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_sign(params: Dict[str, Any], key: str) -> bool:
    sign = params.get("sign") or ""
    if not sign:
        return False
    return sign == sign_params(params, key)


def build_pay_url(base_url: str, params: Dict[str, Any], key: str) -> str:
    signed = dict(params)
    signed["sign_type"] = "MD5"
    signed["sign"] = sign_params(signed, key)
    query = "&".join([f"{k}={signed[k]}" for k in signed])
    return f"{base_url.rstrip('/')}/submit.php?{query}"


def normalize_notify(payload: Dict[str, Any]) -> Tuple[str, str]:
    trade_status = payload.get("trade_status") or payload.get("status") or ""
    out_trade_no = payload.get("out_trade_no") or ""
    return trade_status, out_trade_no


def _looks_like_pem(key: str | None) -> bool:
    if not key:
        return False
    return "BEGIN" in key and "KEY" in key


def sign_params_rsa(params: Dict[str, Any], private_key_pem: str) -> str:
    """RSA-SHA256 signature (base64) for API v2 style endpoints.

    Uses the same canonical string rule as MD5: sort keys, skip empty, skip sign/sign_type.
    """
    # Canonical query string without trailing secret key.
    pieces = []
    for k in sorted(params.keys()):
        if k in {"sign", "sign_type"}:
            continue
        v = params.get(k)
        if v is None or v == "":
            continue
        pieces.append(f"{k}={v}")
    raw = "&".join(pieces)

    private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    sig = private_key.sign(raw.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def sign_api_params(params: Dict[str, Any], key: str) -> tuple[str, str]:
    """Return (sign_type, sign) for epay API calls.

    - If key looks like a PEM private key, use RSA (base64).
    - Otherwise use legacy MD5.
    """
    if _looks_like_pem(key):
        return "RSA", sign_params_rsa(params, key)
    return "MD5", sign_params(params, key)


async def epay_api_post(
    gateway: str, path: str, params: Dict[str, Any], key: str, timeout: float = 8.0
) -> dict:
    """POST form-encoded params to epay gateway and return JSON payload (or best-effort parse)."""
    signed = dict(params)
    sign_type, sign = sign_api_params(signed, key)
    signed["sign_type"] = sign_type
    signed["sign"] = sign

    url = f"{gateway.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, data=signed)

    # Many gateways return JSON; some return text. Try both.
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text, "status_code": resp.status_code}


async def epay_close(
    gateway: str, pid: str, key: str, out_trade_no: str | None = None, trade_no: str | None = None
) -> dict:
    payload: Dict[str, Any] = {
        "pid": pid,
        "timestamp": str(int(time.time())),
    }
    if trade_no:
        payload["trade_no"] = trade_no
    if out_trade_no:
        payload["out_trade_no"] = out_trade_no
    return await epay_api_post(gateway, "/api/pay/close", payload, key)


async def epay_refund(
    gateway: str,
    pid: str,
    key: str,
    out_trade_no: str,
    money: str,
    out_refund_no: str,
) -> dict:
    payload: Dict[str, Any] = {
        "pid": pid,
        "out_trade_no": out_trade_no,
        "out_refund_no": out_refund_no,
        "money": money,
        "timestamp": str(int(time.time())),
    }
    return await epay_api_post(gateway, "/api/pay/refund", payload, key)


async def epay_refund_query(
    gateway: str,
    pid: str,
    key: str,
    out_refund_no: str | None = None,
    refund_no: str | None = None,
) -> dict:
    payload: Dict[str, Any] = {
        "pid": pid,
        "timestamp": str(int(time.time())),
    }
    if refund_no:
        payload["refund_no"] = refund_no
    if out_refund_no:
        payload["out_refund_no"] = out_refund_no
    return await epay_api_post(gateway, "/api/pay/refundquery", payload, key)
