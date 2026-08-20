from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any

from albion_crafter.core.models import ActionKind, SaleMethod
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import MarketSide, Region

LEGACY_SNAPSHOT_FORMAT_VERSION = 1
V2_SNAPSHOT_FORMAT_VERSION = 2
SNAPSHOT_FORMAT_VERSION = 3

# Optimizer resource arithmetic is deliberately integral. Callers must round
# cash/Focus consumption upward and expected profit downward before constructing
# CandidateEconomics. This makes a feasible result conservative and keeps Pareto
# comparisons deterministic across platforms.
type ResourceAmount = int
type ExecutionCapacityKey = tuple[Region, str, str, int]


def quantize_resource_up(value: int | float | Decimal) -> ResourceAmount:
    """Round a non-negative resource requirement up to a whole unit."""

    number = _decimal(value, "resource requirement")
    if number < 0:
        raise ValueError("resource requirement cannot be negative")
    return int(number.to_integral_value(rounding=ROUND_CEILING))


def quantize_profit_down(value: int | float | Decimal) -> ResourceAmount:
    """Round expected profit down to a conservative whole-silver amount."""

    return int(_decimal(value, "expected profit").to_integral_value(rounding=ROUND_FLOOR))


class TransportPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    ACKNOWLEDGED_UNCOSTED = "acknowledged_uncosted"
    EXPLICIT_COST = "explicit_cost"


class ArbitrageScope(StrEnum):
    ALL_PRODUCTION_OUTPUTS = "all_production_outputs"
    CRAFTED_OUTPUTS = "crafted_outputs"
    REFINED_RESOURCES = "refined_resources"


OUTER_ROYAL_CITIES = (
    "Bridgewatch",
    "Fort Sterling",
    "Lymhurst",
    "Martlock",
    "Thetford",
)


class MinimumLiquidity(StrEnum):
    ANY = "any"
    LOW = "low_or_better"
    MODERATE = "moderate_or_better"
    HIGH = "high_only"

    @property
    def minimum_rank(self) -> int:
        return {
            MinimumLiquidity.ANY: 0,
            MinimumLiquidity.LOW: 1,
            MinimumLiquidity.MODERATE: 2,
            MinimumLiquidity.HIGH: 3,
        }[self]


class PriceRole(StrEnum):
    MATERIAL = "material"
    OUTPUT = "output"
    RETURNED_MATERIAL_INFORMATIONAL = "returned_material_informational"
    ARBITRAGE_SOURCE = "arbitrage_source"
    ARBITRAGE_DESTINATION = "arbitrage_destination"


class CapacityRole(StrEnum):
    ACQUISITION = "acquisition"
    LIQUIDATION = "liquidation"


class PlanStatus(StrEnum):
    DECISION_GRADE = "decision_grade"
    ADVISORY = "advisory"
    NON_ACTIONABLE = "non_actionable"


class OptimizationStatus(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"


class PlanReasonSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class PlanReasonCode(StrEnum):
    APPROXIMATE_OPTIMIZATION = "approximate_optimization"
    CANCELLED = "cancelled"
    INSUFFICIENT_FOCUS = "insufficient_focus"
    INSUFFICIENT_SILVER = "insufficient_silver"
    FUTURE_MARKET_DATA = "future_market_data"
    FUTURE_STATION_FEE = "future_station_fee"
    INVALID_ACTION_EVIDENCE = "invalid_action_evidence"
    INVALID_RESOURCE_TOTAL = "invalid_resource_total"
    LOW_LIQUIDITY = "low_liquidity"
    MISSING_MATERIAL_PRICE = "missing_material_price"
    MISSING_OUTPUT_PRICE = "missing_output_price"
    MISSING_STATION_FEE = "missing_station_fee"
    MISSING_EXPLICIT_TRANSPORT_COST = "missing_explicit_transport_cost"
    NO_FEASIBLE_ACTIONS = "no_feasible_actions"
    QUANTITY_CEILING_EXCEEDED = "quantity_ceiling_exceeded"
    STALE_MARKET_DATA = "stale_market_data"
    STALE_STATION_FEE = "stale_station_fee"
    TRANSPORT_FORBIDDEN = "transport_forbidden"
    UNMODELED_TRANSPORT = "unmodeled_transport"
    UNKNOWN_CITY_BONUS = "unknown_city_bonus"
    UNKNOWN_FCE = "unknown_fce"
    UNKNOWN_LIQUIDITY = "unknown_liquidity"
    UNTRUSTED_PROVENANCE = "untrusted_provenance"
    UNVERIFIED_MECHANICS = "unverified_mechanics"
    UNSUPPORTED_OUTPUT_QUALITY = "unsupported_output_quality"
    VALIDATION_FAILED = "validation_failed"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PlanReason:
    code: PlanReasonCode
    message: str
    severity: PlanReasonSeverity = PlanReasonSeverity.BLOCKING

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("plan reason message is required")


@dataclass(frozen=True, slots=True)
class FindMoneyConstraints:
    available_silver: ResourceAmount
    available_focus: ResourceAmount
    region: Region = Region.AMERICAS
    silver_reserve: ResourceAmount = 0
    focus_reserve: ResourceAmount = 0
    premium: bool = True
    item_query: str = ""
    tiers: frozenset[int] = field(default_factory=lambda: frozenset(range(4, 9)))
    enchantments: frozenset[int] = field(default_factory=lambda: frozenset(range(4)))
    categories: frozenset[str] = field(default_factory=frozenset)
    material_cities: tuple[str, ...] = ("Bridgewatch",)
    craft_cities: tuple[str, ...] = ("Bridgewatch",)
    sell_cities: tuple[str, ...] = ("Bridgewatch",)
    use_focus: bool = True
    max_market_age: timedelta = timedelta(hours=4)
    max_station_fee_age: timedelta = timedelta(hours=24)
    allow_stale_station_fees: bool = False
    minimum_profit: ResourceAmount | None = None
    minimum_roi: float | None = None
    minimum_liquidity: MinimumLiquidity = MinimumLiquidity.ANY
    sale_method: SaleMethod = SaleMethod.SELL_ORDER
    transport_policy: TransportPolicy = TransportPolicy.LOCAL_ONLY
    transport_cost_per_craft: ResourceAmount | None = None
    per_item_craft_cap: int = 10
    historical_volume_share: float | None = 0.20
    history_enabled: bool = True
    history_shortlist_limit: int = 200
    force_current_price_refresh: bool = False
    action_kinds: frozenset[ActionKind] = field(
        default_factory=lambda: frozenset((ActionKind.CRAFT, ActionKind.REFINE))
    )
    refining_families: frozenset[str] = field(
        default_factory=lambda: frozenset({"ore", "wood", "hide", "fiber", "rock"})
    )
    arbitrage_scope: ArbitrageScope = ArbitrageScope.ALL_PRODUCTION_OUTPUTS
    arbitrage_source_cities: tuple[str, ...] = OUTER_ROYAL_CITIES
    arbitrage_destination_cities: tuple[str, ...] = OUTER_ROYAL_CITIES

    def __post_init__(self) -> None:
        _nonnegative_int(self.available_silver, "available_silver")
        _nonnegative_int(self.available_focus, "available_focus")
        _nonnegative_int(self.silver_reserve, "silver_reserve")
        _nonnegative_int(self.focus_reserve, "focus_reserve")
        if self.silver_reserve > self.available_silver:
            raise ValueError("silver_reserve cannot exceed available_silver")
        if self.focus_reserve > self.available_focus:
            raise ValueError("focus_reserve cannot exceed available_focus")
        if not self.tiers or any(
            isinstance(value, bool) or not 1 <= value <= 8 for value in self.tiers
        ):
            raise ValueError("tiers must contain integers between 1 and 8")
        if not self.enchantments or any(
            isinstance(value, bool) or not 0 <= value <= 4 for value in self.enchantments
        ):
            raise ValueError("enchantments must contain integers between 0 and 4")
        if any(not value.strip() for value in self.categories):
            raise ValueError("categories cannot contain blank values")
        if not self.action_kinds:
            raise ValueError("at least one action kind must be selected")
        if any(not isinstance(value, ActionKind) for value in self.action_kinds):
            raise ValueError("action_kinds must contain ActionKind values")
        supported_refining = {"ore", "wood", "hide", "fiber", "rock"}
        if any(value not in supported_refining for value in self.refining_families):
            raise ValueError("refining_families contains an unsupported resource family")
        if ActionKind.REFINE in self.action_kinds and not self.refining_families:
            raise ValueError("at least one refining family is required when refining is selected")
        outer = {value.casefold() for value in OUTER_ROYAL_CITIES}
        for name in ("arbitrage_source_cities", "arbitrage_destination_cities"):
            values = getattr(self, name)
            _validate_cities(values, name)
            if any(value.casefold() not in outer for value in values):
                raise ValueError(f"{name} supports only the five outer Royal cities")
        if ActionKind.ARBITRAGE in self.action_kinds and (
            not self.arbitrage_source_cities or not self.arbitrage_destination_cities
        ):
            raise ValueError("arbitrage requires source and destination cities")
        if "\x00" in self.item_query:
            raise ValueError("item_query cannot contain NUL characters")
        for name in ("material_cities", "craft_cities", "sell_cities"):
            _validate_cities(getattr(self, name), name)
        if self.max_market_age <= timedelta(0):
            raise ValueError("max_market_age must be positive")
        if self.max_station_fee_age <= timedelta(0):
            raise ValueError("max_station_fee_age must be positive")
        if self.minimum_profit is not None:
            _integer(self.minimum_profit, "minimum_profit")
        if self.minimum_roi is not None and (
            isinstance(self.minimum_roi, bool)
            or not isinstance(self.minimum_roi, (int, float))
            or not math.isfinite(self.minimum_roi)
        ):
            raise ValueError("minimum_roi must be a finite number")
        _positive_int(self.per_item_craft_cap, "per_item_craft_cap")
        _positive_int(self.history_shortlist_limit, "history_shortlist_limit")
        if self.historical_volume_share is not None and (
            isinstance(self.historical_volume_share, bool)
            or not isinstance(self.historical_volume_share, (int, float))
            or not math.isfinite(self.historical_volume_share)
            or not 0 < self.historical_volume_share <= 1
        ):
            raise ValueError("historical_volume_share must be greater than 0 and at most 1")
        if self.transport_cost_per_craft is not None:
            _nonnegative_int(self.transport_cost_per_craft, "transport_cost_per_craft")
        if self.transport_policy is TransportPolicy.EXPLICIT_COST:
            if self.transport_cost_per_craft is None:
                raise ValueError("explicit-cost transport requires transport_cost_per_craft")
        elif self.transport_cost_per_craft is not None:
            raise ValueError("transport_cost_per_craft is only valid with explicit-cost transport")

    @property
    def silver_budget(self) -> ResourceAmount:
        return self.available_silver - self.silver_reserve

    @property
    def focus_budget(self) -> ResourceAmount:
        return self.available_focus - self.focus_reserve if self.use_focus else 0

    @property
    def production_cities(self) -> tuple[str, ...]:
        return self.craft_cities

    @property
    def transport_cost_per_action_unit(self) -> ResourceAmount | None:
        return self.transport_cost_per_craft

    def to_dict(
        self,
        *,
        legacy: bool = False,
        format_version: int | None = None,
    ) -> dict[str, Any]:
        version = LEGACY_SNAPSHOT_FORMAT_VERSION if legacy else (format_version or 3)
        result = {
            "available_silver": self.available_silver,
            "available_focus": self.available_focus,
            "region": self.region.value,
            "silver_reserve": self.silver_reserve,
            "focus_reserve": self.focus_reserve,
            "premium": self.premium,
            "item_query": self.item_query,
            "tiers": sorted(self.tiers),
            "enchantments": sorted(self.enchantments),
            "categories": sorted(self.categories),
            "material_cities": list(self.material_cities),
            ("craft_cities" if version == 1 else "production_cities"): list(self.craft_cities),
            "sell_cities": list(self.sell_cities),
            "use_focus": self.use_focus,
            "max_market_age_seconds": self.max_market_age.total_seconds(),
            "max_station_fee_age_seconds": self.max_station_fee_age.total_seconds(),
            "allow_stale_station_fees": self.allow_stale_station_fees,
            "minimum_profit": self.minimum_profit,
            "minimum_roi": self.minimum_roi,
            "minimum_liquidity": self.minimum_liquidity.value,
            "sale_method": self.sale_method.value,
            "transport_policy": self.transport_policy.value,
            (
                "transport_cost_per_craft"
                if version <= V2_SNAPSHOT_FORMAT_VERSION
                else "transport_cost_per_action_unit"
            ): self.transport_cost_per_craft,
            "per_item_craft_cap": self.per_item_craft_cap,
            "historical_volume_share": self.historical_volume_share,
            "history_enabled": self.history_enabled,
            "history_shortlist_limit": self.history_shortlist_limit,
            "force_current_price_refresh": self.force_current_price_refresh,
        }
        if version >= V2_SNAPSHOT_FORMAT_VERSION:
            result["action_kinds"] = sorted(value.value for value in self.action_kinds)
            result["refining_families"] = sorted(self.refining_families)
        if version >= SNAPSHOT_FORMAT_VERSION:
            result["arbitrage_scope"] = self.arbitrage_scope.value
            result["arbitrage_source_cities"] = list(self.arbitrage_source_cities)
            result["arbitrage_destination_cities"] = list(self.arbitrage_destination_cities)
        return result

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy: bool = False,
        format_version: int | None = None,
    ) -> FindMoneyConstraints:
        version = LEGACY_SNAPSHOT_FORMAT_VERSION if legacy else (format_version or 3)
        return cls(
            available_silver=int(value["available_silver"]),
            available_focus=int(value["available_focus"]),
            region=Region(value["region"]),
            silver_reserve=int(value.get("silver_reserve", 0)),
            focus_reserve=int(value.get("focus_reserve", 0)),
            premium=bool(value.get("premium", True)),
            item_query=str(value.get("item_query", "")),
            tiers=frozenset(int(item) for item in value["tiers"]),
            enchantments=frozenset(int(item) for item in value["enchantments"]),
            categories=frozenset(str(item) for item in value.get("categories", ())),
            material_cities=tuple(str(item) for item in value["material_cities"]),
            craft_cities=tuple(
                str(item) for item in value.get("production_cities", value.get("craft_cities", ()))
            ),
            sell_cities=tuple(str(item) for item in value["sell_cities"]),
            use_focus=bool(value.get("use_focus", True)),
            max_market_age=timedelta(seconds=float(value["max_market_age_seconds"])),
            max_station_fee_age=timedelta(seconds=float(value["max_station_fee_age_seconds"])),
            allow_stale_station_fees=bool(value.get("allow_stale_station_fees", False)),
            minimum_profit=_optional_int(value.get("minimum_profit")),
            minimum_roi=_optional_float(value.get("minimum_roi")),
            minimum_liquidity=MinimumLiquidity(value.get("minimum_liquidity", "any")),
            sale_method=SaleMethod(value.get("sale_method", SaleMethod.SELL_ORDER.value)),
            transport_policy=TransportPolicy(
                value.get("transport_policy", TransportPolicy.LOCAL_ONLY.value)
            ),
            transport_cost_per_craft=_optional_int(
                value.get("transport_cost_per_action_unit", value.get("transport_cost_per_craft"))
            ),
            per_item_craft_cap=int(value.get("per_item_craft_cap", 10)),
            historical_volume_share=_optional_float(value.get("historical_volume_share")),
            history_enabled=bool(value.get("history_enabled", True)),
            history_shortlist_limit=int(value.get("history_shortlist_limit", 200)),
            force_current_price_refresh=bool(value.get("force_current_price_refresh", False)),
            action_kinds=(
                frozenset({ActionKind.CRAFT})
                if version == LEGACY_SNAPSHOT_FORMAT_VERSION
                else frozenset(
                    ActionKind(item)
                    for item in value.get(
                        "action_kinds",
                        (
                            (ActionKind.CRAFT.value, ActionKind.REFINE.value)
                            if version == V2_SNAPSHOT_FORMAT_VERSION
                            else (ActionKind.CRAFT.value, ActionKind.REFINE.value)
                        ),
                    )
                )
            ),
            refining_families=frozenset(
                str(item)
                for item in value.get(
                    "refining_families",
                    ("ore", "wood", "hide", "fiber", "rock"),
                )
            ),
            arbitrage_scope=ArbitrageScope(
                value.get("arbitrage_scope", ArbitrageScope.ALL_PRODUCTION_OUTPUTS.value)
            ),
            arbitrage_source_cities=tuple(
                str(item) for item in value.get("arbitrage_source_cities", OUTER_ROYAL_CITIES)
            ),
            arbitrage_destination_cities=tuple(
                str(item) for item in value.get("arbitrage_destination_cities", OUTER_ROYAL_CITIES)
            ),
        )


@dataclass(frozen=True, slots=True, order=True)
class MarketKey:
    region: Region
    item_id: str
    city: str
    quality: int = 1

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.city.strip():
            raise ValueError("market item ID and city are required")
        if isinstance(self.quality, bool) or not 1 <= self.quality <= 5:
            raise ValueError("market quality must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class CapacityRequirement:
    """One shared market-capacity resource consumed by an action unit."""

    key: ExecutionCapacityKey
    role: CapacityRole
    units_per_action_unit: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, CapacityRole):
            raise ValueError("capacity role must be a CapacityRole")
        _positive_int(self.units_per_action_unit, "units_per_action_unit")

    @property
    def canonical_key(self) -> tuple[str, str, str, int, str, int]:
        return (
            self.key[0].value,
            self.key[1],
            self.key[2].casefold(),
            self.key[3],
            self.role.value,
            self.units_per_action_unit,
        )


@dataclass(frozen=True, slots=True)
class PriceRequirement:
    key: MarketKey
    side: MarketSide
    role: PriceRole
    required_for_actionability: bool = True


@dataclass(frozen=True, slots=True)
class CandidateRoute:
    region: Region
    material_city: str
    craft_city: str
    sell_city: str
    transport_policy: TransportPolicy
    transport_cost_per_craft: ResourceAmount = 0
    reasons: tuple[PlanReason, ...] = ()

    def __post_init__(self) -> None:
        _validate_cities(
            (self.material_city, self.craft_city, self.sell_city),
            "candidate route cities",
            allow_duplicates=True,
        )
        _nonnegative_int(self.transport_cost_per_craft, "transport_cost_per_craft")
        if self.transport_policy is TransportPolicy.LOCAL_ONLY and self.is_cross_city:
            raise ValueError("local-only transport cannot describe a cross-city route")

    @property
    def is_cross_city(self) -> bool:
        return (
            len(
                {
                    self.material_city.casefold(),
                    self.craft_city.casefold(),
                    self.sell_city.casefold(),
                }
            )
            > 1
        )

    @property
    def canonical_key(self) -> tuple[str, str, str, str]:
        return (
            self.region.value,
            self.material_city.casefold(),
            self.craft_city.casefold(),
            self.sell_city.casefold(),
        )

    @property
    def production_city(self) -> str:
        return self.craft_city

    @property
    def buy_city(self) -> str:
        return self.material_city

    @property
    def transport_cost_per_action_unit(self) -> ResourceAmount:
        return self.transport_cost_per_craft


@dataclass(frozen=True, slots=True)
class CandidateEconomics:
    """One-craft integer economics after conservative planner quantization.

    ``pre_revenue_cash_per_craft`` includes gross material purchase, station,
    sell-order setup, and any explicit transport cash. Transaction tax belongs
    in profit/economic cost because it is deducted after sale.
    """

    pre_revenue_cash_per_craft: ResourceAmount
    nonfocused_profit_per_craft: ResourceAmount
    focused_profit_per_craft: ResourceAmount | None = None
    focus_per_focused_craft: ResourceAmount | None = None
    nonfocused_eligible: bool = True
    expected_revenue_per_craft: ResourceAmount | None = None
    nonfocused_effective_cost_per_craft: ResourceAmount | None = None
    focused_effective_cost_per_craft: ResourceAmount | None = None
    gross_material_cash_per_craft: ResourceAmount | None = None
    station_cash_per_craft: ResourceAmount | None = None
    setup_cash_per_craft: ResourceAmount | None = None
    transport_cash_per_craft: ResourceAmount | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.pre_revenue_cash_per_craft, "pre_revenue_cash_per_craft")
        _integer(self.nonfocused_profit_per_craft, "nonfocused_profit_per_craft")
        paired = (self.focused_profit_per_craft, self.focus_per_focused_craft)
        if (paired[0] is None) != (paired[1] is None):
            raise ValueError(
                "focused profit and Focus cost must either both be known or both be absent"
            )
        if self.focused_profit_per_craft is not None:
            _integer(self.focused_profit_per_craft, "focused_profit_per_craft")
            assert self.focus_per_focused_craft is not None
            _positive_int(self.focus_per_focused_craft, "focus_per_focused_craft")
        for name in (
            "expected_revenue_per_craft",
            "nonfocused_effective_cost_per_craft",
            "focused_effective_cost_per_craft",
            "gross_material_cash_per_craft",
            "station_cash_per_craft",
            "setup_cash_per_craft",
            "transport_cash_per_craft",
        ):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_int(value, name)

    @property
    def has_focused_variant(self) -> bool:
        return self.focused_profit_per_craft is not None

    @property
    def incremental_focus_profit_per_craft(self) -> ResourceAmount | None:
        """Profit uplift attributable to Focus for one otherwise identical craft."""

        if self.focused_profit_per_craft is None:
            return None
        return self.focused_profit_per_craft - self.nonfocused_profit_per_craft


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    candidate_id: str
    item_id: str
    display_name: str
    route: CandidateRoute
    economics: CandidateEconomics
    action_kind: ActionKind = ActionKind.CRAFT
    output_quantity_per_craft: int = 1
    quality: int = 1
    sale_method: SaleMethod = SaleMethod.SELL_ORDER
    liquidity: LiquidityLevel = LiquidityLevel.UNKNOWN
    nonfocused_roi: float | None = None
    focused_roi: float | None = None
    execution_capacity_key: ExecutionCapacityKey | None = None
    capacity_requirements: tuple[CapacityRequirement, ...] = ()
    reasons: tuple[PlanReason, ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    oldest_market_observed_at: datetime | None = None
    station_fee_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not self.candidate_id.strip()
            or not self.item_id.strip()
            or not self.display_name.strip()
        ):
            raise ValueError("candidate ID, item ID, and display name are required")
        _positive_int(self.output_quantity_per_craft, "output_quantity_per_craft")
        if self.quality != 1:
            raise ValueError("V0.6 planning supports Normal quality only")
        for name in ("nonfocused_roi", "focused_roi"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        expected_key = (self.route.region, self.item_id, self.route.sell_city, self.quality)
        if self.execution_capacity_key is None:
            object.__setattr__(self, "execution_capacity_key", expected_key)
        elif self.execution_capacity_key != expected_key:
            raise ValueError(
                "execution capacity key must match region, item, sell city, and quality"
            )
        if not self.capacity_requirements:
            object.__setattr__(
                self,
                "capacity_requirements",
                (
                    CapacityRequirement(
                        expected_key,
                        CapacityRole.LIQUIDATION,
                        self.output_quantity_per_craft,
                    ),
                ),
            )
        _validate_capacity_requirements(self.capacity_requirements, expected_key)
        if self.action_kind is ActionKind.ARBITRAGE:
            _validate_arbitrage_capacity_requirements(
                self.capacity_requirements,
                self.route,
                self.item_id,
                self.quality,
            )
            if self.output_quantity_per_craft != 1:
                raise ValueError("arbitrage action units must move exactly one market item")
            if self.economics.has_focused_variant or self.station_fee_observed_at is not None:
                raise ValueError("arbitrage candidates cannot use Focus or station evidence")
        if self.route.transport_cost_per_craft > 0 and (
            self.economics.transport_cash_per_craft != self.route.transport_cost_per_craft
        ):
            raise ValueError(
                "explicit route transport cost must be included in candidate economics"
            )
        cash_components = (
            self.economics.gross_material_cash_per_craft,
            self.economics.station_cash_per_craft,
            self.economics.setup_cash_per_craft,
            self.economics.transport_cash_per_craft,
        )
        if all(value is not None for value in cash_components) and (
            sum(value for value in cash_components if value is not None)
            != self.economics.pre_revenue_cash_per_craft
        ):
            raise ValueError("pre-revenue cash must equal its supplied component breakdown")
        for name in ("oldest_market_observed_at", "station_fee_observed_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        if len({key for key, _ in self.evidence}) != len(self.evidence):
            raise ValueError("plan candidate evidence keys must be unique")
        if any(not key.strip() for key, _ in self.evidence):
            raise ValueError("plan candidate evidence keys cannot be blank")

    @property
    def has_blocker(self) -> bool:
        return any(reason.severity is PlanReasonSeverity.BLOCKING for reason in self.reasons)

    @property
    def liquidity_rank(self) -> int:
        return liquidity_rank(self.liquidity)

    @property
    def canonical_key(self) -> tuple[str, ...]:
        return (
            self.action_kind.value,
            self.item_id,
            *self.route.canonical_key,
            self.sale_method.value,
            self.candidate_id,
        )

    @property
    def capacity_signature(self) -> tuple[tuple[str, str, str, int, str, int], ...]:
        return tuple(
            sorted(requirement.canonical_key for requirement in self.capacity_requirements)
        )


@dataclass(frozen=True, slots=True)
class PlanAction:
    candidate_id: str
    item_id: str
    display_name: str
    route: CandidateRoute
    quantity: int
    focused_quantity: int
    nonfocused_quantity: int
    output_units: int
    quality: int
    sale_method: SaleMethod
    pre_revenue_cash_required: ResourceAmount
    focus_required: ResourceAmount
    expected_profit: ResourceAmount
    liquidity: LiquidityLevel
    execution_capacity_key: ExecutionCapacityKey
    quantity_ceiling: int
    action_kind: ActionKind = ActionKind.CRAFT
    capacity_requirements: tuple[CapacityRequirement, ...] = ()
    execution_ceiling_output_units: int | None = None
    expected_revenue: ResourceAmount | None = None
    effective_economic_cost: ResourceAmount | None = None
    incremental_focus_profit: ResourceAmount | None = None
    reasons: tuple[PlanReason, ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    oldest_market_observed_at: datetime | None = None
    station_fee_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _positive_int(self.quantity, "quantity")
        _nonnegative_int(self.focused_quantity, "focused_quantity")
        _nonnegative_int(self.nonfocused_quantity, "nonfocused_quantity")
        if self.focused_quantity + self.nonfocused_quantity != self.quantity:
            raise ValueError("focused and nonfocused quantities must sum to quantity")
        _positive_int(self.output_units, "output_units")
        if self.quality != 1:
            raise ValueError("V0.6 plan actions support Normal quality only")
        expected_key = (self.route.region, self.item_id, self.route.sell_city, self.quality)
        if self.execution_capacity_key != expected_key:
            raise ValueError("action execution capacity key does not match its output market")
        if not self.capacity_requirements:
            units_per_action = self.output_units // self.quantity
            if units_per_action * self.quantity != self.output_units:
                raise ValueError("output units must divide evenly across action quantity")
            object.__setattr__(
                self,
                "capacity_requirements",
                (
                    CapacityRequirement(
                        expected_key,
                        CapacityRole.LIQUIDATION,
                        units_per_action,
                    ),
                ),
            )
        _validate_capacity_requirements(self.capacity_requirements, expected_key)
        if self.action_kind is ActionKind.ARBITRAGE:
            _validate_arbitrage_capacity_requirements(
                self.capacity_requirements,
                self.route,
                self.item_id,
                self.quality,
            )
            if (
                self.output_units != self.quantity
                or self.focused_quantity
                or self.focus_required
                or self.station_fee_observed_at is not None
            ):
                raise ValueError(
                    "arbitrage actions move one item per unit and cannot use Focus or stations"
                )
        _nonnegative_int(self.pre_revenue_cash_required, "pre_revenue_cash_required")
        _nonnegative_int(self.focus_required, "focus_required")
        _integer(self.expected_profit, "expected_profit")
        _positive_int(self.quantity_ceiling, "quantity_ceiling")
        if self.quantity > self.quantity_ceiling:
            raise ValueError("action quantity cannot exceed its quantity ceiling")
        if self.execution_ceiling_output_units is not None:
            _nonnegative_int(
                self.execution_ceiling_output_units,
                "execution_ceiling_output_units",
            )
            if self.output_units > self.execution_ceiling_output_units:
                raise ValueError("action output units cannot exceed its execution ceiling")
        for name in ("expected_revenue", "effective_economic_cost"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_int(value, name)
        if self.incremental_focus_profit is not None:
            _integer(self.incremental_focus_profit, "incremental_focus_profit")
            if self.focus_required == 0 and self.incremental_focus_profit != 0:
                raise ValueError("incremental Focus profit requires a positive Focus commitment")
        for name in ("oldest_market_observed_at", "station_fee_observed_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        if len({key for key, _ in self.evidence}) != len(self.evidence):
            raise ValueError("plan action evidence keys must be unique")
        if any(not key.strip() for key, _ in self.evidence):
            raise ValueError("plan action evidence keys cannot be blank")

    @property
    def craft_count(self) -> int:
        """Legacy alias: quantity fields now measure generic production batches."""

        return self.quantity

    @property
    def roi(self) -> float | None:
        if not self.effective_economic_cost:
            return None
        return self.expected_profit / self.effective_economic_cost

    @property
    def margin(self) -> float | None:
        if not self.expected_revenue:
            return None
        return self.expected_profit / self.expected_revenue

    @property
    def silver_per_focus(self) -> float | None:
        if self.focus_required <= 0 or self.incremental_focus_profit is None:
            return None
        return self.incremental_focus_profit / self.focus_required

    @property
    def liquidity_rank(self) -> int:
        return liquidity_rank(self.liquidity)

    @property
    def canonical_key(self) -> tuple[str, ...]:
        return (
            self.action_kind.value,
            self.item_id,
            *self.route.canonical_key,
            self.sale_method.value,
            self.candidate_id,
        )

    @property
    def capacity_consumption(self) -> tuple[tuple[ExecutionCapacityKey, int], ...]:
        return tuple(
            (requirement.key, requirement.units_per_action_unit * self.quantity)
            for requirement in self.capacity_requirements
        )


@dataclass(frozen=True, slots=True)
class OptimizationDiagnostics:
    method: str
    status: OptimizationStatus
    candidate_count: int
    group_count: int
    quantity_decision_count: int
    states_considered: int
    states_pruned: int
    state_limit: int
    state_limit_reached: bool
    elapsed_seconds: float
    quantization_policy: str = "whole_units:resource_ceiling,profit_floor,budgets_integral:v1"
    quantity_states_generated: int = 0
    quantity_states_after_pruning: int = 0
    portfolio_states_considered: int = 0
    portfolio_states_pruned: int = 0
    peak_frontier_size: int = 0
    candidate_routes_before_pruning: int = 0
    candidate_routes_after_pruning: int = 0
    candidate_local_modes_removed: int = 0
    equivalent_routes_collapsed: int = 0
    approximation_reasons: tuple[str, ...] = ()
    effective_state_limit: int = 0
    quantity_transition_limit: int = 0
    portfolio_transition_limit: int = 0

    def __post_init__(self) -> None:
        if not self.method.strip():
            raise ValueError("optimizer method is required")
        for name in (
            "candidate_count",
            "group_count",
            "quantity_decision_count",
            "states_considered",
            "states_pruned",
            "state_limit",
            "quantity_states_generated",
            "quantity_states_after_pruning",
            "portfolio_states_considered",
            "portfolio_states_pruned",
            "peak_frontier_size",
            "candidate_routes_before_pruning",
            "candidate_routes_after_pruning",
            "candidate_local_modes_removed",
            "equivalent_routes_collapsed",
            "effective_state_limit",
            "quantity_transition_limit",
            "portfolio_transition_limit",
        ):
            _nonnegative_int(getattr(self, name), name)
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("optimizer elapsed_seconds must be finite and non-negative")
        if not self.quantization_policy.strip():
            raise ValueError("optimizer quantization_policy is required")
        if any(not reason.strip() for reason in self.approximation_reasons):
            raise ValueError("optimizer approximation reasons cannot be blank")
        if len(set(self.approximation_reasons)) != len(self.approximation_reasons):
            raise ValueError("optimizer approximation reasons must be unique")


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    actions: tuple[PlanAction, ...]
    total_pre_revenue_cash: ResourceAmount
    total_focus: ResourceAmount
    total_expected_profit: ResourceAmount
    silver_remaining: ResourceAmount
    focus_remaining: ResourceAmount
    plan_status: PlanStatus
    reasons: tuple[PlanReason, ...]
    diagnostics: OptimizationDiagnostics

    def __post_init__(self) -> None:
        for name in (
            "total_pre_revenue_cash",
            "total_focus",
            "silver_remaining",
            "focus_remaining",
        ):
            _nonnegative_int(getattr(self, name), name)
        _integer(self.total_expected_profit, "total_expected_profit")


@dataclass(frozen=True, slots=True)
class PlanDataHealth:
    market_observations_used: int = 0
    market_fresh: int = 0
    market_stale: int = 0
    user_overrides_used: int = 0
    station_fees_used: int = 0
    station_fees_fresh: int = 0
    station_fees_stale: int = 0
    mechanics_status: str = "unknown"

    def __post_init__(self) -> None:
        for name in (
            "market_observations_used",
            "market_fresh",
            "market_stale",
            "user_overrides_used",
            "station_fees_used",
            "station_fees_fresh",
            "station_fees_stale",
        ):
            _nonnegative_int(getattr(self, name), name)
        if not self.mechanics_status.strip():
            raise ValueError("mechanics_status is required")


@dataclass(frozen=True, slots=True)
class RefreshStatistics:
    keys_required: int = 0
    batches_planned: int = 0
    batches_completed: int = 0
    batches_failed: int = 0
    records_loaded: int = 0
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "keys_required",
            "batches_planned",
            "batches_completed",
            "batches_failed",
            "records_loaded",
        ):
            _nonnegative_int(getattr(self, name), name)
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("refresh elapsed_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    snapshot_id: str
    created_at: datetime
    completed_at: datetime
    region: Region
    constraints: FindMoneyConstraints
    actions: tuple[PlanAction, ...]
    total_pre_revenue_cash: ResourceAmount
    total_focus: ResourceAmount
    total_expected_profit: ResourceAmount
    silver_remaining: ResourceAmount
    focus_remaining: ResourceAmount
    plan_status: PlanStatus
    reasons: tuple[PlanReason, ...]
    optimizer: OptimizationDiagnostics
    catalog_source_version: str
    mechanics_ruleset_id: str
    assumptions: tuple[str, ...] = ()
    data_health: PlanDataHealth = field(default_factory=PlanDataHealth)
    current_refresh: RefreshStatistics = field(default_factory=RefreshStatistics)
    history_refresh: RefreshStatistics = field(default_factory=RefreshStatistics)
    oldest_market_observed_at: datetime | None = None
    oldest_station_observed_at: datetime | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    snapshot_format_version: int = SNAPSHOT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip():
            raise ValueError("snapshot_id is required")
        if self.snapshot_format_version not in {
            LEGACY_SNAPSHOT_FORMAT_VERSION,
            V2_SNAPSHOT_FORMAT_VERSION,
            SNAPSHOT_FORMAT_VERSION,
        }:
            raise ValueError(
                f"unsupported plan snapshot format version {self.snapshot_format_version}"
            )
        _aware(self.created_at, "created_at")
        _aware(self.completed_at, "completed_at")
        if self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        if self.region is not self.constraints.region:
            raise ValueError("snapshot region must match constraints")
        for name in (
            "total_pre_revenue_cash",
            "total_focus",
            "silver_remaining",
            "focus_remaining",
        ):
            _nonnegative_int(getattr(self, name), name)
        _integer(self.total_expected_profit, "total_expected_profit")
        if not self.catalog_source_version.strip() or not self.mechanics_ruleset_id.strip():
            raise ValueError("catalog source version and mechanics ruleset are required")
        if any(not value.strip() for value in self.assumptions):
            raise ValueError("snapshot assumptions cannot be blank")
        if len({key for key, _ in self.metadata}) != len(self.metadata):
            raise ValueError("snapshot metadata keys must be unique")
        for name in ("oldest_market_observed_at", "oldest_station_observed_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)

    @classmethod
    def from_optimization(
        cls,
        *,
        snapshot_id: str,
        created_at: datetime,
        completed_at: datetime,
        constraints: FindMoneyConstraints,
        result: OptimizationResult,
        catalog_source_version: str,
        mechanics_ruleset_id: str,
        assumptions: tuple[str, ...] = (),
        data_health: PlanDataHealth | None = None,
        current_refresh: RefreshStatistics | None = None,
        history_refresh: RefreshStatistics | None = None,
        metadata: tuple[tuple[str, str], ...] = (),
    ) -> PlanSnapshot:
        market_times = [
            action.oldest_market_observed_at
            for action in result.actions
            if action.oldest_market_observed_at is not None
        ]
        station_times = [
            action.station_fee_observed_at
            for action in result.actions
            if action.station_fee_observed_at is not None
        ]
        return cls(
            snapshot_id=snapshot_id,
            created_at=created_at,
            completed_at=completed_at,
            region=constraints.region,
            constraints=constraints,
            actions=result.actions,
            total_pre_revenue_cash=result.total_pre_revenue_cash,
            total_focus=result.total_focus,
            total_expected_profit=result.total_expected_profit,
            silver_remaining=result.silver_remaining,
            focus_remaining=result.focus_remaining,
            plan_status=result.plan_status,
            reasons=result.reasons,
            optimizer=result.diagnostics,
            catalog_source_version=catalog_source_version,
            mechanics_ruleset_id=mechanics_ruleset_id,
            assumptions=assumptions,
            data_health=data_health or PlanDataHealth(),
            current_refresh=current_refresh or RefreshStatistics(),
            history_refresh=history_refresh or RefreshStatistics(),
            oldest_market_observed_at=min(market_times) if market_times else None,
            oldest_station_observed_at=min(station_times) if station_times else None,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-safe snapshot envelope."""

        return {
            "snapshot_format_version": self.snapshot_format_version,
            "snapshot_id": self.snapshot_id,
            "created_at": _datetime_text(self.created_at),
            "completed_at": _datetime_text(self.completed_at),
            "region": self.region.value,
            "constraints": self.constraints.to_dict(format_version=self.snapshot_format_version),
            "actions": [
                _action_to_dict(
                    action,
                    format_version=self.snapshot_format_version,
                )
                for action in self.actions
            ],
            "totals": {
                "pre_revenue_cash": self.total_pre_revenue_cash,
                "focus": self.total_focus,
                "expected_profit": self.total_expected_profit,
                "silver_remaining": self.silver_remaining,
                "focus_remaining": self.focus_remaining,
            },
            "plan_status": self.plan_status.value,
            "reasons": [_reason_to_dict(reason) for reason in self.reasons],
            "optimizer": _optimizer_to_dict(self.optimizer),
            "catalog_source_version": self.catalog_source_version,
            "mechanics_ruleset_id": self.mechanics_ruleset_id,
            "assumptions": list(self.assumptions),
            "data_health": _data_health_to_dict(self.data_health),
            "current_refresh": _refresh_to_dict(self.current_refresh),
            "history_refresh": _refresh_to_dict(self.history_refresh),
            "oldest_market_observed_at": _optional_datetime_text(self.oldest_market_observed_at),
            "oldest_station_observed_at": _optional_datetime_text(self.oldest_station_observed_at),
            "metadata": [[key, value] for key, value in sorted(self.metadata)],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanSnapshot:
        version = int(value.get("snapshot_format_version", 0))
        if version not in {
            LEGACY_SNAPSHOT_FORMAT_VERSION,
            V2_SNAPSHOT_FORMAT_VERSION,
            SNAPSHOT_FORMAT_VERSION,
        }:
            raise ValueError(f"unsupported plan snapshot format version {version}")
        totals = value["totals"]
        return cls(
            snapshot_id=str(value["snapshot_id"]),
            created_at=_parse_datetime(value["created_at"]),
            completed_at=_parse_datetime(value["completed_at"]),
            region=Region(value["region"]),
            constraints=FindMoneyConstraints.from_dict(
                value["constraints"],
                format_version=version,
            ),
            actions=tuple(
                _action_from_dict(item, format_version=version) for item in value["actions"]
            ),
            total_pre_revenue_cash=int(totals["pre_revenue_cash"]),
            total_focus=int(totals["focus"]),
            total_expected_profit=int(totals["expected_profit"]),
            silver_remaining=int(totals["silver_remaining"]),
            focus_remaining=int(totals["focus_remaining"]),
            plan_status=PlanStatus(value["plan_status"]),
            reasons=tuple(_reason_from_dict(item) for item in value.get("reasons", ())),
            optimizer=_optimizer_from_dict(value["optimizer"]),
            catalog_source_version=str(value["catalog_source_version"]),
            mechanics_ruleset_id=str(value["mechanics_ruleset_id"]),
            assumptions=tuple(str(item) for item in value.get("assumptions", ())),
            data_health=_data_health_from_dict(value.get("data_health", {})),
            current_refresh=_refresh_from_dict(value.get("current_refresh", {})),
            history_refresh=_refresh_from_dict(value.get("history_refresh", {})),
            oldest_market_observed_at=_optional_parse_datetime(
                value.get("oldest_market_observed_at")
            ),
            oldest_station_observed_at=_optional_parse_datetime(
                value.get("oldest_station_observed_at")
            ),
            metadata=tuple((str(key), str(item)) for key, item in value.get("metadata", ())),
            snapshot_format_version=version,
        )


def liquidity_rank(level: LiquidityLevel) -> int:
    return {
        LiquidityLevel.UNKNOWN: 0,
        LiquidityLevel.LOW: 1,
        LiquidityLevel.MODERATE: 2,
        LiquidityLevel.HIGH: 3,
    }[level]


def _route_to_dict(
    route: CandidateRoute,
    *,
    format_version: int = SNAPSHOT_FORMAT_VERSION,
) -> dict[str, Any]:
    result = {
        "region": route.region.value,
        "material_city": route.material_city,
        (
            "craft_city" if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION else "production_city"
        ): route.production_city,
        "sell_city": route.sell_city,
        "transport_policy": route.transport_policy.value,
        (
            "transport_cost_per_craft"
            if format_version <= V2_SNAPSHOT_FORMAT_VERSION
            else "transport_cost_per_action_unit"
        ): route.transport_cost_per_craft,
        "reasons": [_reason_to_dict(reason) for reason in route.reasons],
    }
    if format_version >= SNAPSHOT_FORMAT_VERSION:
        result["buy_city"] = route.buy_city
    return result


def _route_from_dict(value: Mapping[str, Any]) -> CandidateRoute:
    return CandidateRoute(
        region=Region(value["region"]),
        material_city=str(value["material_city"]),
        craft_city=str(value.get("production_city", value.get("craft_city", ""))),
        sell_city=str(value["sell_city"]),
        transport_policy=TransportPolicy(value["transport_policy"]),
        transport_cost_per_craft=int(
            value.get("transport_cost_per_action_unit", value.get("transport_cost_per_craft", 0))
        ),
        reasons=tuple(_reason_from_dict(item) for item in value.get("reasons", ())),
    )


def _action_to_dict(
    action: PlanAction,
    *,
    format_version: int = SNAPSHOT_FORMAT_VERSION,
) -> dict[str, Any]:
    key = action.execution_capacity_key
    result = {
        "candidate_id": action.candidate_id,
        "item_id": action.item_id,
        "display_name": action.display_name,
        "route": _route_to_dict(action.route, format_version=format_version),
        "quantity": action.quantity,
        "focused_quantity": action.focused_quantity,
        "nonfocused_quantity": action.nonfocused_quantity,
        "output_units": action.output_units,
        "quality": action.quality,
        "sale_method": action.sale_method.value,
        "pre_revenue_cash_required": action.pre_revenue_cash_required,
        "focus_required": action.focus_required,
        "expected_profit": action.expected_profit,
        "liquidity": action.liquidity.value,
        "execution_capacity_key": [key[0].value, key[1], key[2], key[3]],
        "quantity_ceiling": action.quantity_ceiling,
        "execution_ceiling_output_units": action.execution_ceiling_output_units,
        "expected_revenue": action.expected_revenue,
        "effective_economic_cost": action.effective_economic_cost,
        "incremental_focus_profit": action.incremental_focus_profit,
        "reasons": [_reason_to_dict(reason) for reason in action.reasons],
        "evidence": [[key, item] for key, item in sorted(action.evidence)],
        "oldest_market_observed_at": _optional_datetime_text(action.oldest_market_observed_at),
        "station_fee_observed_at": _optional_datetime_text(action.station_fee_observed_at),
    }
    if format_version >= V2_SNAPSHOT_FORMAT_VERSION:
        result["action_kind"] = action.action_kind.value
    if format_version >= SNAPSHOT_FORMAT_VERSION:
        result["capacity_requirements"] = [
            {
                "key": [
                    requirement.key[0].value,
                    requirement.key[1],
                    requirement.key[2],
                    requirement.key[3],
                ],
                "role": requirement.role.value,
                "units_per_action_unit": requirement.units_per_action_unit,
            }
            for requirement in sorted(
                action.capacity_requirements,
                key=lambda value: value.canonical_key,
            )
        ]
    return result


def _action_from_dict(
    value: Mapping[str, Any],
    *,
    format_version: int = SNAPSHOT_FORMAT_VERSION,
) -> PlanAction:
    raw_key = value["execution_capacity_key"]
    execution_key = (
        Region(raw_key[0]),
        str(raw_key[1]),
        str(raw_key[2]),
        int(raw_key[3]),
    )
    capacity_requirements = (
        tuple(
            CapacityRequirement(
                (
                    Region(item["key"][0]),
                    str(item["key"][1]),
                    str(item["key"][2]),
                    int(item["key"][3]),
                ),
                CapacityRole(item["role"]),
                int(item["units_per_action_unit"]),
            )
            for item in value["capacity_requirements"]
        )
        if format_version >= SNAPSHOT_FORMAT_VERSION
        else ()
    )
    return PlanAction(
        candidate_id=str(value["candidate_id"]),
        item_id=str(value["item_id"]),
        display_name=str(value["display_name"]),
        route=_route_from_dict(value["route"]),
        quantity=int(value["quantity"]),
        focused_quantity=int(value["focused_quantity"]),
        nonfocused_quantity=int(value["nonfocused_quantity"]),
        output_units=int(value["output_units"]),
        quality=int(value["quality"]),
        sale_method=SaleMethod(value["sale_method"]),
        pre_revenue_cash_required=int(value["pre_revenue_cash_required"]),
        focus_required=int(value["focus_required"]),
        expected_profit=int(value["expected_profit"]),
        liquidity=LiquidityLevel(value["liquidity"]),
        execution_capacity_key=execution_key,
        quantity_ceiling=int(value["quantity_ceiling"]),
        action_kind=(
            ActionKind.CRAFT
            if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION
            else ActionKind(value["action_kind"])
        ),
        capacity_requirements=capacity_requirements,
        execution_ceiling_output_units=_optional_int(value.get("execution_ceiling_output_units")),
        expected_revenue=_optional_int(value.get("expected_revenue")),
        effective_economic_cost=_optional_int(value.get("effective_economic_cost")),
        incremental_focus_profit=_optional_int(value.get("incremental_focus_profit")),
        reasons=tuple(_reason_from_dict(item) for item in value.get("reasons", ())),
        evidence=tuple((str(key), str(item)) for key, item in value.get("evidence", ())),
        oldest_market_observed_at=_optional_parse_datetime(value.get("oldest_market_observed_at")),
        station_fee_observed_at=_optional_parse_datetime(value.get("station_fee_observed_at")),
    )


def _reason_to_dict(reason: PlanReason) -> dict[str, str]:
    return {
        "code": reason.code.value,
        "message": reason.message,
        "severity": reason.severity.value,
    }


def _reason_from_dict(value: Mapping[str, Any]) -> PlanReason:
    return PlanReason(
        PlanReasonCode(value["code"]),
        str(value["message"]),
        PlanReasonSeverity(value["severity"]),
    )


def _optimizer_to_dict(value: OptimizationDiagnostics) -> dict[str, Any]:
    result: dict[str, Any] = {
        "method": value.method,
        "status": value.status.value,
        "candidate_count": value.candidate_count,
        "group_count": value.group_count,
        "quantity_decision_count": value.quantity_decision_count,
        "states_considered": value.states_considered,
        "states_pruned": value.states_pruned,
        "state_limit": value.state_limit,
        "state_limit_reached": value.state_limit_reached,
        "elapsed_seconds": value.elapsed_seconds,
        "quantization_policy": value.quantization_policy,
    }
    optional_values: tuple[tuple[str, Any], ...] = (
        ("quantity_states_generated", value.quantity_states_generated),
        ("quantity_states_after_pruning", value.quantity_states_after_pruning),
        ("portfolio_states_considered", value.portfolio_states_considered),
        ("portfolio_states_pruned", value.portfolio_states_pruned),
        ("peak_frontier_size", value.peak_frontier_size),
        ("candidate_routes_before_pruning", value.candidate_routes_before_pruning),
        ("candidate_routes_after_pruning", value.candidate_routes_after_pruning),
        ("candidate_local_modes_removed", value.candidate_local_modes_removed),
        ("equivalent_routes_collapsed", value.equivalent_routes_collapsed),
        ("approximation_reasons", list(value.approximation_reasons)),
        ("effective_state_limit", value.effective_state_limit),
        ("quantity_transition_limit", value.quantity_transition_limit),
        ("portfolio_transition_limit", value.portfolio_transition_limit),
    )
    for key, item in optional_values:
        if item not in (0, [], ()):
            result[key] = item
    return result


def _optimizer_from_dict(value: Mapping[str, Any]) -> OptimizationDiagnostics:
    return OptimizationDiagnostics(
        method=str(value["method"]),
        status=OptimizationStatus(value["status"]),
        candidate_count=int(value["candidate_count"]),
        group_count=int(value["group_count"]),
        quantity_decision_count=int(value["quantity_decision_count"]),
        states_considered=int(value["states_considered"]),
        states_pruned=int(value["states_pruned"]),
        state_limit=int(value["state_limit"]),
        state_limit_reached=bool(value["state_limit_reached"]),
        elapsed_seconds=float(value["elapsed_seconds"]),
        quantization_policy=str(
            value.get(
                "quantization_policy",
                "whole_units:resource_ceiling,profit_floor,budgets_integral:v1",
            )
        ),
        quantity_states_generated=int(value.get("quantity_states_generated", 0)),
        quantity_states_after_pruning=int(value.get("quantity_states_after_pruning", 0)),
        portfolio_states_considered=int(value.get("portfolio_states_considered", 0)),
        portfolio_states_pruned=int(value.get("portfolio_states_pruned", 0)),
        peak_frontier_size=int(value.get("peak_frontier_size", 0)),
        candidate_routes_before_pruning=int(value.get("candidate_routes_before_pruning", 0)),
        candidate_routes_after_pruning=int(value.get("candidate_routes_after_pruning", 0)),
        candidate_local_modes_removed=int(value.get("candidate_local_modes_removed", 0)),
        equivalent_routes_collapsed=int(value.get("equivalent_routes_collapsed", 0)),
        approximation_reasons=tuple(str(item) for item in value.get("approximation_reasons", ())),
        effective_state_limit=int(value.get("effective_state_limit", 0)),
        quantity_transition_limit=int(value.get("quantity_transition_limit", 0)),
        portfolio_transition_limit=int(value.get("portfolio_transition_limit", 0)),
    )


def _data_health_to_dict(value: PlanDataHealth) -> dict[str, Any]:
    return {name: getattr(value, name) for name in value.__dataclass_fields__}


def _data_health_from_dict(value: Mapping[str, Any]) -> PlanDataHealth:
    return PlanDataHealth(
        market_observations_used=int(value.get("market_observations_used", 0)),
        market_fresh=int(value.get("market_fresh", 0)),
        market_stale=int(value.get("market_stale", 0)),
        user_overrides_used=int(value.get("user_overrides_used", 0)),
        station_fees_used=int(value.get("station_fees_used", 0)),
        station_fees_fresh=int(value.get("station_fees_fresh", 0)),
        station_fees_stale=int(value.get("station_fees_stale", 0)),
        mechanics_status=str(value.get("mechanics_status", "unknown")),
    )


def _refresh_to_dict(value: RefreshStatistics) -> dict[str, Any]:
    return {name: getattr(value, name) for name in value.__dataclass_fields__}


def _refresh_from_dict(value: Mapping[str, Any]) -> RefreshStatistics:
    return RefreshStatistics(
        keys_required=int(value.get("keys_required", 0)),
        batches_planned=int(value.get("batches_planned", 0)),
        batches_completed=int(value.get("batches_completed", 0)),
        batches_failed=int(value.get("batches_failed", 0)),
        records_loaded=int(value.get("records_loaded", 0)),
        elapsed_seconds=float(value.get("elapsed_seconds", 0)),
    )


def _decimal(value: int | float | Decimal, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _integer(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer resource amount")


def _nonnegative_int(value: int, label: str) -> None:
    _integer(value, label)
    if value < 0:
        raise ValueError(f"{label} cannot be negative")


def _positive_int(value: int, label: str) -> None:
    _integer(value, label)
    if value < 1:
        raise ValueError(f"{label} must be positive")


def _validate_capacity_requirements(
    requirements: tuple[CapacityRequirement, ...],
    expected_liquidation_key: ExecutionCapacityKey,
) -> None:
    if not requirements:
        raise ValueError("at least one capacity requirement is required")
    keys = [requirement.key for requirement in requirements]
    if len(keys) != len(set(keys)):
        raise ValueError("capacity requirement keys must be unique per candidate/action")
    liquidation = tuple(
        requirement
        for requirement in requirements
        if requirement.role is CapacityRole.LIQUIDATION
        and requirement.key == expected_liquidation_key
    )
    if len(liquidation) != 1:
        raise ValueError("one liquidation capacity must match the action sell market")


def _validate_arbitrage_capacity_requirements(
    requirements: tuple[CapacityRequirement, ...],
    route: CandidateRoute,
    item_id: str,
    quality: int,
) -> None:
    if route.buy_city.casefold() == route.sell_city.casefold():
        raise ValueError("arbitrage source and destination cities must differ")
    if route.production_city.casefold() != route.buy_city.casefold():
        raise ValueError("arbitrage route compatibility city must equal its source city")
    expected = {
        CapacityRole.ACQUISITION: (route.region, item_id, route.buy_city, quality),
        CapacityRole.LIQUIDATION: (route.region, item_id, route.sell_city, quality),
    }
    by_role = {requirement.role: requirement for requirement in requirements}
    if len(requirements) != 2 or set(by_role) != set(expected):
        raise ValueError("arbitrage requires one acquisition and one liquidation capacity")
    if any(
        by_role[role].key != key or by_role[role].units_per_action_unit != 1
        for role, key in expected.items()
    ):
        raise ValueError("arbitrage capacity keys must match source and destination market units")


def _validate_cities(
    values: tuple[str, ...],
    label: str,
    *,
    allow_duplicates: bool = False,
) -> None:
    if not values or any(not value.strip() for value in values):
        raise ValueError(f"{label} must contain non-blank city names")
    if not allow_duplicates and len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{label} cannot contain duplicates")


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_datetime_text(value: datetime | None) -> str | None:
    return None if value is None else _datetime_text(value)


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    _aware(parsed, "serialized datetime")
    return parsed.astimezone(UTC)


def _optional_parse_datetime(value: Any) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
