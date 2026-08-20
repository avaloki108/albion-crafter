from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.provenance import Provenance
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.liquidity import LiquidityLevel, assess_liquidity
from albion_crafter.market.models import Region

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _interval(
    days_ago: int,
    *,
    count: int = 20,
    price: float = 100,
    item_id: str = "T4_BAG",
    fetched_at: datetime = NOW,
) -> MarketHistoryInterval:
    return MarketHistoryInterval(
        item_id=item_id,
        city="Bridgewatch",
        quality=1,
        region=Region.AMERICAS,
        observed_at=NOW - timedelta(days=days_ago),
        item_count=count,
        average_price=price,
        time_scale=HistoryTimeScale.DAILY,
        fetched_at=fetched_at,
        provenance=Provenance.AODP_CACHED,
    )


def test_no_history_is_unknown_and_does_not_invent_volume() -> None:
    assessment = assess_liquidity((), current_price=100, now=NOW, history_available=False)
    assert assessment.level is LiquidityLevel.UNKNOWN
    assert assessment.reported_volume is None
    assert assessment.active_intervals is None
    assert assessment.weighted_mean_price is None
    assert assessment.current_price_deviation is None
    assert not assessment.has_history_metrics


def test_successful_empty_history_is_unknown_not_claimed_real_zero_liquidity() -> None:
    assessment = assess_liquidity((), current_price=100, now=NOW, history_available=True)
    assert assessment.level is LiquidityLevel.UNKNOWN
    assert assessment.reported_volume == 0
    assert assessment.active_intervals == 0
    assert "does not establish zero real trading" in assessment.reasons[0]


def test_future_history_is_ignored_but_small_clock_skew_is_tolerated() -> None:
    future = replace(_interval(0), observed_at=NOW + timedelta(minutes=5))
    invalid = assess_liquidity((future,), current_price=100, now=NOW)
    tolerated = assess_liquidity(
        (replace(_interval(0), observed_at=NOW + timedelta(minutes=2)),),
        current_price=100,
        now=NOW,
    )

    assert invalid.level is LiquidityLevel.UNKNOWN
    assert invalid.reported_volume == 0
    assert "future-dated" in invalid.reasons[0]
    assert tolerated.reported_volume == 20
    assert tolerated.active_intervals == 1


def test_high_liquidity_exposes_weighted_metrics_and_signed_deviation() -> None:
    intervals = tuple(
        _interval(days_ago, count=20, price=price)
        for days_ago, price in zip(range(1, 6), (100, 101, 102, 103, 104), strict=True)
    )
    assessment = assess_liquidity(intervals, current_price=105, now=NOW)

    assert assessment.level is LiquidityLevel.HIGH
    assert assessment.reported_volume == 100
    assert assessment.active_intervals == 5
    assert assessment.weighted_mean_price == pytest.approx(102)
    assert assessment.current_price_deviation == pytest.approx((105 - 102) / 102)
    assert assessment.minimum_interval_average == 100
    assert assessment.maximum_interval_average == 104
    assert any("High-liquidity" in reason for reason in assessment.reasons)


def test_moderate_liquidity_is_explainable() -> None:
    intervals = tuple(_interval(day, count=10, price=100) for day in range(1, 6))
    assessment = assess_liquidity(intervals, current_price=110, now=NOW)
    assert assessment.level is LiquidityLevel.MODERATE
    assert assessment.reported_volume == 50
    assert assessment.active_intervals == 5
    assert any("does not meet every High" in reason for reason in assessment.reasons)


def test_low_reported_activity_and_extreme_price_are_low_with_reasons() -> None:
    low_activity = assess_liquidity([_interval(1, count=3)], current_price=100, now=NOW)
    assert low_activity.level is LiquidityLevel.LOW
    assert any("10-item" in reason for reason in low_activity.reasons)
    assert any("fewer than 2 intervals" in reason for reason in low_activity.reasons)

    price_outlier = assess_liquidity(
        [_interval(day, count=20, price=100) for day in range(1, 6)],
        current_price=175,
        now=NOW,
    )
    assert price_outlier.level is LiquidityLevel.LOW
    assert price_outlier.current_price_deviation == pytest.approx(0.75)
    assert any("more than 50%" in reason for reason in price_outlier.reasons)


def test_incomplete_history_is_unknown_but_retains_underlying_numbers() -> None:
    assessment = assess_liquidity(
        [_interval(day, count=20, price=100) for day in range(1, 6)],
        current_price=100,
        now=NOW,
        history_complete=False,
    )
    assert assessment.level is LiquidityLevel.UNKNOWN
    assert assessment.reported_volume == 100
    assert assessment.active_intervals == 5
    assert assessment.weighted_mean_price == 100
    assert any("incomplete" in reason for reason in assessment.reasons)


def test_duplicate_bucket_uses_latest_fetch_instead_of_double_counting() -> None:
    old_fetch = NOW - timedelta(hours=1)
    intervals = [
        _interval(1, count=10, price=100, fetched_at=old_fetch),
        _interval(1, count=25, price=120, fetched_at=NOW),
        _interval(2, count=25, price=120, fetched_at=NOW),
    ]
    assessment = assess_liquidity(intervals, current_price=120, now=NOW)
    assert assessment.reported_volume == 50
    assert assessment.active_intervals == 2
    assert assessment.weighted_mean_price == 120


def test_liquidity_refuses_to_mix_different_market_series() -> None:
    with pytest.raises(ValueError, match="one item/city/quality/time-scale series"):
        assess_liquidity(
            [_interval(1), _interval(2, item_id="T5_BAG")],
            current_price=100,
            now=NOW,
        )


def test_out_of_window_history_is_unknown() -> None:
    assessment = assess_liquidity([_interval(20)], current_price=100, now=NOW)
    assert assessment.level is LiquidityLevel.UNKNOWN
    assert assessment.reported_volume == 0
