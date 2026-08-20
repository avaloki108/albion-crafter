from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from albion_crafter.core.actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
)
from albion_crafter.core.models import Recipe
from albion_crafter.core.provenance import Provenance

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

    @property
    def is_override(self) -> bool:
        return self.provenance is Provenance.USER_OVERRIDE


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
) -> ResolvedPrice:
    """Select one effective price using the shared, fixed-clock trust policy.

    A matching user override always wins. Cached zero values are AODP's missing
    sentinel, not a free price. The selected market side supplies both the value
    and its independent observation timestamp.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
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
        )

    if market_price is None:
        return ResolvedPrice(
            item_id=item_id,
            city=city,
            quality=quality,
            side=side,
            price=None,
            observation_timestamp=None,
            fetched_at=None,
            provenance=Provenance.UNKNOWN,
            freshness=Freshness.UNKNOWN,
            role=role,
        )

    cached_price = market_price.price_for_side(side)
    price = float(cached_price) if cached_price is not None and cached_price > 0 else None
    timestamp = market_price.timestamp_for_side(side) if price is not None else None
    return ResolvedPrice(
        item_id=item_id,
        city=city,
        quality=quality,
        side=side,
        price=price,
        observation_timestamp=timestamp,
        fetched_at=market_price.fetched_at,
        provenance=market_price.provenance,
        freshness=freshness_policy.classify(timestamp, now=as_of),
        role=role,
    )


def price_quality_reasons(line: ResolvedPrice) -> tuple[ActionabilityReason, ...]:
    """Return provenance/freshness blockers for a selected, present price."""
    if line.price is None:
        return ()
    reasons: list[ActionabilityReason] = []
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
                f"{line.item_id} {line.side.value} price is stale.",
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
                f"{line.item_id} {line.side.value} price has no observation timestamp.",
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


class PriceResolver:
    """Resolve an explicit material side and output side without hiding trust state."""

    def __init__(
        self,
        repository: MarketPriceRepository,
        overrides: PriceOverrideRepository | None = None,
    ) -> None:
        self.repository = repository
        self.overrides = overrides

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
        )
