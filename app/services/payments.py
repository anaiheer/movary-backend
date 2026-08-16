from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import BalanceTransaction
from app.models.order import Order, OrderStatus, OrderType, PaymentTransaction
from app.models.subscription import (
    BillingCycle,
    Plan,
    Subscription,
    SubscriptionSource,
    SubscriptionStatus,
)
from app.models.user import User
from app.services.emby_accounts import ensure_emby_accounts_for_user
from app.services.order_chains import apply_successful_order_chain_updates
from app.services.plan_purchase import get_cycle_end_at, resolve_billing
from app.services.subscriptions import sync_user_subscription_entitlements
from app.services.telegram import create_telegram_notification, format_notification_amount


async def handle_paid_order(
    order: Order,
    provider_trade_no: str | None,
    raw: dict | None,
    db: AsyncSession,
    *,
    commit: bool = True,
) -> None:
    if order.status in {OrderStatus.PAID, OrderStatus.COMPLETED}:
        return

    payload = order.pay_payload or {}
    if order.type == OrderType.RECHARGE and (
        order.pay_provider == "BALANCE" or str(payload.get("pay_type") or "").lower() == "balance"
    ):
        raise ValueError("充值订单不支持余额支付")

    now = datetime.utcnow()
    order.status = OrderStatus.PAID
    order.paid_at = now

    if order.type == OrderType.PLAN and order.plan_id:
        plan_stmt = select(Plan).where(Plan.id == order.plan_id)
        plan = (await db.execute(plan_stmt)).scalar()
        if plan:
            purchase_action = str(payload.get("purchase_action") or "DIRECT_PURCHASE").upper()
            cycle_raw = payload.get("billing_cycle") or None
            if cycle_raw == BillingCycle.UNSET.value:
                cycle_raw = None
            start_at = now
            renewal_target: Subscription | None = None
            if purchase_action == "RENEW":
                anchor_id = payload.get("renewal_of_subscription_id") or payload.get(
                    "source_subscription_id"
                )
                anchor_subscription = None
                if anchor_id:
                    try:
                        anchor_subscription = await db.get(Subscription, UUID(str(anchor_id)))
                    except (TypeError, ValueError, AttributeError):
                        anchor_subscription = None
                if (
                    anchor_subscription
                    and anchor_subscription.user_id == order.user_id
                    and anchor_subscription.plan_id == plan.id
                    and anchor_subscription.status == SubscriptionStatus.ACTIVE
                    and anchor_subscription.end_at > now
                ):
                    renewal_target = anchor_subscription
                    start_at = anchor_subscription.end_at
            elif purchase_action in {"UPGRADE", "REPLACE_TRIAL"}:
                raw_source_ids = payload.get("source_subscription_ids") or []
                if not raw_source_ids and payload.get("source_subscription_id"):
                    raw_source_ids = [payload.get("source_subscription_id")]
                for source_id in raw_source_ids:
                    source_subscription = None
                    if source_id:
                        try:
                            source_subscription = await db.get(Subscription, UUID(str(source_id)))
                        except (TypeError, ValueError, AttributeError):
                            source_subscription = None
                    if source_subscription:
                        source_subscription.status = SubscriptionStatus.CANCELED
                        cancel_at = (
                            now
                            if source_subscription.start_at <= now
                            else source_subscription.start_at
                        )
                        if source_subscription.end_at > cancel_at:
                            source_subscription.end_at = cancel_at
                        db.add(source_subscription)

            cycle, _, _ = await resolve_billing(db, plan, cycle_raw, anchor_time=start_at)
            end_at = get_cycle_end_at(
                start_at,
                cycle,
                trial_days=int(plan.trial_days or 0),
                custom_days=int(plan.duration_days or 0),
            )

            if renewal_target:
                subscription = renewal_target
                subscription.billing_cycle = cycle
                subscription.end_at = end_at
                subscription.auto_renew = False
                subscription.source = SubscriptionSource.PAYMENT
                db.add(subscription)
            else:
                subscription = Subscription(
                    user_id=order.user_id,
                    plan_id=plan.id,
                    status=SubscriptionStatus.ACTIVE,
                    billing_cycle=cycle,
                    start_at=start_at,
                    end_at=end_at,
                    auto_renew=False,
                    source=SubscriptionSource.PAYMENT,
                )
                db.add(subscription)
                await db.flush()

            user = (await db.execute(select(User).where(User.id == order.user_id))).scalar()
            if user:
                await ensure_emby_accounts_for_user(db, user, plan.id, None)
                await sync_user_subscription_entitlements(db, user, now=now)

            # Link order to the created subscription for refund/VOD attribution.
            try:
                order.subscription_id = subscription.id
            except Exception:
                pass
            order.pay_payload = {
                **(order.pay_payload or {}),
                "subscription_id": str(subscription.id),
            }

            order.status = OrderStatus.COMPLETED

            await create_telegram_notification(
                db,
                user_id=order.user_id,
                notification_type="payment_success",
                title="支付成功",
                content=f"订单{order.order_no}支付成功，金额{format_notification_amount(order.amount)}",
                reference_id=str(order.id),
            )
            await create_telegram_notification(
                db,
                user_id=order.user_id,
                notification_type="subscription_activated",
                title="订阅已激活",
                content=f"您的{plan.name}已激活，有效期至{end_at.strftime('%Y-%m-%d')}",
                reference_id=str(subscription.id),
            )

            carry_balance_amount = Decimal(str(payload.get("carry_balance_amount") or 0))
            if carry_balance_amount > 0 and user:
                before = Decimal(str(user.balance or 0))
                after = before + carry_balance_amount
                user.balance = after
                db.add(user)
                db.add(
                    BalanceTransaction(
                        user_id=user.id,
                        operator_user_id=user.id,
                        delta=carry_balance_amount,
                        before_balance=before,
                        after_balance=after,
                        reason="PLAN_UPGRADE_CREDIT",
                    )
                )
            await apply_successful_order_chain_updates(db, order)

    if order.type == OrderType.RECHARGE:
        user = (await db.execute(select(User).where(User.id == order.user_id))).scalar()
        if user:
            before = float(user.balance or 0)
            after = before + float(order.amount or 0)
            user.balance = after
            db.add(user)
            db.add(
                BalanceTransaction(
                    user_id=user.id,
                    operator_user_id=user.id,
                    delta=float(order.amount or 0),
                    before_balance=before,
                    after_balance=after,
                    reason="RECHARGE",
                )
            )
            order.status = OrderStatus.COMPLETED
            await create_telegram_notification(
                db,
                user_id=user.id,
                notification_type="payment_success",
                title="支付成功",
                content=f"订单{order.order_no}支付成功，金额{format_notification_amount(order.amount)}",
                reference_id=str(order.id),
            )
            await apply_successful_order_chain_updates(db, order)

    if provider_trade_no:
        db.add(
            PaymentTransaction(
                order_id=order.id,
                provider_trade_no=provider_trade_no,
                status=order.status.value,
                raw_callback=raw or None,
            )
        )

    db.add(order)
    if commit:
        await db.commit()
