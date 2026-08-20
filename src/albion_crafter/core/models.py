from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .actionability import ActionabilityAssessment
from .crafting_profile import CraftingSkillProfile
from .freshness import FreshnessPolicy
from .provenance import Provenance
from .stations import StationFeeObservation


class SaleMethod(StrEnum):
    SELL_ORDER = "sell_order"
    INSTANT_SELL = "instant_sell"


class ActionKind(StrEnum):
    """Durable economic-action identity used across planning and snapshots."""

    CRAFT = "craft"
    REFINE = "refine"
    ARBITRAGE = "arbitrage"


REFINING_CATEGORIES = frozenset({"ore", "wood", "hide", "fiber", "rock"})


@dataclass(frozen=True, slots=True)
class Item:
    item_id: str
    display_name: str
    tier: int | None
    enchantment: int = 0
    category: str = ""
    subcategory: str = ""
    crafting_category: str = ""
    max_quality: int | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id cannot be empty")
        if self.tier is not None and self.tier < 1:
            raise ValueError("tier must be positive when known")
        if self.enchantment < 0:
            raise ValueError("enchantment cannot be negative")
        if self.max_quality is not None and not 1 <= self.max_quality <= 5:
            raise ValueError("max_quality must be between 1 and 5")

    @property
    def action_kind(self) -> ActionKind:
        return (
            ActionKind.REFINE
            if self.crafting_category.casefold() in REFINING_CATEGORIES
            else ActionKind.CRAFT
        )


@dataclass(frozen=True, slots=True)
class MaterialRequirement:
    item_id: str
    quantity: float
    returnable: bool | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("material item_id cannot be empty")
        if not _finite_number(self.quantity) or self.quantity <= 0:
            raise ValueError("material quantity must be finite and positive")


@dataclass(frozen=True, slots=True)
class Recipe:
    output: Item
    output_quantity: int
    materials: tuple[MaterialRequirement, ...]
    item_value: float | None = None
    base_focus_cost: float | None = None
    recipe_ambiguous: bool = False
    provenance: Provenance = Provenance.UNKNOWN
    source_version: str | None = None

    @property
    def action_kind(self) -> ActionKind:
        """Classify the canonical static recipe without persisting duplicate state."""

        return self.output.action_kind

    def __post_init__(self) -> None:
        if self.output_quantity <= 0:
            raise ValueError("output_quantity must be positive")
        if not self.materials:
            raise ValueError("a recipe must contain at least one material")
        if self.item_value is not None and (
            not _finite_number(self.item_value) or self.item_value < 0
        ):
            raise ValueError("item_value must be finite and non-negative")
        if self.base_focus_cost is not None and (
            not _finite_number(self.base_focus_cost) or self.base_focus_cost < 0
        ):
            raise ValueError("base_focus_cost must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class CraftingProfile:
    available_focus: float = 0.0
    focus_cost_efficiency: float = 0.0

    def __post_init__(self) -> None:
        if any(
            not _finite_number(value) or value < 0
            for value in (self.available_focus, self.focus_cost_efficiency)
        ):
            raise ValueError("Focus profile values must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class CraftingContext:
    craft_city: str
    sell_city: str
    crafts: int = 1
    output_quality: int = 1
    use_focus: bool = False
    premium: bool = True
    station_usage_fee_percent: float | None = None
    sale_method: SaleMethod = SaleMethod.SELL_ORDER
    profile: CraftingProfile | CraftingSkillProfile = field(default_factory=CraftingProfile)
    material_buy_city: str | None = None
    station_fee_observation: StationFeeObservation | None = None
    station_fee_freshness_policy: FreshnessPolicy | None = None
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if self.crafts < 1:
            raise ValueError("crafts must be positive")
        if not 1 <= self.output_quality <= 5:
            raise ValueError("output_quality must be between 1 and 5")
        if self.station_usage_fee_percent is not None and (
            not _finite_number(self.station_usage_fee_percent) or self.station_usage_fee_percent < 0
        ):
            raise ValueError("station_usage_fee_percent must be finite and non-negative")
        if (
            self.station_fee_observation is not None
            and self.station_fee_observation.city != self.craft_city
        ):
            raise ValueError("station-fee observation city must match craft_city")
        if (
            self.station_fee_observation is not None
            and self.station_usage_fee_percent is not None
            and self.station_fee_observation.displayed_fee != self.station_usage_fee_percent
        ):
            raise ValueError("raw and observed station fees disagree")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("crafting context as_of must be timezone-aware")

    @property
    def effective_material_buy_city(self) -> str:
        return self.material_buy_city or self.craft_city

    @property
    def production_city(self) -> str:
        """Compatibility-conscious generic name for the existing craft-city field."""

        return self.craft_city

    @property
    def displayed_station_fee(self) -> float | None:
        if self.station_fee_observation is not None:
            return self.station_fee_observation.displayed_fee
        return self.station_usage_fee_percent


@dataclass(frozen=True, slots=True)
class CraftResult:
    item_id: str
    crafts: int
    output_quantity: int
    raw_material_cost: float | None
    expected_returned_material_value: float | None
    effective_material_cost: float | None
    station_fee: float | None
    total_craft_cost: float | None
    gross_sale_value: float | None
    market_fees: float | None
    net_sale_value: float | None
    profit: float | None
    roi: float | None
    margin: float | None
    break_even_price: float | None
    focus_used: float | None
    focus_available: float
    focus_shortfall: float | None
    profit_without_focus: float | None
    profit_with_focus: float | None
    incremental_focus_profit: float | None
    silver_per_focus: float | None
    return_rate: float | None
    ruleset_id: str
    actionability: ActionabilityAssessment
    missing_price_item_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    upfront_material_cost: float | None = None
    upfront_capital_required: float | None = None
    gross_material_purchase_cash: float | None = None
    station_cash: float | None = None
    listing_setup_cash: float | None = None
    transaction_tax: float | None = None
    total_pre_revenue_cash_required: float | None = None
    effective_economic_cost: float | None = None
    returned_material_cost_basis_value: float | None = None
    returned_material_craft_city_market_value: float | None = None

    @property
    def is_calculable(self) -> bool:
        return self.profit is not None

    @property
    def is_displayable(self) -> bool:
        return any(
            value is not None
            for value in (self.raw_material_cost, self.gross_sale_value, self.profit)
        )

    @property
    def is_complete(self) -> bool:
        return not self.missing_price_item_ids and self.profit is not None

    @property
    def required_investment(self) -> float | None:
        """Compatibility-friendly name for cash required before returns/revenue."""

        if self.total_pre_revenue_cash_required is not None:
            return self.total_pre_revenue_cash_required
        return self.upfront_capital_required


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
