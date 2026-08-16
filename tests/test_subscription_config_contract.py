from app.schemas.subscription_config import (
    SubscriptionConfigSummary,
    SubscriptionPlanUpsert,
)


def test_subscription_summary_defaults_match_single_plan_policy() -> None:
    summary = SubscriptionConfigSummary(plan_count=0, visible_plan_count=0, group_count=0)

    assert summary.max_plan_count == 1
    assert summary.locked is False
    assert summary.pro_data_detected is False


def test_subscription_upsert_contract_accepts_monthly_and_optional_yearly_prices() -> None:
    payload = SubscriptionPlanUpsert(
        name="基础订阅",
        description="基础版默认方案",
        monthly_price=29,
        yearly_price=299,
        vod_movie_times=2,
        vod_tv_times=4,
        is_visible=True,
    )

    assert payload.monthly_price == 29
    assert payload.yearly_price == 299
    assert payload.vod_movie_times == 2
