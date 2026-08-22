from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from albion_crafter.core.crafting_profile import (
    CraftingSkillProfile,
    FocusEfficiencyResolution,
    focus_skill_mapping_for_recipe,
)
from albion_crafter.core.freshness import Freshness, FreshnessPolicy
from albion_crafter.core.mechanics import CURRENT_RULES, MechanicsRules
from albion_crafter.core.models import ActionKind, Item, Recipe, SaleMethod
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType, station_type_for_item
from albion_crafter.database.catalog import CatalogImport, CatalogRepository
from albion_crafter.database.database import MarketPriceRepository, PriceOverrideRepository
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.market.aodp import plan_price_requests
from albion_crafter.market.history import HistoryTimeScale
from albion_crafter.market.models import MarketPrice, MarketSide, Region, UserPriceOverride

from .models import (
    ArbitrageScope,
    CandidateRoute,
    FindMoneyConstraints,
    MarketKey,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PriceRequirement,
    PriceRole,
)
from .routes import (
    RouteGenerationCancelled,
    RouteGenerationResult,
    generate_arbitrage_routes,
    generate_candidate_routes,
)
from .workload import (
    DEFAULT_PLANNING_WORKLOAD_POLICY,
    PlanningWorkloadAssessment,
    assess_planning_workload,
)


class ObservationDisposition(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    FUTURE = "future"
    MISSING = "missing"
    OVERRIDE = "user_override"


@dataclass(frozen=True, slots=True)
class PriceRequirementAssessment:
    requirement: PriceRequirement
    disposition: ObservationDisposition
    price: float | None
    observed_at: datetime | None
    provenance: Provenance
    freshness: Freshness
    needs_refresh: bool
    effective_override: bool = False


@dataclass(frozen=True, slots=True)
class PlannedAODPBatch:
    region: Region
    city: str
    quality: int
    item_ids: tuple[str, ...]
    url_length_bytes: int
    cities: tuple[str, ...] = ()

    @property
    def request_cities(self) -> tuple[str, ...]:
        """Return every exact city covered by this rectangular request."""

        return self.cities or (self.city,)


@dataclass(frozen=True, slots=True)
class MarketRefreshPlan:
    assessments: tuple[PriceRequirementAssessment, ...]
    batches: tuple[PlannedAODPBatch, ...]
    force_refresh: bool

    @property
    def required_keys(self) -> tuple[MarketKey, ...]:
        return tuple(
            sorted(
                {
                    assessment.requirement.key
                    for assessment in self.assessments
                    if assessment.requirement.required_for_actionability
                },
                key=_market_key_sort,
            )
        )

    @property
    def refresh_keys(self) -> tuple[MarketKey, ...]:
        return tuple(
            sorted(
                {
                    assessment.requirement.key
                    for assessment in self.assessments
                    if assessment.needs_refresh
                    and assessment.requirement.required_for_actionability
                },
                key=_market_key_sort,
            )
        )

    @property
    def estimated_batches(self) -> int:
        return len(self.batches)

    def disposition_count(self, disposition: ObservationDisposition) -> int:
        return sum(value.disposition is disposition for value in self.assessments)


@dataclass(frozen=True, slots=True)
class StationFeeRequirement:
    region: str
    city: str
    station_type: StationType
    observation: StationFeeObservation | None
    freshness: Freshness
    route_uses: int

    @property
    def needs_attention(self) -> bool:
        return self.observation is None or self.freshness in {
            Freshness.STALE,
            Freshness.FUTURE,
            Freshness.UNKNOWN,
        }


@dataclass(frozen=True, slots=True)
class FocusProfileRequirement:
    mapping_key: str
    crafting_group: str
    resolution: FocusEfficiencyResolution
    recipe_count: int

    @property
    def is_known(self) -> bool:
        return self.resolution.is_known

    @property
    def action_kind(self) -> ActionKind:
        return (
            ActionKind.REFINE if self.crafting_group.startswith("refining:") else ActionKind.CRAFT
        )


@dataclass(frozen=True, slots=True)
class EligibleRecipeRoute:
    recipe: Recipe
    route: CandidateRoute
    station_fee: StationFeeObservation
    station_freshness: Freshness
    focus_resolution: FocusEfficiencyResolution
    focused_variant_eligible: bool
    reasons: tuple[PlanReason, ...] = ()

    @property
    def action_kind(self) -> ActionKind:
        return self.recipe.action_kind


@dataclass(frozen=True, slots=True)
class EligibleArbitrageRoute:
    item: Item
    route: CandidateRoute
    source_price_side: MarketSide = MarketSide.SELL_ORDER
    destination_price_side: MarketSide = MarketSide.SELL_ORDER

    @property
    def action_kind(self) -> ActionKind:
        return ActionKind.ARBITRAGE


@dataclass(frozen=True, slots=True)
class PreflightSummary:
    candidate_recipes: int
    candidate_routes: int
    eligible_recipe_routes: int
    crafting_groups: int
    craft_cities: int
    sell_cities: int
    material_cities: int
    required_current_price_keys: int
    fresh_cached_requirements: int
    aging_cached_requirements: int
    refresh_requirements: int
    estimated_aodp_batches: int
    required_station_fee_keys: int
    fresh_station_fees: int
    missing_or_stale_station_fees: int
    focus_profiles_required: int
    known_focus_profiles: int
    unknown_focus_profiles: int
    conceptual_quantity_states: int
    quantity_bundle_count: int
    estimated_portfolio_frontier_work: int
    crafting_recipes: int = 0
    refining_recipes: int = 0
    crafting_recipe_routes: int = 0
    refining_recipe_routes: int = 0
    arbitrage_items: int = 0
    arbitrage_routes: int = 0
    total_candidates: int = 0
    selected_action_kinds: tuple[ActionKind, ...] = ()
    arbitrage_scope: ArbitrageScope = ArbitrageScope.ALL_PRODUCTION_OUTPUTS
    arbitrage_source_cities: tuple[str, ...] = ()
    arbitrage_destination_cities: tuple[str, ...] = ()
    stale_current_requirements: int = 0
    missing_current_requirements: int = 0
    future_current_requirements: int = 0
    history_capacity_keys: int = 0
    arbitrage_source_history_keys: int = 0
    arbitrage_destination_history_keys: int = 0
    cached_history_keys: int = 0
    history_gaps: int = 0
    maximum_shortlisted_history_keys: int = 0
    estimated_history_city_groups: int = 0
    estimated_capacity_components: int = 0
    frontier_state_limit: int = 0
    quantity_transition_limit: int = 0
    portfolio_transition_limit: int = 0
    supported_catalog_recipes: int = 0
    unsupported_catalog_recipes: int = 0
    matched_recipes: int = 0
    static_supported_matching_recipes: int = 0
    catalog_unknown_item_value: int = 0
    catalog_unknown_station_type: int = 0
    catalog_unknown_returnability: int = 0
    catalog_ambiguous_recipes: int = 0
    catalog_untrusted_recipes: int = 0


@dataclass(frozen=True, slots=True)
class FindMoneyPreflight:
    created_at: datetime
    constraints: FindMoneyConstraints
    catalog: CatalogImport | None
    routes: RouteGenerationResult
    arbitrage_route_universe: RouteGenerationResult
    eligible: tuple[EligibleRecipeRoute, ...]
    arbitrage_routes: tuple[EligibleArbitrageRoute, ...]
    station_requirements: tuple[StationFeeRequirement, ...]
    focus_requirements: tuple[FocusProfileRequirement, ...]
    market_refresh: MarketRefreshPlan
    workload: PlanningWorkloadAssessment
    rejection_counts: tuple[tuple[str, int], ...]
    blockers: tuple[PlanReason, ...]
    summary: PreflightSummary
    database_read_statements: int

    @property
    def has_eligible_routes(self) -> bool:
        return bool(self.eligible or self.arbitrage_routes)

    @property
    def attention_station_fees(self) -> tuple[StationFeeRequirement, ...]:
        return tuple(value for value in self.station_requirements if value.needs_attention)


BatchPlanner = Callable[[tuple[MarketKey, ...]], tuple[PlannedAODPBatch, ...]]


class FindMoneyPreflightPlanner:
    """Build a complete read-only data plan before any network request."""

    def __init__(
        self,
        catalog: CatalogRepository,
        market_prices: MarketPriceRepository,
        overrides: PriceOverrideRepository,
        station_fees: StationFeeRepository,
        crafting_profiles: CraftingProfileRepository,
        history: MarketHistoryRepository | None = None,
        *,
        rules: MechanicsRules = CURRENT_RULES,
        batch_planner: BatchPlanner | None = None,
    ) -> None:
        self.catalog = catalog
        self.market_prices = market_prices
        self.overrides = overrides
        self.station_fees = station_fees
        self.crafting_profiles = crafting_profiles
        self.history = history
        self.rules = rules
        self.batch_planner = batch_planner

    def build(
        self,
        constraints: FindMoneyConstraints,
        *,
        as_of: datetime | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> FindMoneyPreflight:
        created_at = as_of or datetime.now(UTC)
        if created_at.tzinfo is None:
            raise ValueError("preflight as_of must be timezone-aware")

        all_recipes = self.catalog.list_recipes(
            constraints.item_query,
            tier_min=min(constraints.tiers),
            tier_max=max(constraints.tiers),
            enchantments=tuple(sorted(constraints.enchantments)),
        )
        selected_crafting_categories = {value.casefold() for value in constraints.categories}
        recipes = [
            recipe
            for recipe in all_recipes
            if recipe.output.tier in constraints.tiers
            and recipe.output.enchantment in constraints.enchantments
            and recipe.action_kind in constraints.action_kinds
            and (
                (
                    recipe.action_kind is ActionKind.CRAFT
                    and (
                        not constraints.categories
                        or recipe.output.crafting_category.casefold()
                        in selected_crafting_categories
                    )
                )
                or (
                    recipe.action_kind is ActionKind.REFINE
                    and recipe.output.crafting_category.casefold() in constraints.refining_families
                )
            )
        ]
        arbitrage_items_by_id: dict[str, Item] = {}
        if ActionKind.ARBITRAGE in constraints.action_kinds:
            for recipe in all_recipes:
                item = recipe.output
                if (
                    item.tier not in constraints.tiers
                    or item.enchantment not in constraints.enchantments
                ):
                    continue
                if constraints.categories and (
                    item.crafting_category.casefold() not in selected_crafting_categories
                ):
                    continue
                if (
                    constraints.arbitrage_scope is ArbitrageScope.CRAFTED_OUTPUTS
                    and recipe.action_kind is not ActionKind.CRAFT
                ) or (
                    constraints.arbitrage_scope is ArbitrageScope.REFINED_RESOURCES
                    and recipe.action_kind is not ActionKind.REFINE
                ):
                    continue
                if (
                    recipe.action_kind is ActionKind.REFINE
                    and item.crafting_category.casefold() not in constraints.refining_families
                ):
                    continue
                arbitrage_items_by_id[item.item_id] = item
        catalog_metadata = self.catalog.import_metadata()
        catalog_coverage = self.catalog.recipe_coverage()
        route_result = generate_candidate_routes(constraints, cancelled=cancelled)
        arbitrage_route_result = generate_arbitrage_routes(constraints, cancelled=cancelled)
        fees = self.station_fees.list_all(constraints.region)
        stored_profile = self.crafting_profiles.load()
        profile = stored_profile or CraftingSkillProfile(
            available_focus=constraints.available_focus
        )
        database_reads = 2 + 1 + 1 + 1 + (1 if stored_profile is None else 3)

        fee_index = {value.key: value for value in fees}
        station_policy = FreshnessPolicy(constraints.max_station_fee_age)
        rejection_counts: Counter[str] = Counter()
        rejection_counts.update(
            {code.value: count for code, count in route_result.rejection_counts}
        )
        rejection_counts.update(
            {code.value: count for code, count in arbitrage_route_result.rejection_counts}
        )
        station_use_counts: Counter[tuple[str, str, StationType]] = Counter()
        station_states: dict[
            tuple[str, str, StationType], tuple[StationFeeObservation | None, Freshness]
        ] = {}
        focus_counts: Counter[str] = Counter()
        focus_resolutions: dict[str, FocusEfficiencyResolution] = {}
        focus_groups: dict[str, str] = {}
        eligible: list[EligibleRecipeRoute] = []
        arbitrage_routes: list[EligibleArbitrageRoute] = []
        requirements: set[PriceRequirement] = set()
        crafting_groups: set[str] = set()
        static_supported_recipe_ids: set[str] = set()

        for recipe in recipes:
            static_problem = self._static_recipe_problem(recipe)
            if static_problem is not None:
                rejection_counts[static_problem] += len(route_result.routes)
                continue
            station_type = station_type_for_item(recipe.output)
            if station_type is None:
                rejection_counts["unknown_station_type"] += len(route_result.routes)
                continue
            static_supported_recipe_ids.add(recipe.output.item_id)
            mapping = focus_skill_mapping_for_recipe(recipe)
            focus_resolution = profile.resolve(mapping)
            if mapping is not None:
                focus_counts[mapping.mapping_key] += 1
                focus_resolutions[mapping.mapping_key] = focus_resolution
                focus_groups[mapping.mapping_key] = mapping.crafting_group
                crafting_groups.add(mapping.crafting_group)
            elif recipe.output.crafting_category:
                crafting_groups.add(recipe.output.crafting_category)

            for route in route_result.routes:
                bonus = self.rules.production_bonus_resolution(
                    recipe.action_kind,
                    recipe.output,
                    route.production_city,
                    use_focus=False,
                )
                if not bonus.is_verified:
                    rejection_counts["unsupported_city_bonus"] += 1
                    continue
                station_key = (constraints.region.value, route.craft_city, station_type)
                station_use_counts[station_key] += 1
                observation = fee_index.get(station_key)
                freshness = station_policy.classify(
                    observation.observed_at if observation is not None else None,
                    now=created_at,
                )
                station_states[station_key] = (observation, freshness)
                if observation is None:
                    rejection_counts["missing_station_fee"] += 1
                    continue
                reasons = list(route.reasons)
                if freshness is Freshness.FUTURE:
                    rejection_counts["future_station_fee"] += 1
                    continue
                if freshness in {Freshness.STALE, Freshness.UNKNOWN}:
                    if not constraints.allow_stale_station_fees:
                        rejection_counts["stale_station_fee"] += 1
                        continue
                    reasons.append(
                        PlanReason(
                            PlanReasonCode.STALE_STATION_FEE,
                            f"{route.craft_city} {station_type.display_name} fee is "
                            f"{freshness.value}; stale assumptions make the route advisory.",
                            PlanReasonSeverity.WARNING,
                        )
                    )

                focused_eligible = bool(
                    constraints.use_focus
                    and recipe.base_focus_cost is not None
                    and recipe.base_focus_cost > 0
                    and focus_resolution.is_known
                )
                if constraints.use_focus and not focused_eligible:
                    rejection_counts["focused_variant_unknown_fce"] += 1
                candidate = EligibleRecipeRoute(
                    recipe=recipe,
                    route=route,
                    station_fee=observation,
                    station_freshness=freshness,
                    focus_resolution=focus_resolution,
                    focused_variant_eligible=focused_eligible,
                    reasons=tuple(reasons),
                )
                eligible.append(candidate)
                requirements.update(self._requirements_for(candidate, constraints))

        destination_side = (
            MarketSide.SELL_ORDER
            if constraints.sale_method is SaleMethod.SELL_ORDER
            else MarketSide.BUY_ORDER
        )
        for item_index, item in enumerate(
            sorted(arbitrage_items_by_id.values(), key=lambda value: value.item_id)
        ):
            if item_index % 128 == 0 and cancelled is not None and cancelled():
                raise RouteGenerationCancelled("arbitrage preflight was cancelled")
            for route in arbitrage_route_result.routes:
                candidate = EligibleArbitrageRoute(
                    item,
                    route,
                    MarketSide.SELL_ORDER,
                    destination_side,
                )
                arbitrage_routes.append(candidate)
                requirements.update(self._arbitrage_requirements_for(candidate, constraints))

        station_requirements = tuple(
            StationFeeRequirement(
                region=key[0],
                city=key[1],
                station_type=key[2],
                observation=station_states.get(key, (None, Freshness.UNKNOWN))[0],
                freshness=station_states.get(key, (None, Freshness.UNKNOWN))[1],
                route_uses=count,
            )
            for key, count in sorted(
                station_use_counts.items(),
                key=lambda value: (value[0][0], value[0][1].casefold(), value[0][2].value),
            )
        )
        focus_requirements = tuple(
            FocusProfileRequirement(
                mapping_key=key,
                crafting_group=focus_groups[key],
                resolution=focus_resolutions[key],
                recipe_count=count,
            )
            for key, count in sorted(focus_counts.items())
        )
        market_refresh, market_reads = self._build_market_plan(
            requirements,
            constraints,
            created_at,
        )
        database_reads += market_reads
        blockers: list[PlanReason] = []
        capacity_groups = {
            (
                constraints.region,
                value.recipe.output.item_id,
                value.route.sell_city,
                1,
            )
            for value in eligible
        }
        capacity_groups.update(
            (
                constraints.region,
                value.item.item_id,
                city,
                1,
            )
            for value in arbitrage_routes
            for city in (value.route.buy_city, value.route.sell_city)
        )
        capacity_requirement_sets = [
            (
                (
                    constraints.region,
                    value.recipe.output.item_id,
                    value.route.sell_city,
                    1,
                ),
            )
            for value in eligible
        ]
        capacity_requirement_sets.extend(
            (
                (
                    constraints.region,
                    value.item.item_id,
                    value.route.buy_city,
                    1,
                ),
                (
                    constraints.region,
                    value.item.item_id,
                    value.route.sell_city,
                    1,
                ),
            )
            for value in arbitrage_routes
        )
        arbitrage_source_history_keys = {
            (constraints.region, value.item.item_id, value.route.buy_city, 1)
            for value in arbitrage_routes
        }
        arbitrage_destination_history_keys = {
            (constraints.region, value.item.item_id, value.route.sell_city, 1)
            for value in arbitrage_routes
        }
        cached_history_keys = 0
        if self.history is not None and capacity_groups:
            coverage = self.history.list_coverage(
                constraints.region,
                tuple(sorted({value[1] for value in capacity_groups})),
                tuple(sorted({value[2] for value in capacity_groups}, key=str.casefold)),
                1,
                HistoryTimeScale.SIX_HOURLY,
            )
            database_reads += 1
            covered = {
                (value.region, value.item_id, value.city, value.quality)
                for value in coverage
                if value.status in {"success", "empty", "partial"}
            }
            cached_history_keys = len(capacity_groups & covered)
        workload = assess_planning_workload(
            candidate_routes=len(eligible) + len(arbitrage_routes),
            capacity_groups=len(capacity_groups),
            focused_routes=sum(value.focused_variant_eligible for value in eligible),
            requested_craft_cap=constraints.per_item_craft_cap,
        )
        if workload.warning is not None:
            blockers.append(
                PlanReason(
                    PlanReasonCode.APPROXIMATE_OPTIMIZATION,
                    workload.warning,
                    PlanReasonSeverity.WARNING,
                )
            )
        future_override_keys = {
            value.requirement.key
            for value in market_refresh.assessments
            if value.requirement.required_for_actionability
            and value.effective_override
            and value.freshness is Freshness.FUTURE
        }
        stale_override_keys = {
            value.requirement.key
            for value in market_refresh.assessments
            if value.requirement.required_for_actionability
            and value.effective_override
            and value.freshness in {Freshness.STALE, Freshness.UNKNOWN}
        }
        if future_override_keys:
            rejection_counts["future_user_override"] += len(future_override_keys)
            blockers.append(
                PlanReason(
                    PlanReasonCode.FUTURE_MARKET_DATA,
                    f"{len(future_override_keys):,} required user price override(s) are "
                    "future-dated beyond the tolerated two-minute clock skew. AODP refresh "
                    "cannot replace an override; update or remove the affected override before "
                    "expecting a decision-grade route.",
                    PlanReasonSeverity.WARNING,
                )
            )
        if stale_override_keys:
            rejection_counts["stale_user_override"] += len(stale_override_keys)
            blockers.append(
                PlanReason(
                    PlanReasonCode.STALE_MARKET_DATA,
                    f"{len(stale_override_keys):,} required user price override(s) are stale or "
                    "have unknown age. AODP refresh cannot replace an override; "
                    "update or remove the affected override before expecting a decision-grade "
                    "route.",
                    PlanReasonSeverity.WARNING,
                )
            )
        if catalog_metadata is None:
            blockers.append(
                PlanReason(PlanReasonCode.OTHER, "No production static catalog is active.")
            )
        if not eligible and not arbitrage_routes:
            blockers.append(
                PlanReason(
                    PlanReasonCode.NO_FEASIBLE_ACTIONS,
                    "No production or arbitrage route survives static, evidence, and transport "
                    "preflight.",
                )
            )

        required_assessments = tuple(
            value
            for value in market_refresh.assessments
            if value.requirement.required_for_actionability
        )
        current_key_states = _current_key_states(required_assessments)
        summary = PreflightSummary(
            candidate_recipes=len(recipes),
            candidate_routes=len(route_result.routes) + len(arbitrage_route_result.routes),
            eligible_recipe_routes=len(eligible) + len(arbitrage_routes),
            crafting_groups=len(crafting_groups),
            craft_cities=len(constraints.craft_cities),
            sell_cities=len(constraints.sell_cities),
            material_cities=len(constraints.material_cities),
            required_current_price_keys=len(market_refresh.required_keys),
            fresh_cached_requirements=sum(
                value == "fresh" for value in current_key_states.values()
            ),
            aging_cached_requirements=sum(
                value == "aging" for value in current_key_states.values()
            ),
            refresh_requirements=len(market_refresh.refresh_keys),
            estimated_aodp_batches=market_refresh.estimated_batches,
            required_station_fee_keys=len(station_requirements),
            fresh_station_fees=sum(
                value.freshness in {Freshness.FRESH, Freshness.AGING}
                for value in station_requirements
            ),
            missing_or_stale_station_fees=sum(
                value.needs_attention for value in station_requirements
            ),
            focus_profiles_required=len(focus_requirements),
            known_focus_profiles=sum(value.is_known for value in focus_requirements),
            unknown_focus_profiles=sum(not value.is_known for value in focus_requirements),
            conceptual_quantity_states=workload.conceptual_quantity_states,
            quantity_bundle_count=workload.quantity_bundle_count,
            estimated_portfolio_frontier_work=workload.estimated_portfolio_frontier_work,
            crafting_recipes=sum(recipe.action_kind is ActionKind.CRAFT for recipe in recipes),
            refining_recipes=sum(recipe.action_kind is ActionKind.REFINE for recipe in recipes),
            crafting_recipe_routes=sum(value.action_kind is ActionKind.CRAFT for value in eligible),
            refining_recipe_routes=sum(
                value.action_kind is ActionKind.REFINE for value in eligible
            ),
            arbitrage_items=len(arbitrage_items_by_id),
            arbitrage_routes=len(arbitrage_routes),
            total_candidates=len(eligible) + len(arbitrage_routes),
            selected_action_kinds=tuple(
                sorted(constraints.action_kinds, key=lambda value: value.value)
            ),
            arbitrage_scope=constraints.arbitrage_scope,
            arbitrage_source_cities=constraints.arbitrage_source_cities,
            arbitrage_destination_cities=constraints.arbitrage_destination_cities,
            stale_current_requirements=sum(
                value == "stale" for value in current_key_states.values()
            ),
            missing_current_requirements=sum(
                value == "missing" for value in current_key_states.values()
            ),
            future_current_requirements=sum(
                value == "future" for value in current_key_states.values()
            ),
            history_capacity_keys=len(capacity_groups),
            arbitrage_source_history_keys=len(arbitrage_source_history_keys),
            arbitrage_destination_history_keys=len(arbitrage_destination_history_keys),
            cached_history_keys=cached_history_keys,
            history_gaps=max(len(capacity_groups) - cached_history_keys, 0),
            maximum_shortlisted_history_keys=min(
                len(capacity_groups), constraints.history_shortlist_limit * 2
            ),
            estimated_history_city_groups=len({value[2] for value in capacity_groups}),
            estimated_capacity_components=_capacity_component_count(
                capacity_requirement_sets,
                cancelled=cancelled,
            ),
            frontier_state_limit=DEFAULT_PLANNING_WORKLOAD_POLICY.frontier_state_limit,
            quantity_transition_limit=(DEFAULT_PLANNING_WORKLOAD_POLICY.quantity_transition_limit),
            portfolio_transition_limit=(
                DEFAULT_PLANNING_WORKLOAD_POLICY.portfolio_transition_limit
            ),
            supported_catalog_recipes=catalog_coverage.supported,
            unsupported_catalog_recipes=catalog_coverage.unsupported,
            matched_recipes=len(recipes),
            static_supported_matching_recipes=len(static_supported_recipe_ids),
            catalog_unknown_item_value=catalog_coverage.unknown_item_value,
            catalog_unknown_station_type=catalog_coverage.unknown_station_type,
            catalog_unknown_returnability=catalog_coverage.unknown_returnability,
            catalog_ambiguous_recipes=catalog_coverage.ambiguous_recipe,
            catalog_untrusted_recipes=catalog_coverage.untrusted_recipe,
        )
        return FindMoneyPreflight(
            created_at=created_at,
            constraints=constraints,
            catalog=catalog_metadata,
            routes=route_result,
            arbitrage_route_universe=arbitrage_route_result,
            eligible=tuple(eligible),
            arbitrage_routes=tuple(arbitrage_routes),
            station_requirements=station_requirements,
            focus_requirements=focus_requirements,
            market_refresh=market_refresh,
            workload=workload,
            rejection_counts=tuple(sorted(rejection_counts.items())),
            blockers=tuple(blockers),
            summary=summary,
            database_read_statements=database_reads,
        )

    @staticmethod
    def _static_recipe_problem(recipe: Recipe) -> str | None:
        if recipe.provenance is not Provenance.STATIC_GAME_DATA:
            return "untrusted_recipe"
        if recipe.recipe_ambiguous:
            return "ambiguous_recipe"
        if recipe.item_value is None:
            return "unknown_item_value"
        if any(material.returnable is None for material in recipe.materials):
            return "unknown_returnability"
        return None

    @staticmethod
    def _requirements_for(
        candidate: EligibleRecipeRoute,
        constraints: FindMoneyConstraints,
    ) -> Iterable[PriceRequirement]:
        recipe = candidate.recipe
        route = candidate.route
        for material in recipe.materials:
            yield PriceRequirement(
                MarketKey(constraints.region, material.item_id, route.material_city, 1),
                MarketSide.SELL_ORDER,
                PriceRole.MATERIAL,
            )
            if material.returnable and route.material_city != route.craft_city:
                yield PriceRequirement(
                    MarketKey(constraints.region, material.item_id, route.craft_city, 1),
                    MarketSide.SELL_ORDER,
                    PriceRole.RETURNED_MATERIAL_INFORMATIONAL,
                    required_for_actionability=False,
                )
        output_side = (
            MarketSide.SELL_ORDER
            if constraints.sale_method is SaleMethod.SELL_ORDER
            else MarketSide.BUY_ORDER
        )
        yield PriceRequirement(
            MarketKey(constraints.region, recipe.output.item_id, route.sell_city, 1),
            output_side,
            PriceRole.OUTPUT,
        )

    @staticmethod
    def _arbitrage_requirements_for(
        candidate: EligibleArbitrageRoute,
        constraints: FindMoneyConstraints,
    ) -> Iterable[PriceRequirement]:
        yield PriceRequirement(
            MarketKey(
                constraints.region,
                candidate.item.item_id,
                candidate.route.buy_city,
                1,
            ),
            MarketSide.SELL_ORDER,
            PriceRole.ARBITRAGE_SOURCE,
        )
        yield PriceRequirement(
            MarketKey(
                constraints.region,
                candidate.item.item_id,
                candidate.route.sell_city,
                1,
            ),
            candidate.destination_price_side,
            PriceRole.ARBITRAGE_DESTINATION,
        )

    def _build_market_plan(
        self,
        requirements: set[PriceRequirement],
        constraints: FindMoneyConstraints,
        as_of: datetime,
    ) -> tuple[MarketRefreshPlan, int]:
        if not requirements:
            return MarketRefreshPlan((), (), constraints.force_current_price_refresh), 0
        keys = {requirement.key for requirement in requirements}
        item_ids = tuple(sorted({key.item_id for key in keys}))
        cities = tuple(sorted({key.city for key in keys}, key=str.casefold))
        qualities = tuple(sorted({key.quality for key in keys}))
        market_rows = self.market_prices.list_for_scan(
            constraints.region,
            cities=cities,
            qualities=qualities,
            item_ids=item_ids,
        )
        override_rows = self.overrides.list_for_scan(
            constraints.region,
            cities=cities,
            qualities=qualities,
            item_ids=item_ids,
        )
        market_index = {
            (value.region, value.item_id, value.city, value.quality): value for value in market_rows
        }
        override_index = {
            (value.region, value.item_id, value.city, value.quality, value.side): value
            for value in override_rows
        }
        policy = FreshnessPolicy(constraints.max_market_age)
        assessments = tuple(
            self._assess_requirement(
                requirement,
                market_index,
                override_index,
                policy,
                as_of,
                force=constraints.force_current_price_refresh,
            )
            for requirement in sorted(requirements, key=_requirement_sort)
        )
        refresh_keys = tuple(
            sorted(
                {
                    value.requirement.key
                    for value in assessments
                    if value.needs_refresh and value.requirement.required_for_actionability
                },
                key=_market_key_sort,
            )
        )
        batches = (
            self.batch_planner(refresh_keys)
            if self.batch_planner is not None
            else _default_batch_planner(refresh_keys)
        )
        fixed_parameters = 1 + len(cities) + len(qualities)
        chunk_size = max(1, 900 - fixed_parameters)
        database_reads = 2 * ((len(item_ids) + chunk_size - 1) // chunk_size)
        return (
            MarketRefreshPlan(
                assessments=assessments,
                batches=batches,
                force_refresh=constraints.force_current_price_refresh,
            ),
            database_reads,
        )

    @staticmethod
    def _assess_requirement(
        requirement: PriceRequirement,
        market_index: dict[tuple, MarketPrice],
        override_index: dict[tuple, UserPriceOverride],
        policy: FreshnessPolicy,
        as_of: datetime,
        *,
        force: bool,
    ) -> PriceRequirementAssessment:
        key = requirement.key
        override = override_index.get(
            (key.region, key.item_id, key.city, key.quality, requirement.side)
        )
        if override is not None:
            freshness = policy.classify(override.entered_at, now=as_of)
            return PriceRequirementAssessment(
                requirement,
                ObservationDisposition.OVERRIDE,
                float(override.price),
                override.entered_at,
                override.provenance,
                freshness,
                False,
                effective_override=True,
            )

        row = market_index.get((key.region, key.item_id, key.city, key.quality))
        price = row.price_for_side(requirement.side) if row is not None else None
        timestamp = row.timestamp_for_side(requirement.side) if row is not None else None
        if price is None or price <= 0:
            return PriceRequirementAssessment(
                requirement,
                ObservationDisposition.MISSING,
                None,
                None,
                row.provenance if row is not None else Provenance.UNKNOWN,
                Freshness.UNKNOWN,
                requirement.required_for_actionability,
            )
        freshness = policy.classify(timestamp, now=as_of)
        disposition = {
            Freshness.FRESH: ObservationDisposition.FRESH,
            Freshness.AGING: ObservationDisposition.AGING,
            Freshness.STALE: ObservationDisposition.STALE,
            Freshness.FUTURE: ObservationDisposition.FUTURE,
            Freshness.UNKNOWN: ObservationDisposition.MISSING,
        }[freshness]
        needs_refresh = requirement.required_for_actionability and (
            force or freshness in {Freshness.STALE, Freshness.FUTURE, Freshness.UNKNOWN}
        )
        return PriceRequirementAssessment(
            requirement,
            disposition,
            float(price),
            timestamp,
            row.provenance,
            freshness,
            needs_refresh,
        )


def _default_batch_planner(keys: tuple[MarketKey, ...]) -> tuple[PlannedAODPBatch, ...]:
    grouped: dict[tuple[Region, int], dict[str, set[str]]] = {}
    for key in keys:
        by_item = grouped.setdefault((key.region, key.quality), {})
        by_item.setdefault(key.item_id, set()).add(key.city)

    # AODP accepts multiple locations in one request. Items requiring the exact
    # same city set can therefore share a request without widening the sparse
    # item/city key set into an unused cross product.
    rectangles: list[tuple[Region, int, tuple[str, ...], tuple[str, ...]]] = []
    for (region, quality), by_item in grouped.items():
        items_by_cities: dict[tuple[str, ...], list[str]] = {}
        for item_id, cities in by_item.items():
            signature = tuple(sorted(cities, key=str.casefold))
            items_by_cities.setdefault(signature, []).append(item_id)
        for cities, item_ids in items_by_cities.items():
            rectangles.append(
                (
                    region,
                    quality,
                    cities,
                    tuple(sorted(item_ids)),
                )
            )

    result: list[PlannedAODPBatch] = []
    for region, quality, cities, item_ids in sorted(
        rectangles,
        key=lambda value: (
            value[0].value,
            value[1],
            tuple(city.casefold() for city in value[2]),
            value[3],
        ),
    ):
        request_plan = plan_price_requests(
            item_ids,
            region=region,
            cities=cities,
            qualities=(quality,),
        )
        for batch in request_plan.batches:
            result.append(
                PlannedAODPBatch(
                    region=region,
                    city=cities[0],
                    quality=quality,
                    item_ids=batch.item_ids,
                    url_length_bytes=batch.url_bytes,
                    cities=cities,
                )
            )
    return tuple(result)


def _market_key_sort(key: MarketKey) -> tuple[str, str, str, int]:
    return (key.region.value, key.city.casefold(), key.item_id, key.quality)


def _requirement_sort(
    requirement: PriceRequirement,
) -> tuple[str, str, str, int, str, str]:
    key = requirement.key
    return (
        key.region.value,
        key.city.casefold(),
        key.item_id,
        key.quality,
        requirement.side.value,
        requirement.role.value,
    )


def _capacity_component_count(
    requirement_sets: Iterable[tuple[tuple, ...]],
    *,
    cancelled: Callable[[], bool] | None,
) -> int:
    """Count potential market-capacity components without constructing candidates."""

    parents: dict[tuple, tuple] = {}

    def find(key: tuple) -> tuple:
        parent = parents.setdefault(key, key)
        while parent != key:
            grandparent = parents[parent]
            parents[key] = grandparent
            key, parent = parent, grandparent
        return key

    for index, requirements in enumerate(requirement_sets):
        if index % 256 == 0 and cancelled is not None and cancelled():
            raise RouteGenerationCancelled("capacity-component preflight was cancelled")
        if not requirements:
            continue
        first = find(requirements[0])
        for key in requirements[1:]:
            other = find(key)
            if first != other:
                parents[other] = first
    return len({find(key) for key in parents})


def _current_key_states(
    assessments: Iterable[PriceRequirementAssessment],
) -> dict[MarketKey, str]:
    """Collapse side/role requirements into mutually exclusive refresh-key states."""

    priority = {"fresh": 0, "aging": 1, "stale": 2, "missing": 3, "future": 4}
    result: dict[MarketKey, str] = {}
    for value in assessments:
        if value.freshness is Freshness.FUTURE:
            state = "future"
        elif value.price is None:
            state = "missing"
        elif value.freshness in {Freshness.STALE, Freshness.UNKNOWN}:
            state = "stale"
        elif value.freshness is Freshness.AGING:
            state = "aging"
        else:
            state = "fresh"
        key = value.requirement.key
        prior = result.get(key)
        if prior is None or priority[state] > priority[prior]:
            result[key] = state
    return result
