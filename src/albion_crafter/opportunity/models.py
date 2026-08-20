from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Event
from typing import TYPE_CHECKING

from albion_crafter.core.actionability import ActionabilityReason
from albion_crafter.core.models import CraftResult, Recipe, SaleMethod
from albion_crafter.core.provenance import Provenance
from albion_crafter.market.estimation import MarketPriceSource, PriceConfidence
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Freshness, Region

if TYPE_CHECKING:
    from albion_crafter.market.liquidity import LiquidityAssessment


class OpportunitySort(StrEnum):
    PROFIT = "profit"
    ROI = "roi"
    MARGIN = "margin"
    SILVER_PER_FOCUS = "silver_per_focus"


@dataclass(frozen=True, slots=True)
class ScanConstraints:
    """Explicit bounds for a scan.

    ``material_city`` deliberately supports a constrained buy-city mode without
    creating the V0.4 material x craft x sell Cartesian search. When it is
    absent, materials are bought in each scenario's craft city.
    """

    region: Region
    craft_cities: tuple[str, ...]
    sell_cities: tuple[str, ...]
    material_city: str | None = None
    text: str = ""
    tier_min: int | None = None
    tier_max: int | None = None
    enchantments: tuple[int, ...] = ()
    crafting_categories: tuple[str, ...] = ()
    use_focus: bool = False
    available_focus: float = 0.0
    premium: bool = True
    maximum_price_age: timedelta = timedelta(hours=4)
    maximum_station_fee_age: timedelta = timedelta(hours=24)
    allow_stale_station_fees: bool = False
    actionable_only: bool = True
    minimum_profit: float | None = None
    minimum_roi: float | None = None
    maximum_upfront_capital: float | None = None
    liquidity_levels: tuple[str, ...] = ()
    output_quality: int = 1
    crafts: int = 1
    sale_method: SaleMethod = SaleMethod.SELL_ORDER
    sort_by: OpportunitySort = OpportunitySort.PROFIT
    descending: bool = True

    def __post_init__(self) -> None:
        if not self.craft_cities or not self.sell_cities:
            raise ValueError("at least one craft city and sell city are required")
        if any(not city.strip() for city in (*self.craft_cities, *self.sell_cities)):
            raise ValueError("city names cannot be empty")
        if _casefold_duplicates(self.craft_cities):
            raise ValueError("craft_cities cannot contain duplicates")
        if _casefold_duplicates(self.sell_cities):
            raise ValueError("sell_cities cannot contain duplicates")
        if self.material_city is not None and not self.material_city.strip():
            raise ValueError("material_city cannot be blank")
        if self.tier_min is not None and not 1 <= self.tier_min <= 8:
            raise ValueError("tier_min must be between 1 and 8")
        if self.tier_max is not None and not 1 <= self.tier_max <= 8:
            raise ValueError("tier_max must be between 1 and 8")
        if (
            self.tier_min is not None
            and self.tier_max is not None
            and self.tier_min > self.tier_max
        ):
            raise ValueError("tier_min cannot exceed tier_max")
        if any(not 0 <= level <= 4 for level in self.enchantments):
            raise ValueError("enchantments must be between 0 and 4")
        if len(set(self.enchantments)) != len(self.enchantments):
            raise ValueError("enchantments cannot contain duplicates")
        if any(not value.strip() for value in self.crafting_categories):
            raise ValueError("crafting categories cannot be blank")
        if _casefold_duplicates(self.crafting_categories):
            raise ValueError("crafting_categories cannot contain duplicates")
        if not _finite_number(self.available_focus) or self.available_focus < 0:
            raise ValueError("available_focus cannot be negative")
        if self.maximum_price_age <= timedelta(0):
            raise ValueError("maximum_price_age must be positive")
        if self.maximum_station_fee_age <= timedelta(0):
            raise ValueError("maximum_station_fee_age must be positive")
        for name, value in (
            ("minimum_profit", self.minimum_profit),
            ("minimum_roi", self.minimum_roi),
            ("maximum_upfront_capital", self.maximum_upfront_capital),
        ):
            if value is not None and not _finite_number(value):
                raise ValueError(f"{name} must be finite")
        if self.maximum_upfront_capital is not None and self.maximum_upfront_capital < 0:
            raise ValueError("maximum_upfront_capital cannot be negative")
        valid_liquidity = {level.value for level in LiquidityLevel}
        if any(str(level) not in valid_liquidity for level in self.liquidity_levels):
            raise ValueError("liquidity_levels contains an unknown classification")
        if len(set(self.liquidity_levels)) != len(self.liquidity_levels):
            raise ValueError("liquidity_levels cannot contain duplicates")
        if isinstance(self.output_quality, bool) or not 1 <= self.output_quality <= 5:
            raise ValueError("output_quality must be between 1 and 5")
        if isinstance(self.crafts, bool) or not isinstance(self.crafts, int) or self.crafts < 1:
            raise ValueError("crafts must be positive")
        if not isinstance(self.sale_method, SaleMethod):
            raise ValueError("sale_method must be a SaleMethod")
        if not isinstance(self.sort_by, OpportunitySort):
            raise ValueError("sort_by must be an OpportunitySort")


@dataclass(frozen=True, slots=True)
class PriceEvidence:
    item_id: str
    city: str
    quality: int
    side: str
    role: str
    price: float | None
    observation_timestamp: datetime | None
    fetched_at: datetime | None
    provenance: Provenance
    freshness: Freshness
    source: MarketPriceSource
    confidence: PriceConfidence
    current_price: float | None = None
    current_timestamp: datetime | None = None
    current_fetched_at: datetime | None = None
    current_freshness: Freshness = Freshness.UNKNOWN
    historical_reference_price: float | None = None
    historical_days_used: int = 0
    historical_total_volume: int = 0
    historical_avg_daily_volume_7d: float | None = None
    historical_avg_daily_volume_30d: float | None = None
    historical_median_price: float | None = None
    historical_volatility: float | None = None
    historical_latest_bucket: datetime | None = None
    historical_outliers_ignored: int = 0

    def age(self, as_of: datetime) -> timedelta | None:
        if self.observation_timestamp is None:
            return None
        return as_of - self.observation_timestamp


@dataclass(frozen=True, slots=True)
class OpportunityPricingSnapshot:
    material_prices: dict[str, float | None]
    output_price: float | None
    evidence: tuple[PriceEvidence, ...]
    freshness: Freshness
    output_timestamp: datetime | None
    oldest_material_timestamp: datetime | None
    oldest_required_timestamp: datetime | None
    returned_material_craft_city_prices: dict[str, float | None] = field(default_factory=dict)
    data_quality_reasons: tuple[ActionabilityReason, ...] = ()

    def output_age(self, as_of: datetime) -> timedelta | None:
        if self.output_timestamp is None:
            return None
        return as_of - self.output_timestamp

    def oldest_material_age(self, as_of: datetime) -> timedelta | None:
        if self.oldest_material_timestamp is None:
            return None
        return as_of - self.oldest_material_timestamp

    def oldest_required_age(self, as_of: datetime) -> timedelta | None:
        if self.oldest_required_timestamp is None:
            return None
        return as_of - self.oldest_required_timestamp

    @property
    def historical_estimate_count(self) -> int:
        return sum(
            line.source is MarketPriceSource.HISTORICAL_ESTIMATE
            and line.role != "returned_material_informational"
            for line in self.evidence
        )

    @property
    def live_price_count(self) -> int:
        return sum(
            line.source is MarketPriceSource.CURRENT
            and line.role != "returned_material_informational"
            for line in self.evidence
        )


@dataclass(frozen=True, slots=True)
class CraftOpportunity:
    recipe: Recipe
    material_city: str
    craft_city: str
    sell_city: str
    pricing: OpportunityPricingSnapshot
    calculation: CraftResult
    station_type: str | None
    station_displayed_fee: float | None
    station_fee_provenance: Provenance
    station_fee_observed_at: datetime | None
    production_bonus: float | None
    production_bonus_status: str
    focus_efficiency: float | None
    focus_efficiency_source: str
    upfront_capital_required: float | None
    maximum_focus_crafts: int | None
    station_fee_freshness: Freshness = Freshness.UNKNOWN
    liquidity: LiquidityAssessment | None = None

    @property
    def item_id(self) -> str:
        return self.recipe.output.item_id

    @property
    def display_name(self) -> str:
        return self.recipe.output.display_name

    @property
    def profit(self) -> float | None:
        return self.calculation.profit

    @property
    def roi(self) -> float | None:
        return self.calculation.roi

    @property
    def margin(self) -> float | None:
        return self.calculation.margin

    @property
    def silver_per_focus(self) -> float | None:
        return self.calculation.silver_per_focus


@dataclass(frozen=True, slots=True)
class ScanProgress:
    stage: str
    message: str
    completed: int = 0
    total: int | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(max(self.completed / self.total, 0.0), 1.0)


@dataclass(frozen=True, slots=True)
class ScanSnapshot:
    """Immutable scan result and bounded-load telemetry.

    ``database_load_operations`` counts SQL read statements performed by the
    production bulk repositories. The name is retained as a compatibility-safe
    distinction from the much larger number of in-memory scenario evaluations.
    """

    scan_time: datetime
    ruleset_id: str
    constraints: ScanConstraints
    recipes_considered: int
    scenarios_evaluated: int
    actionable_count: int
    rejected_count: int
    opportunities: tuple[CraftOpportunity, ...]
    database_load_operations: int
    market_rows_loaded: int
    override_rows_loaded: int
    elapsed_seconds: float
    cancelled: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)
    rejection_class_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.scan_time.tzinfo is None:
            raise ValueError("scan_time must be timezone-aware")

    @property
    def database_read_statements(self) -> int:
        return self.database_load_operations


class CancellationToken:
    """Small GUI-neutral cancellation primitive for worker adapters."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _casefold_duplicates(values: tuple[str, ...]) -> bool:
    normalized = tuple(value.casefold() for value in values)
    return len(set(normalized)) != len(normalized)
