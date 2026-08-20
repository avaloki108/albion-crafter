from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from albion_crafter.core.freshness import DEFAULT_CLOCK_SKEW_TOLERANCE

from .history import HistoryTimeScale, MarketHistoryInterval


class MarketPriceSource(StrEnum):
    USER_OVERRIDE = "USER_OVERRIDE"
    CURRENT = "CURRENT"
    HISTORICAL_ESTIMATE = "HISTORICAL_ESTIMATE"
    MISSING = "MISSING"


class PriceConfidence(StrEnum):
    LIVE = "LIVE"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class HistoricalEstimationPolicy:
    """Tunable, deterministic rules for recent AODP sell-history estimates."""

    price_lookback: timedelta = timedelta(days=7)
    volume_lookback: timedelta = timedelta(days=30)
    high_min_days: int = 5
    medium_min_days: int = 3
    high_min_total_volume: int = 50
    medium_min_total_volume: int = 10
    high_maximum_activity_age: timedelta = timedelta(days=2)
    medium_maximum_activity_age: timedelta = timedelta(days=4)
    high_maximum_volatility: float = 0.25
    medium_maximum_volatility: float = 0.60
    minimum_outlier_deviation: float = 0.50
    outlier_mad_multiplier: float = 3.0
    maximum_daily_weight_multiple: float = 3.0

    def __post_init__(self) -> None:
        if not timedelta(0) < self.price_lookback <= self.volume_lookback:
            raise ValueError("history price/volume lookbacks must be positive and ordered")
        if not 1 <= self.medium_min_days <= self.high_min_days:
            raise ValueError("history day thresholds must be positive and ordered")
        if not 1 <= self.medium_min_total_volume <= self.high_min_total_volume:
            raise ValueError("history volume thresholds must be positive and ordered")
        if not (
            timedelta(0)
            < self.high_maximum_activity_age
            <= self.medium_maximum_activity_age
            <= self.price_lookback
        ):
            raise ValueError("history activity-age thresholds must be positive and ordered")
        if not 0 <= self.high_maximum_volatility <= self.medium_maximum_volatility:
            raise ValueError("history volatility thresholds must be non-negative and ordered")
        if self.minimum_outlier_deviation < 0 or self.outlier_mad_multiplier <= 0:
            raise ValueError("history outlier thresholds must be non-negative")
        if self.maximum_daily_weight_multiple < 1:
            raise ValueError("maximum daily history weight multiple must be at least one")


DEFAULT_HISTORICAL_ESTIMATION_POLICY = HistoricalEstimationPolicy()


@dataclass(frozen=True, slots=True)
class HistoricalPriceEstimate:
    reference_price: float
    confidence: PriceConfidence
    days_used: int
    days_available: int
    total_volume_7d: int
    average_daily_volume_7d: float
    average_daily_volume_30d: float | None
    median_price: float
    volatility: float
    latest_bucket_at: datetime
    fetched_at: datetime
    outliers_ignored: int


@dataclass(frozen=True, slots=True)
class _DailyPoint:
    observed_at: datetime
    price: float
    volume: int
    fetched_at: datetime


def estimate_historical_sell_price(
    intervals: Sequence[MarketHistoryInterval],
    *,
    as_of: datetime,
    policy: HistoricalEstimationPolicy = DEFAULT_HISTORICAL_ESTIMATION_POLICY,
) -> HistoricalPriceEstimate | None:
    """Estimate a sell price with an outlier-resistant, volume-aware median.

    Daily rows are first collapsed by UTC date. Price outliers are rejected using
    deviation from the unweighted median and median absolute deviation. Remaining
    daily volume weights are capped at three times the median daily volume before
    taking a weighted median, so one abnormal price or volume spike cannot dominate.
    """

    if as_of.tzinfo is None:
        raise ValueError("history estimate as_of must be timezone-aware")
    current_time = as_of.astimezone(UTC)
    if intervals:
        _validate_one_series(intervals)
    points = _daily_points(intervals, as_of=current_time, policy=policy)
    price_start = current_time - policy.price_lookback
    recent = [point for point in points if point.observed_at >= price_start]
    if not recent:
        return None

    prices = [point.price for point in recent]
    raw_median = float(statistics.median(prices))
    relative_deviations = [abs(value - raw_median) / raw_median for value in prices]
    median_deviation = float(statistics.median(relative_deviations))
    outlier_limit = max(
        policy.minimum_outlier_deviation,
        median_deviation * policy.outlier_mad_multiplier,
    )
    retained = [
        point
        for point, deviation in zip(recent, relative_deviations, strict=True)
        if deviation <= outlier_limit
    ]
    if not retained:
        return None

    median_price = float(statistics.median(point.price for point in retained))
    median_volume = float(statistics.median(point.volume for point in retained))
    weight_cap = max(median_volume * policy.maximum_daily_weight_multiple, 1.0)
    weighted_points = tuple(
        (point.price, min(float(point.volume), weight_cap)) for point in retained
    )
    reference_price = _weighted_median(weighted_points)
    volatility = _weighted_median(
        tuple(
            (abs(point.price - median_price) / median_price, weight)
            for point, (_, weight) in zip(retained, weighted_points, strict=True)
        )
    )
    total_volume = sum(point.volume for point in retained)
    latest = max(retained, key=lambda point: point.observed_at)
    activity_through = latest.observed_at + timedelta(hours=int(HistoryTimeScale.DAILY))
    activity_age = max(current_time - activity_through, timedelta(0))
    confidence = _confidence(
        days=len(retained),
        volume=total_volume,
        volatility=volatility,
        activity_age=activity_age,
        policy=policy,
    )

    volume_days = max(policy.price_lookback.total_seconds() / 86_400, 1.0)
    volume_30_start = current_time - policy.volume_lookback
    volume_30_points = [point for point in points if point.observed_at >= volume_30_start]
    covered_span = (
        current_time - min(point.observed_at for point in volume_30_points)
        if volume_30_points
        else timedelta(0)
    )
    average_30 = (
        sum(point.volume for point in volume_30_points)
        / max(policy.volume_lookback.total_seconds() / 86_400, 1.0)
        if covered_span >= policy.volume_lookback - timedelta(days=1)
        else None
    )
    return HistoricalPriceEstimate(
        reference_price=reference_price,
        confidence=confidence,
        days_used=len(retained),
        days_available=len(recent),
        total_volume_7d=total_volume,
        average_daily_volume_7d=total_volume / volume_days,
        average_daily_volume_30d=average_30,
        median_price=median_price,
        volatility=volatility,
        latest_bucket_at=latest.observed_at,
        fetched_at=max(point.fetched_at for point in retained),
        outliers_ignored=len(recent) - len(retained),
    )


def _daily_points(
    intervals: Sequence[MarketHistoryInterval],
    *,
    as_of: datetime,
    policy: HistoricalEstimationPolicy,
) -> tuple[_DailyPoint, ...]:
    volume_start = as_of - policy.volume_lookback
    grouped: dict[object, list[MarketHistoryInterval]] = {}
    for interval in intervals:
        observed = interval.observed_at.astimezone(UTC)
        if interval.time_scale is not HistoryTimeScale.DAILY:
            continue
        if observed < volume_start or observed > as_of + DEFAULT_CLOCK_SKEW_TOLERANCE:
            continue
        grouped.setdefault(observed.date(), []).append(interval)

    points: list[_DailyPoint] = []
    for values in grouped.values():
        volume = sum(value.item_count for value in values)
        if volume <= 0:
            continue
        points.append(
            _DailyPoint(
                observed_at=max(value.observed_at.astimezone(UTC) for value in values),
                price=sum(value.average_price * value.item_count for value in values) / volume,
                volume=volume,
                fetched_at=max(value.fetched_at.astimezone(UTC) for value in values),
            )
        )
    return tuple(sorted(points, key=lambda point: point.observed_at))


def _weighted_median(values: Sequence[tuple[float, float]]) -> float:
    if not values:
        raise ValueError("weighted median requires at least one value")
    if any(
        not math.isfinite(value) or value < 0 or not math.isfinite(weight) or weight <= 0
        for value, weight in values
    ):
        raise ValueError("weighted median values must be non-negative and weights positive")
    ordered = sorted(values)
    midpoint = sum(weight for _, weight in ordered) / 2
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= midpoint:
            return float(value)
    return float(ordered[-1][0])


def _confidence(
    *,
    days: int,
    volume: int,
    volatility: float,
    activity_age: timedelta,
    policy: HistoricalEstimationPolicy,
) -> PriceConfidence:
    if (
        days >= policy.high_min_days
        and volume >= policy.high_min_total_volume
        and volatility <= policy.high_maximum_volatility
        and activity_age <= policy.high_maximum_activity_age
    ):
        return PriceConfidence.HIGH
    if (
        days >= policy.medium_min_days
        and volume >= policy.medium_min_total_volume
        and volatility <= policy.medium_maximum_volatility
        and activity_age <= policy.medium_maximum_activity_age
    ):
        return PriceConfidence.MEDIUM
    return PriceConfidence.LOW


def _validate_one_series(intervals: Sequence[MarketHistoryInterval]) -> None:
    identities = {
        (value.region, value.item_id.casefold(), value.city.casefold(), value.quality)
        for value in intervals
    }
    if len(identities) != 1:
        raise ValueError("history estimate intervals must describe one item/city/quality series")
