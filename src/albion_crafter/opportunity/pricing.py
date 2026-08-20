from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from albion_crafter.core.actionability import ActionabilityReason
from albion_crafter.core.models import Recipe
from albion_crafter.market.history import MarketHistoryInterval
from albion_crafter.market.models import (
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)
from albion_crafter.market.pricing import (
    ResolvedPrice,
    price_quality_reasons,
    resolve_price,
    worst_freshness,
)

from .models import OpportunityPricingSnapshot, PriceEvidence

MarketKey = tuple[Region, str, str, int]
OverrideKey = tuple[Region, str, str, int, MarketSide]


class PricingIndex:
    """Resolve all scenario prices from preloaded rows with one fixed scan clock."""

    def __init__(
        self,
        market_prices: Iterable[MarketPrice],
        overrides: Iterable[UserPriceOverride] = (),
        history: Iterable[MarketHistoryInterval] = (),
    ) -> None:
        self.market_prices: dict[MarketKey, MarketPrice] = {
            (row.region, row.item_id, row.city, row.quality): row for row in market_prices
        }
        self.overrides: dict[OverrideKey, UserPriceOverride] = {
            (row.region, row.item_id, row.city, row.quality, row.side): row for row in overrides
        }
        self.history: dict[MarketKey, list[MarketHistoryInterval]] = {}
        for row in history:
            key = (row.region, row.item_id, row.city, row.quality)
            self.history.setdefault(key, []).append(row)

    def resolve(
        self,
        recipe: Recipe,
        *,
        material_city: str,
        craft_city: str,
        sell_city: str,
        region: Region,
        output_quality: int,
        material_side: MarketSide,
        output_side: MarketSide,
        freshness_policy: FreshnessPolicy,
        as_of: datetime,
        include_returned_material_prices: bool = False,
    ) -> OpportunityPricingSnapshot:
        material_prices: dict[str, float | None] = {}
        returned_material_prices: dict[str, float | None] = {}
        evidence: list[PriceEvidence] = []
        reasons: list[ActionabilityReason] = []
        for requirement in recipe.materials:
            line = self._resolve_one(
                requirement.item_id,
                material_city,
                1,
                region,
                material_side,
                freshness_policy,
                as_of,
                role="material",
            )
            material_prices[requirement.item_id] = line.price
            evidence.append(line)
            self._append_quality_reasons(line, reasons)

        output = self._resolve_one(
            recipe.output.item_id,
            sell_city,
            output_quality,
            region,
            output_side,
            freshness_policy,
            as_of,
            role="output",
        )
        evidence.append(output)
        self._append_quality_reasons(output, reasons)

        required_evidence = tuple(evidence)
        if include_returned_material_prices:
            for requirement in recipe.materials:
                if requirement.returnable is not True:
                    continue
                line = self._resolve_one(
                    requirement.item_id,
                    craft_city,
                    1,
                    region,
                    material_side,
                    freshness_policy,
                    as_of,
                    role="returned_material_informational",
                )
                returned_material_prices[requirement.item_id] = line.price
                evidence.append(line)

        material_lines = [line for line in required_evidence if line.role == "material"]
        material_timestamps = [
            line.observation_timestamp
            for line in material_lines
            if line.observation_timestamp is not None
        ]
        all_timestamps = [
            line.observation_timestamp
            for line in required_evidence
            if line.observation_timestamp is not None
        ]
        return OpportunityPricingSnapshot(
            material_prices=material_prices,
            output_price=output.price,
            evidence=tuple(evidence),
            freshness=worst_freshness(required_evidence),
            output_timestamp=output.observation_timestamp,
            oldest_material_timestamp=(
                min(material_timestamps)
                if material_lines and len(material_timestamps) == len(material_lines)
                else None
            ),
            oldest_required_timestamp=(
                min(all_timestamps)
                if required_evidence and len(all_timestamps) == len(required_evidence)
                else None
            ),
            returned_material_craft_city_prices=returned_material_prices,
            data_quality_reasons=tuple(reasons),
        )

    def _resolve_one(
        self,
        item_id: str,
        city: str,
        quality: int,
        region: Region,
        side: MarketSide,
        policy: FreshnessPolicy,
        as_of: datetime,
        *,
        role: str,
    ) -> PriceEvidence:
        override = self.overrides.get((region, item_id, city, quality, side))
        record = self.market_prices.get((region, item_id, city, quality))
        history = self.history.get((region, item_id, city, quality), ())
        selection = resolve_price(
            item_id=item_id,
            city=city,
            quality=quality,
            side=side,
            role=role,
            freshness_policy=policy,
            as_of=as_of,
            market_price=record,
            override=override,
            history=history,
        )
        return self._to_evidence(selection)

    @staticmethod
    def _to_evidence(line: ResolvedPrice) -> PriceEvidence:
        return PriceEvidence(
            item_id=line.item_id,
            city=line.city,
            quality=line.quality,
            side=line.side.value,
            role=line.role,
            price=line.price,
            observation_timestamp=line.observation_timestamp,
            fetched_at=line.fetched_at,
            provenance=line.provenance,
            freshness=line.freshness,
            source=line.source,
            confidence=line.confidence,
            current_price=line.current_price,
            current_timestamp=line.current_timestamp,
            current_fetched_at=line.current_fetched_at,
            current_freshness=line.current_freshness,
            historical_reference_price=line.historical_reference_price,
            historical_days_used=line.historical_days_used,
            historical_total_volume=line.historical_total_volume,
            historical_avg_daily_volume_7d=line.historical_avg_daily_volume_7d,
            historical_avg_daily_volume_30d=line.historical_avg_daily_volume_30d,
            historical_median_price=line.historical_median_price,
            historical_volatility=line.historical_volatility,
            historical_latest_bucket=line.historical_latest_bucket,
            historical_outliers_ignored=line.historical_outliers_ignored,
        )

    @staticmethod
    def _from_evidence(line: PriceEvidence) -> ResolvedPrice:
        return ResolvedPrice(
            item_id=line.item_id,
            city=line.city,
            quality=line.quality,
            side=MarketSide(line.side),
            role=line.role,
            price=line.price,
            observation_timestamp=line.observation_timestamp,
            fetched_at=line.fetched_at,
            provenance=line.provenance,
            freshness=line.freshness,
            source=line.source,
            confidence=line.confidence,
            current_price=line.current_price,
            current_timestamp=line.current_timestamp,
            current_fetched_at=line.current_fetched_at,
            current_freshness=line.current_freshness,
            historical_reference_price=line.historical_reference_price,
            historical_days_used=line.historical_days_used,
            historical_total_volume=line.historical_total_volume,
            historical_avg_daily_volume_7d=line.historical_avg_daily_volume_7d,
            historical_avg_daily_volume_30d=line.historical_avg_daily_volume_30d,
            historical_median_price=line.historical_median_price,
            historical_volatility=line.historical_volatility,
            historical_latest_bucket=line.historical_latest_bucket,
            historical_outliers_ignored=line.historical_outliers_ignored,
        )

    @staticmethod
    def _append_quality_reasons(
        line: PriceEvidence,
        reasons: list[ActionabilityReason],
    ) -> None:
        reasons.extend(price_quality_reasons(PricingIndex._from_evidence(line)))
