from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from albion_crafter.core.actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
    ReasonSeverity,
)
from albion_crafter.core.models import Recipe
from albion_crafter.core.provenance import Provenance

from .estimation import (
    DEFAULT_HISTORICAL_ESTIMATION_POLICY,
    HistoricalEstimationPolicy,
    HistoricalPriceEstimate,
    MarketPriceSource,
    PriceConfidence,
    estimate_historical_sell_price,
)
from .history import HistoryTimeScale, MarketHistoryInterval
from .models import (
    Freshness,
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)

if TYPE_CHECKING:
    from albion_crafter.database.database import MarketPriceRepository, PriceOverrideRepository
    from albion_crafter.database.v3 import MarketHistoryRepository

_FRESHNESS_RANK = {
    Freshness.FRESH: 0,
    Freshness.AGING: 1,
    Freshness.STALE: 2,
    Freshness.UNKNOWN: 3,
    Freshness.FUTURE: 4,
}


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    item_id: str
    city: str
    quality: int
    side: MarketSide
    price: float | None
    observation_timestamp: datetime | None
    fetched_at: datetime | None
    provenance: Provenance
    freshness: Freshness
    role: str
    source: MarketPriceSource
    confidence: PriceConfidence
    current_price: float | None = None
    current_timestamp: datetime | None = None
    current_fetched_at: datetime | None = None
    current_freshness: Freshness = Freshness.UNKNOWN
    historical_reference_price: float | None = None
    historical_days_used: int = 0
    historical_days_available: int = 0
    historical_total_volume: int = 0
    historical_avg_daily_volume_7d: float | None = None
    historical_avg_daily_volume_30d: float | None = None
    historical_median_price: float | None = None
    historical_volatility: float | None = None
    historical_latest_bucket: datetime | None = None
    historical_outliers_ignored: int = 0

    @property
    def is_override(self) -> bool:
        return self.provenance is Provenance.USER_OVERRIDE

    @property
    def is_historical_estimate(self) -> bool:
        return self.source is MarketPriceSource.HISTORICAL_ESTIMATE

    @property
    def current_is_stale(self) -> bool:
        return self.current_freshness is Freshness.STALE


def resolve_price(
    *,
    item_id: str,
    city: str,
    quality: int,
    side: MarketSide,
    role: str,
    freshness_policy: FreshnessPolicy,
    as_of: datetime,
    market_price: MarketPrice | None = None,
    override: UserPriceOverride | None = None,
    history: Sequence[MarketHistoryInterval] = (),
    history_policy: HistoricalEstimationPolicy = DEFAULT_HISTORICAL_ESTIMATION_POLICY,
) -> ResolvedPrice:
    """Select one effective price using the shared, fixed-clock trust policy.

    A matching user override always wins. Cached zero values are AODP's missing
    sentinel, not a free price. The selected market side supplies both the value
    and its independent observation timestamp.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    current_price: float | None = None
    current_timestamp: datetime | None = None
    current_fetched_at: datetime | None = None
    current_provenance = Provenance.UNKNOWN
    current_freshness = Freshness.UNKNOWN
    if market_price is not None:
        cached_price = market_price.price_for_side(side)
        current_price = (
            float(cached_price) if cached_price is not None and cached_price > 0 else None
        )
        current_timestamp = (
            market_price.timestamp_for_side(side) if current_price is not None else None
        )
        current_fetched_at = market_price.fetched_at
        current_provenance = market_price.provenance
        current_freshness = freshness_policy.classify(current_timestamp, now=as_of)

    shared = {
        "current_price": current_price,
        "current_timestamp": current_timestamp,
        "current_fetched_at": current_fetched_at,
        "current_freshness": current_freshness,
    }
    if override is not None:
        return ResolvedPrice(
            item_id=item_id,
            city=city,
            quality=quality,
            side=side,
            price=float(override.price),
            observation_timestamp=override.entered_at,
            fetched_at=None,
            provenance=override.provenance,
            freshness=freshness_policy.classify(override.entered_at, now=as_of),
            role=role,
            source=MarketPriceSource.USER_OVERRIDE,
            confidence=PriceConfidence.LIVE,
            **shared,
        )

    # AODP's current endpoint represents the current order-book observation even
    # when that order was first seen long ago. Age remains visible evidence, but
    # it must not turn a real non-zero order into unusable/missing data.
    if current_price is not None:
        return ResolvedPrice(
            item_id=item_id,
            city=city,
            quality=quality,
            side=side,
            price=current_price,
            observation_timestamp=current_timestamp,
            fetched_at=current_fetched_at,
            provenance=current_provenance,
            freshness=current_freshness,
            role=role,
            source=MarketPriceSource.CURRENT,
            confidence=PriceConfidence.LIVE,
            **shared,
        )

    estimate: HistoricalPriceEstimate | None = None
    if side is MarketSide.SELL_ORDER and history:
        estimate = estimate_historical_sell_price(history, as_of=as_of, policy=history_policy)
    if estimate is not None:
        return ResolvedPrice(
            item_id=item_id,
            city=city,
            quality=quality,
            side=side,
            price=estimate.reference_price,
            observation_timestamp=estimate.latest_bucket_at,
            fetched_at=estimate.fetched_at,
            provenance=Provenance.AODP_CACHED,
            freshness=Freshness.AGING,
            role=role,
            source=MarketPriceSource.HISTORICAL_ESTIMATE,
            confidence=estimate.confidence,
            historical_reference_price=estimate.reference_price,
            historical_days_used=estimate.days_used,
            historical_days_available=estimate.days_available,
            historical_total_volume=estimate.total_volume_7d,
            historical_avg_daily_volume_7d=estimate.average_daily_volume_7d,
            historical_avg_daily_volume_30d=estimate.average_daily_volume_30d,
            historical_median_price=estimate.median_price,
            historical_volatility=estimate.volatility,
            historical_latest_bucket=estimate.latest_bucket_at,
            historical_outliers_ignored=estimate.outliers_ignored,
            **shared,
        )

    return ResolvedPrice(
        item_id=item_id,
        city=city,
        quality=quality,
        side=side,
        price=None,
        observation_timestamp=None,
        fetched_at=current_fetched_at,
        provenance=Provenance.UNKNOWN,
        freshness=current_freshness,
        role=role,
        source=MarketPriceSource.MISSING,
        confidence=PriceConfidence.MISSING,
        **shared,
    )


def price_quality_reasons(line: ResolvedPrice) -> tuple[ActionabilityReason, ...]:
    """Return provenance blockers and advisory timestamp diagnostics."""
    reasons: list[ActionabilityReason] = []
    if line.source is MarketPriceSource.HISTORICAL_ESTIMATE:
        reasons.append(
            ActionabilityReason(
                ReasonCode.HISTORICAL_PRICE_ESTIMATE,
                f"{line.item_id} {line.role} uses a {line.confidence.value} confidence "
                f"AODP historical sell estimate from {line.historical_days_used} recent day(s) "
                f"and {line.historical_total_volume:,} reported item(s).",
                ReasonSeverity.WARNING,
            )
        )
        return tuple(reasons)
    if line.price is None:
        return tuple(reasons)
    if not line.provenance.is_actionable_price_source:
        reasons.append(
            ActionabilityReason(
                ReasonCode.UNTRUSTED_PROVENANCE,
                f"{line.item_id} {line.role} price uses {line.provenance.value} data.",
            )
        )
    if line.freshness is Freshness.STALE:
        reasons.append(
            ActionabilityReason(
                ReasonCode.STALE_PRICE,
                f"{line.item_id} {line.side.value} uses the latest available current order; "
                "its observation timestamp is older than the preferred age.",
                ReasonSeverity.WARNING,
            )
        )
    elif line.freshness is Freshness.FUTURE:
        reasons.append(
            ActionabilityReason(
                ReasonCode.FUTURE_TIMESTAMP,
                f"{line.item_id} {line.side.value} price is materially future-dated.",
            )
        )
    elif line.freshness is Freshness.UNKNOWN:
        reasons.append(
            ActionabilityReason(
                ReasonCode.UNKNOWN_TIMESTAMP,
                f"{line.item_id} {line.side.value} uses the latest available current order; "
                "it has no observation timestamp.",
                ReasonSeverity.WARNING,
            )
        )
    return tuple(reasons)


class _HasFreshness(Protocol):
    freshness: Freshness


def worst_freshness(lines: Sequence[_HasFreshness]) -> Freshness:
    """Return the most conservative state across required price lines."""
    return (
        max((line.freshness for line in lines), key=_FRESHNESS_RANK.__getitem__)
        if lines
        else Freshness.UNKNOWN
    )


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    material_prices: dict[str, float | None]
    output_price: float | None
    resolved_prices: tuple[ResolvedPrice, ...]
    freshness: Freshness
    oldest_timestamp: datetime | None
    actionability: ActionabilityAssessment
    material_side: MarketSide
    output_side: MarketSide
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")

    @property
    def age_seconds(self) -> float | None:
        if self.oldest_timestamp is None:
            return None
        return (self.as_of - self.oldest_timestamp).total_seconds()

    @property
    def has_sample_data(self) -> bool:
        return any(price.provenance is Provenance.DEMO_SAMPLE for price in self.resolved_prices)

    @property
    def historical_estimate_count(self) -> int:
        return sum(line.is_historical_estimate for line in self.resolved_prices)

    @property
    def live_price_count(self) -> int:
        return sum(line.source is MarketPriceSource.CURRENT for line in self.resolved_prices)


class PriceResolver:
    """Resolve an explicit material side and output side without hiding trust state."""

    def __init__(
        self,
        repository: MarketPriceRepository,
        overrides: PriceOverrideRepository | None = None,
        history: MarketHistoryRepository | None = None,
        *,
        history_policy: HistoricalEstimationPolicy = DEFAULT_HISTORICAL_ESTIMATION_POLICY,
    ) -> None:
        self.repository = repository
        self.overrides = overrides
        self.history = history
        self.history_policy = history_policy

    def resolve(
        self,
        recipe: Recipe,
        *,
        buy_city: str,
        sell_city: str,
        region: Region,
        quality: int,
        freshness_policy: FreshnessPolicy,
        material_side: MarketSide = MarketSide.SELL_ORDER,
        output_side: MarketSide = MarketSide.SELL_ORDER,
        as_of: datetime | None = None,
    ) -> PricingSnapshot:
        resolution_time = as_of or datetime.now(UTC)
        if resolution_time.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        material_prices: dict[str, float | None] = {}
        resolved: list[ResolvedPrice] = []

        for requirement in recipe.materials:
            line = self._resolve_one(
                requirement.item_id,
                buy_city,
                1,
                region,
                material_side,
                freshness_policy,
                resolution_time,
                role="material",
            )
            material_prices[requirement.item_id] = line.price
            resolved.append(line)

        output = self._resolve_one(
            recipe.output.item_id,
            sell_city,
            quality,
            region,
            output_side,
            freshness_policy,
            resolution_time,
            role="output",
        )
        resolved.append(output)
        timestamps = [
            line.observation_timestamp
            for line in resolved
            if line.observation_timestamp is not None
        ]
        reasons = [reason for line in resolved for reason in price_quality_reasons(line)]
        return PricingSnapshot(
            material_prices=material_prices,
            output_price=output.price,
            resolved_prices=tuple(resolved),
            freshness=worst_freshness(resolved),
            oldest_timestamp=(
                min(timestamps) if resolved and len(timestamps) == len(resolved) else None
            ),
            actionability=ActionabilityAssessment(tuple(reasons)),
            material_side=material_side,
            output_side=output_side,
            as_of=resolution_time,
        )

    def resolve_item(
        self,
        item_id: str,
        *,
        city: str,
        quality: int,
        region: Region,
        side: MarketSide,
        freshness_policy: FreshnessPolicy,
        as_of: datetime | None = None,
        role: str = "item",
    ) -> ResolvedPrice:
        """Resolve one standalone market item using the shared trust policy.

        Loadouts and shopping lists are not recipes, but they must still follow
        the same override/current/history/missing rules as production pricing.
        """

        resolution_time = as_of or datetime.now(UTC)
        if resolution_time.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return self._resolve_one(
            item_id,
            city,
            quality,
            region,
            side,
            freshness_policy,
            resolution_time,
            role=role,
        )

    def _resolve_one(
        self,
        item_id: str,
        city: str,
        quality: int,
        region: Region,
        side: MarketSide,
        freshness_policy: FreshnessPolicy,
        as_of: datetime,
        *,
        role: str,
    ) -> ResolvedPrice:
        override = (
            self.overrides.get(item_id, city, quality, region, side)
            if self.overrides is not None
            else None
        )
        record = self.repository.get(item_id, city, quality, region)
        history = (
            self.history.list_for_items(
                region,
                (item_id,),
                (city,),
                quality,
                as_of - self.history_policy.volume_lookback,
                time_scale=HistoryTimeScale.DAILY,
            )
            if self.history is not None and side is MarketSide.SELL_ORDER
            else ()
        )
        return resolve_price(
            item_id=item_id,
            city=city,
            quality=quality,
            side=side,
            role=role,
            freshness_policy=freshness_policy,
            as_of=as_of,
            market_price=record,
            override=override,
            history=history,
            history_policy=self.history_policy,
        )
