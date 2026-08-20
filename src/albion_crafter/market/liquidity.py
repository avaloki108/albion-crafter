from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from albion_crafter.core.freshness import (
    DEFAULT_CLOCK_SKEW_TOLERANCE,
    future_offset_beyond_tolerance,
)

from .history import MarketHistoryInterval


class LiquidityLevel(StrEnum):
    HIGH = "High"
    MODERATE = "Moderate"
    LOW = "Low"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class LiquidityPolicy:
    """Explainable, deliberately conservative reported-activity thresholds."""

    lookback: timedelta = timedelta(days=7)
    moderate_min_volume: int = 10
    high_min_volume: int = 100
    moderate_min_active_intervals: int = 2
    high_min_active_intervals: int = 5
    maximum_activity_age: timedelta = timedelta(days=3)
    high_maximum_activity_age: timedelta = timedelta(days=2)
    high_maximum_absolute_deviation: float = 0.25
    low_absolute_deviation: float = 0.50

    def __post_init__(self) -> None:
        if self.lookback <= timedelta(0):
            raise ValueError("liquidity lookback must be positive")
        if not 0 < self.moderate_min_volume <= self.high_min_volume:
            raise ValueError("liquidity volume thresholds must be positive and ordered")
        if not 0 < self.moderate_min_active_intervals <= self.high_min_active_intervals:
            raise ValueError("active-interval thresholds must be positive and ordered")
        if not timedelta(0) < self.high_maximum_activity_age <= self.maximum_activity_age:
            raise ValueError("liquidity activity ages must be positive and ordered")
        if (
            not math.isfinite(self.high_maximum_absolute_deviation)
            or not math.isfinite(self.low_absolute_deviation)
            or not 0 <= self.high_maximum_absolute_deviation < self.low_absolute_deviation
        ):
            raise ValueError("liquidity deviation thresholds must be non-negative and ordered")


@dataclass(frozen=True, slots=True)
class LiquidityAssessment:
    level: LiquidityLevel
    reported_volume: int | None
    active_intervals: int | None
    weighted_mean_price: float | None
    current_price_deviation: float | None
    current_price: float | None
    last_activity_at: datetime | None
    minimum_interval_average: float | None
    maximum_interval_average: float | None
    reasons: tuple[str, ...]

    @property
    def has_history_metrics(self) -> bool:
        return self.reported_volume is not None


DEFAULT_LIQUIDITY_POLICY = LiquidityPolicy()


def assess_liquidity(
    intervals: Sequence[MarketHistoryInterval],
    *,
    current_price: float | None,
    now: datetime | None = None,
    policy: LiquidityPolicy = DEFAULT_LIQUIDITY_POLICY,
    history_available: bool = True,
    history_complete: bool = True,
) -> LiquidityAssessment:
    """Classify reported history without treating it as executable order-book depth.

    The signed ``current_price_deviation`` is ``(current - weighted_mean) / weighted_mean``.
    A successful empty history response remains Unknown: AODP non-reporting is not proof of zero
    actual trading.
    """
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("liquidity assessment time must be timezone-aware")
    current_time = current_time.astimezone(UTC)
    if current_price is not None and (
        isinstance(current_price, bool)
        or not isinstance(current_price, (int, float))
        or not math.isfinite(current_price)
        or current_price <= 0
    ):
        raise ValueError("current_price must be a finite positive number when supplied")

    if intervals:
        _validate_one_series(intervals)
    window_start = current_time - policy.lookback
    deduplicated = _deduplicate_intervals(intervals)
    future_interval_count = sum(
        future_offset_beyond_tolerance(interval.observed_at, now=current_time) is not None
        for interval in deduplicated
    )
    recent = [
        interval
        for interval in deduplicated
        if future_offset_beyond_tolerance(interval.observed_at, now=current_time) is None
        and window_start
        <= interval.observed_at.astimezone(UTC)
        <= current_time + DEFAULT_CLOCK_SKEW_TOLERANCE
    ]
    if not recent:
        if not history_available:
            message = "Historical market activity was not available."
            volume: int | None = None
            active: int | None = None
        elif future_interval_count:
            message = (
                f"{future_interval_count:,} reported history interval(s) were materially "
                "future-dated and ignored."
            )
            volume = 0
            active = 0
        else:
            message = (
                "No reported AODP activity was returned in the lookback window; "
                "this does not establish zero real trading."
            )
            volume = 0
            active = 0
        return LiquidityAssessment(
            level=LiquidityLevel.UNKNOWN,
            reported_volume=volume,
            active_intervals=active,
            weighted_mean_price=None,
            current_price_deviation=None,
            current_price=current_price,
            last_activity_at=None,
            minimum_interval_average=None,
            maximum_interval_average=None,
            reasons=(message,),
        )

    reported_volume = sum(interval.item_count for interval in recent)
    active_intervals = len(recent)
    weighted_mean = (
        sum(interval.average_price * interval.item_count for interval in recent) / reported_volume
    )
    minimum_average = min(interval.average_price for interval in recent)
    maximum_average = max(interval.average_price for interval in recent)
    last_interval = max(recent, key=lambda interval: interval.observed_at)
    last_activity_at = last_interval.observed_at.astimezone(UTC)
    activity_evidence_through = last_activity_at + timedelta(hours=int(last_interval.time_scale))
    activity_age = max(current_time - activity_evidence_through, timedelta(0))
    deviation = (
        None
        if current_price is None or weighted_mean <= 0
        else (current_price - weighted_mean) / weighted_mean
    )

    reasons = [
        f"Reported volume is {reported_volume:,} items across "
        f"{active_intervals:,} active intervals in the lookback window.",
        f"Reported-volume-weighted mean price is {weighted_mean:,.2f} silver.",
    ]
    if future_interval_count:
        reasons.append(
            f"{future_interval_count:,} materially future-dated history interval(s) were ignored."
        )
    if deviation is None:
        reasons.append("No valid current price was available for a history comparison.")
    else:
        direction = "above" if deviation >= 0 else "below"
        reasons.append(
            f"Current top-of-book price is {abs(deviation):.1%} {direction} "
            "the reported-history weighted mean."
        )

    if not history_complete:
        reasons.append("History coverage was incomplete, so liquidity remains Unknown.")
        level = LiquidityLevel.UNKNOWN
    else:
        low_reasons: list[str] = []
        if reported_volume < policy.moderate_min_volume:
            low_reasons.append(
                f"Reported volume is below the {policy.moderate_min_volume:,}-item "
                "Moderate threshold."
            )
        if active_intervals < policy.moderate_min_active_intervals:
            low_reasons.append(
                f"Activity appears in fewer than {policy.moderate_min_active_intervals} intervals."
            )
        if activity_age > policy.maximum_activity_age:
            low_reasons.append(
                f"Last reported activity is older than "
                f"{_duration_text(policy.maximum_activity_age)}."
            )
        if deviation is not None and abs(deviation) > policy.low_absolute_deviation:
            low_reasons.append(
                f"Current price differs from the weighted mean by more than "
                f"{policy.low_absolute_deviation:.0%}."
            )

        if low_reasons:
            level = LiquidityLevel.LOW
            reasons.extend(low_reasons)
        elif (
            reported_volume >= policy.high_min_volume
            and active_intervals >= policy.high_min_active_intervals
            and activity_age <= policy.high_maximum_activity_age
            and deviation is not None
            and abs(deviation) <= policy.high_maximum_absolute_deviation
        ):
            level = LiquidityLevel.HIGH
            reasons.append("Reported activity meets every High-liquidity threshold.")
        else:
            level = LiquidityLevel.MODERATE
            reasons.append(
                "Reported activity clears Low conditions but does not meet every High threshold."
            )

    return LiquidityAssessment(
        level=level,
        reported_volume=reported_volume,
        active_intervals=active_intervals,
        weighted_mean_price=weighted_mean,
        current_price_deviation=deviation,
        current_price=current_price,
        last_activity_at=last_activity_at,
        minimum_interval_average=minimum_average,
        maximum_interval_average=maximum_average,
        reasons=tuple(reasons),
    )


def _validate_one_series(intervals: Sequence[MarketHistoryInterval]) -> None:
    identities = {
        (
            interval.region,
            interval.item_id,
            interval.city,
            interval.quality,
            interval.time_scale,
        )
        for interval in intervals
    }
    if len(identities) != 1:
        raise ValueError(
            "liquidity intervals must describe one item/city/quality/time-scale series"
        )


def _deduplicate_intervals(
    intervals: Sequence[MarketHistoryInterval],
) -> tuple[MarketHistoryInterval, ...]:
    by_bucket: dict[tuple[datetime, int], MarketHistoryInterval] = {}
    for interval in intervals:
        key = (interval.observed_at.astimezone(UTC), int(interval.time_scale))
        existing = by_bucket.get(key)
        if existing is None or interval.fetched_at > existing.fetched_at:
            by_bucket[key] = interval
    return tuple(by_bucket.values())


def _duration_text(value: timedelta) -> str:
    hours = value.total_seconds() / 3600
    return f"{hours / 24:g} days" if hours % 24 == 0 else f"{hours:g} hours"
