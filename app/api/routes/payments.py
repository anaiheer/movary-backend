from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.order import Order
from app.models.system_settings import SystemSettings
from app.services.epay import verify_sign
from app.services.payments import handle_paid_order
from app.services.telegram import create_telegram_notification, telegram_notification_exists

router = APIRouter(prefix="/pay", tags=["payments"])


async def _get_settings(db: AsyncSession) -> SystemSettings:
    row = (await db.execute(select(SystemSettings))).scalar()
    if row:
        return row
    row = SystemSettings()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/config")
async def pay_config(db: AsyncSession = Depends(get_db)):
    settings_row = await _get_settings(db)
    epay_ready = bool(
        settings_row.epay_enabled
        and settings_row.epay_gateway
        and settings_row.epay_merchant_id
        and settings_row.epay_key
    )
    return {
        "epay_enabled": bool(settings_row.epay_enabled),
        "epay_ready": epay_ready,
        "supported_methods": ["alipay", "wxpay"] if epay_ready else [],
        "balance_enabled": True,
        "refund_enabled": bool(getattr(settings_row, "refund_enabled", False)),
        "refund_policy": {
            "window_days": int(getattr(settings_row, "refund_window_days", 0) or 0),
            "forbid_if_vod_used": bool(getattr(settings_row, "refund_forbid_if_vod_used", False)),
            "vod_used_threshold": int(getattr(settings_row, "refund_vod_used_threshold", 0) or 0),
            "monthly_limit": int(getattr(settings_row, "refund_user_monthly_limit", 0) or 0),
            "monthly_window_days": int(
                getattr(settings_row, "refund_user_monthly_window_days", 30) or 30
            ),
        },
    }


@router.api_route("/epay/notify", methods=["GET", "POST"], response_class=PlainTextResponse)
async def epay_notify(request: Request, db: AsyncSession = Depends(get_db)):
    settings_row = await _get_settings(db)
    if not settings_row.epay_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="易支付已禁用")
    if not settings_row.epay_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="易支付未配置")

    if request.method == "POST":
        form = await request.form()
        payload = dict(form)
    else:
        payload = dict(request.query_params)

    if not verify_sign(payload, settings_row.epay_key):
        return PlainTextResponse("fail")

    trade_status = payload.get("trade_status") or payload.get("status") or ""
    out_trade_no = payload.get("out_trade_no") or ""
    trade_no = payload.get("trade_no") or payload.get("transaction_id") or None

    if not out_trade_no:
        return PlainTextResponse("fail")

    order = (await db.execute(select(Order).where(Order.order_no == out_trade_no))).scalar()
    if not order:
        return PlainTextResponse("fail")
    if order.pay_provider != "EPAY":
        return PlainTextResponse("fail")

    if trade_status in {"TRADE_FAILED", "FAILED", "TRADE_CLOSED", "CLOSED"}:
        if not await telegram_notification_exists(
            db,
            user_id=order.user_id,
            notification_type="payment_failed",
            reference_id=str(order.id),
        ):
            await create_telegram_notification(
                db,
                user_id=order.user_id,
                notification_type="payment_failed",
                title="支付失败",
                content=f"订单{order.order_no}支付失败",
                reference_id=str(order.id),
            )
            await db.commit()
        return PlainTextResponse("fail")

    if trade_status not in {"TRADE_SUCCESS", "SUCCESS"}:
        return PlainTextResponse("fail")

    try:
        callback_amount = Decimal(str(payload.get("money") or payload.get("total_amount") or "0"))
        order_amount = Decimal(str(order.amount or 0)).quantize(Decimal("0.01"))
        callback_amount = callback_amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return PlainTextResponse("fail")
    if callback_amount != order_amount:
        return PlainTextResponse("fail")
    if payload.get("pid") and str(payload.get("pid")) != str(settings_row.epay_merchant_id):
        return PlainTextResponse("fail")

    await handle_paid_order(order, trade_no, payload, db)
    return PlainTextResponse("success")


@router.api_route("/epay/return", methods=["GET", "POST"])
async def epay_return(request: Request, db: AsyncSession = Depends(get_db)):
    settings_row = await _get_settings(db)
    if not settings_row.epay_enabled or not settings_row.epay_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="易支付已禁用")

    if request.method == "POST":
        form = await request.form()
        payload = dict(form)
    else:
        payload = dict(request.query_params)

    if not verify_sign(payload, settings_row.epay_key):
        return {"success": False, "message": "签名校验失败"}

    trade_status = payload.get("trade_status") or payload.get("status") or ""
    out_trade_no = payload.get("out_trade_no") or ""
    trade_no = payload.get("trade_no") or payload.get("transaction_id") or None

    if not out_trade_no or trade_status not in {"TRADE_SUCCESS", "SUCCESS"}:
        return {"success": False, "message": "支付未完成"}

    order = (await db.execute(select(Order).where(Order.order_no == out_trade_no))).scalar()
    if not order:
        return {"success": False, "message": "订单不存在"}
    if order.pay_provider != "EPAY":
        return {"success": False, "message": "订单支付方式不匹配"}

    try:
        callback_amount = Decimal(str(payload.get("money") or payload.get("total_amount") or "0"))
        order_amount = Decimal(str(order.amount or 0)).quantize(Decimal("0.01"))
        callback_amount = callback_amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return {"success": False, "message": "支付金额无效"}
    if callback_amount != order_amount:
        return {"success": False, "message": "支付金额不匹配"}
    if payload.get("pid") and str(payload.get("pid")) != str(settings_row.epay_merchant_id):
        return {"success": False, "message": "商户号不匹配"}

    await handle_paid_order(order, trade_no, payload, db)
    return {"success": True, "message": "支付成功"}
