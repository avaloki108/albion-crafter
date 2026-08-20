from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime

from albion_crafter.core.actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
    ReasonSeverity,
)
from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.crafting_profile import (
    CraftingSkillProfile,
    crafting_skill_mapping_for_recipe,
)
from albion_crafter.core.mechanics import CURRENT_RULES, MechanicsRules
from albion_crafter.core.models import CraftingContext, Recipe, SaleMethod
from albion_crafter.core.stations import StationFeeObservation, resolve_station_fee
from albion_crafter.market.history import MarketHistoryInterval
from albion_crafter.market.liquidity import (
    LiquidityAssessment,
    LiquidityLevel,
    assess_liquidity,
)
from albion_crafter.market.models import (
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)

from .filtering import filter_recipes, opportunity_passes_filters, sort_opportunities
from .models import (
    CancellationToken,
    CraftOpportunity,
    ScanConstraints,
    ScanProgress,
    ScanSnapshot,
)
from .pricing import PricingIndex

ProgressCallback = Callable[[ScanProgress], None]
LiquidityKey = tuple[Region, str, str, int]
_SCAN_NOTES = (
    "Normal quality is the only decision-grade crafting mode; "
    "higher qualities block actionability.",
    "Current AODP values are top-of-book unit observations; order depth is not modeled.",
    "Daily AODP sell history may provide a labeled estimate when a current SELL price is "
    "missing or stale; BUY prices never use history.",
)


class OpportunityScanner:
    """Pure, GUI-independent scenario evaluator over preloaded data.

    The scanner never opens SQLite and never performs HTTP. Repository/network
    orchestration belongs to ``OpportunityScannerService``; this class is also
    the performance-test seam for thousands of recipes.
    """

    def __init__(self, rules: MechanicsRules = CURRENT_RULES) -> None:
        self.rules = rules
        self.calculator = CraftCalculator(rules)

    def scan(
        self,
        recipes: Iterable[Recipe],
        market_prices: Iterable[MarketPrice],
        overrides: Iterable[UserPriceOverride],
        station_fees: Iterable[StationFeeObservation],
        crafting_profile: CraftingSkillProfile,
        constraints: ScanConstraints,
        *,
        liquidity_by_key: Mapping[LiquidityKey, LiquidityAssessment] | None = None,
        history_by_key: Mapping[LiquidityKey, tuple[MarketHistoryInterval, ...]] | None = None,
        history_status_by_key: Mapping[LiquidityKey, str] | None = None,
        price_history: Iterable[MarketHistoryInterval] = (),
        as_of: datetime | None = None,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
        database_load_operations: int = 0,
    ) -> ScanSnapshot:
        started = time.perf_counter()
        scan_time = as_of or datetime.now(UTC)
        if scan_time.tzinfo is None:
            raise ValueError("scan as_of must be timezone-aware")
        prices = tuple(market_prices)
        manual_prices = tuple(overrides)
        fee_observations = tuple(station_fees)
        scan_profile = replace(
            crafting_profile,
            available_focus=constraints.available_focus,
        )
        candidates = filter_recipes(recipes, constraints)
        cancelled = cancellation is not None and cancellation.is_cancelled
        if cancelled:
            self._report(
                progress,
                "cancelled",
                "Scan cancelled before scenario evaluation.",
                0,
                0,
            )
            return ScanSnapshot(
                scan_time=scan_time,
                ruleset_id=self.rules.ruleset_id,
                constraints=constraints,
                recipes_considered=len(candidates),
                scenarios_evaluated=0,
                actionable_count=0,
                rejected_count=0,
                opportunities=(),
                database_load_operations=database_load_operations,
                market_rows_loaded=len(prices),
                override_rows_loaded=len(manual_prices),
                elapsed_seconds=time.perf_counter() - started,
                cancelled=True,
                notes=_SCAN_NOTES,
            )
        pricing_index = PricingIndex(prices, manual_prices, price_history)
        policy = FreshnessPolicy(constraints.maximum_price_age)
        station_policy = FreshnessPolicy(constraints.maximum_station_fee_age)
        total = len(candidates) * len(constraints.craft_cities) * len(constraints.sell_cities)
        self._report(
            progress,
            "evaluate",
            f"Evaluating {total:,} scenarios in memory...",
            0,
            total,
        )

        opportunities: list[CraftOpportunity] = []
        rejection_classes: Counter[str] = Counter()
        evaluated = 0
        actionable = 0
        cancelled = False
        progress_step = max(total // 20, 1)
        output_side = (
            MarketSide.SELL_ORDER
            if constraints.sale_method is SaleMethod.SELL_ORDER
            else MarketSide.BUY_ORDER
        )
        liquidity_index = liquidity_by_key or {}
        history_index = history_by_key or {}
        history_status = history_status_by_key or {}

        for recipe in candidates:
            if cancelled:
                break
            mapping = crafting_skill_mapping_for_recipe(recipe)
            focus_resolution = scan_profile.resolve(mapping)
            for craft_city in constraints.craft_cities:
                material_city = constraints.material_city or craft_city
                station = resolve_station_fee(
                    recipe.output,
                    region=constraints.region.value,
                    city=craft_city,
                    observations=fee_observations,
                    freshness_policy=station_policy,
                    as_of=scan_time,
                )
                bonus = self.rules.city_bonus(
                    recipe.output,
                    craft_city,
                    use_focus=constraints.use_focus,
                )
                for sell_city in constraints.sell_cities:
                    if cancellation is not None and cancellation.is_cancelled:
                        cancelled = True
                        break
                    pricing = pricing_index.resolve(
                        recipe,
                        material_city=material_city,
                        craft_city=craft_city,
                        sell_city=sell_city,
                        region=constraints.region,
                        output_quality=constraints.output_quality,
                        material_side=MarketSide.SELL_ORDER,
                        output_side=output_side,
                        freshness_policy=policy,
                        as_of=scan_time,
                        include_returned_material_prices=True,
                    )
                    context = CraftingContext(
                        craft_city=craft_city,
                        sell_city=sell_city,
                        material_buy_city=material_city,
                        crafts=constraints.crafts,
                        output_quality=constraints.output_quality,
                        use_focus=constraints.use_focus,
                        premium=constraints.premium,
                        station_fee_observation=station.observation,
                        station_fee_freshness_policy=station_policy,
                        as_of=scan_time,
                        sale_method=constraints.sale_method,
                        profile=scan_profile,
                    )
                    calculation = self.calculator.calculate(
                        recipe,
                        pricing.material_prices,
                        pricing.output_price,
                        context,
                        data_quality=ActionabilityAssessment(pricing.data_quality_reasons),
                        returned_material_craft_city_prices=(
                            pricing.returned_material_craft_city_prices
                        ),
                    )
                    if constraints.allow_stale_station_fees:
                        calculation = self._permit_stale_station_assumption(calculation)
                    evaluated += 1
                    maximum_focus_crafts = self._maximum_focus_crafts(
                        calculation.focus_used,
                        constraints.crafts,
                        constraints.available_focus,
                        use_focus=constraints.use_focus,
                    )
                    liquidity_key = (
                        constraints.region,
                        recipe.output.item_id,
                        sell_city,
                        constraints.output_quality,
                    )
                    liquidity = liquidity_index.get(liquidity_key)
                    if liquidity is None:
                        status = history_status.get(liquidity_key)
                        liquidity = assess_liquidity(
                            history_index.get(liquidity_key, ()),
                            current_price=pricing.output_price,
                            now=scan_time,
                            history_available=status in {"success", "empty", "partial"},
                            history_complete=status in {"success", "empty"},
                        )
                    warnings = list(self._liquidity_warnings(liquidity))
                    if pricing.output_price is not None and not any(
                        reason.code is ReasonCode.TOP_OF_BOOK_DEPTH_UNMODELED
                        for reason in calculation.actionability.reasons
                    ):
                        warnings.append(
                            ActionabilityReason(
                                ReasonCode.TOP_OF_BOOK_DEPTH_UNMODELED,
                                "Profit uses a current top-of-book unit price; order depth "
                                "and guaranteed execution quantity are not modeled.",
                                ReasonSeverity.WARNING,
                            )
                        )
                    if warnings:
                        calculation = replace(
                            calculation,
                            actionability=calculation.actionability.adding(*warnings),
                        )
                    if calculation.actionability.is_actionable:
                        actionable += 1
                    opportunity = CraftOpportunity(
                        recipe=recipe,
                        material_city=material_city,
                        craft_city=craft_city,
                        sell_city=sell_city,
                        pricing=pricing,
                        calculation=calculation,
                        station_type=(
                            station.station_type.value if station.station_type is not None else None
                        ),
                        station_displayed_fee=station.displayed_fee,
                        station_fee_provenance=station.provenance,
                        station_fee_observed_at=station.observed_at,
                        production_bonus=bonus.total_production_bonus,
                        production_bonus_status=bonus.classification.value,
                        focus_efficiency=focus_resolution.focus_cost_efficiency,
                        focus_efficiency_source=focus_resolution.source.value,
                        upfront_capital_required=(calculation.total_pre_revenue_cash_required),
                        maximum_focus_crafts=maximum_focus_crafts,
                        station_fee_freshness=station.freshness,
                        liquidity=liquidity,
                    )
                    if opportunity_passes_filters(opportunity, constraints):
                        opportunities.append(opportunity)
                    else:
                        rejection_classes[
                            self._player_rejection_class(opportunity, constraints)
                        ] += 1
                    if evaluated % progress_step == 0 or evaluated == total:
                        self._report(
                            progress,
                            "evaluate",
                            f"Evaluated {evaluated:,} of {total:,} scenarios...",
                            evaluated,
                            total,
                        )
                if cancelled:
                    break
            if cancelled:
                break

        if cancellation is not None and cancellation.is_cancelled:
            cancelled = True
        if cancelled:
            self._report(
                progress,
                "cancelled",
                f"Returning {len(opportunities):,} partial results without ranking...",
                evaluated,
                total,
            )
            ranked = tuple(opportunities)
        else:
            self._report(
                progress,
                "rank",
                f"Ranking {len(opportunities):,} matching opportunities...",
                evaluated,
                total,
            )
            ranked = sort_opportunities(
                opportunities,
                constraints.sort_by,
                descending=constraints.descending,
            )
        elapsed = time.perf_counter() - started
        return ScanSnapshot(
            scan_time=scan_time,
            ruleset_id=self.rules.ruleset_id,
            constraints=constraints,
            recipes_considered=len(candidates),
            scenarios_evaluated=evaluated,
            actionable_count=actionable,
            rejected_count=evaluated - actionable,
            opportunities=ranked,
            database_load_operations=database_load_operations,
            market_rows_loaded=len(prices),
            override_rows_loaded=len(manual_prices),
            elapsed_seconds=elapsed,
            cancelled=cancelled,
            notes=_SCAN_NOTES,
            rejection_class_counts=tuple(sorted(rejection_classes.items())),
        )

    @staticmethod
    def _player_rejection_class(
        opportunity: CraftOpportunity,
        constraints: ScanConstraints,
    ) -> str:
        result = opportunity.calculation
        codes = {reason.code for reason in result.actionability.blocking_reasons}
        if codes & {
            ReasonCode.MISSING_MATERIAL_PRICE,
            ReasonCode.MISSING_OUTPUT_PRICE,
            ReasonCode.STALE_PRICE,
            ReasonCode.FUTURE_TIMESTAMP,
            ReasonCode.UNKNOWN_TIMESTAMP,
        }:
            return "market_data"
        if codes & {
            ReasonCode.UNKNOWN_ITEM_VALUE,
            ReasonCode.UNKNOWN_RETURNABILITY,
            ReasonCode.AMBIGUOUS_RECIPE,
            ReasonCode.UNKNOWN_CITY_BONUS_CLASSIFICATION,
            ReasonCode.UNSUPPORTED_OUTPUT_QUALITY,
            ReasonCode.PROVISIONAL_MECHANICS,
        }:
            return "unsupported_static"
        if codes & {
            ReasonCode.UNKNOWN_STATION_FEE,
            ReasonCode.STALE_STATION_FEE,
            ReasonCode.FUTURE_STATION_FEE_TIMESTAMP,
            ReasonCode.UNKNOWN_STATION_FEE_TIMESTAMP,
            ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION,
            ReasonCode.UNKNOWN_REFINING_SPECIALIZATION,
            ReasonCode.MISSING_FOCUS_COST,
            ReasonCode.INSUFFICIENT_FOCUS,
        }:
            return "setup_required"
        if codes & {ReasonCode.UNTRUSTED_PROVENANCE}:
            return "trust_liquidity"
        if result.profit is not None and (
            result.profit <= 0
            or (
                constraints.minimum_profit is not None
                and result.profit < constraints.minimum_profit
            )
            or (
                constraints.minimum_roi is not None
                and (result.roi is None or result.roi < constraints.minimum_roi)
            )
        ):
            return "unprofitable"
        if constraints.liquidity_levels:
            return "trust_liquidity"
        if result.profit is None:
            return "incomplete"
        return "outside_filters"

    @staticmethod
    def _maximum_focus_crafts(
        focus_used: float | None,
        crafts: int,
        available_focus: float,
        *,
        use_focus: bool,
    ) -> int | None:
        if not use_focus or focus_used is None:
            return None
        per_craft = focus_used / crafts
        if per_craft <= 0:
            return None
        return math.floor(available_focus / per_craft)

    @staticmethod
    def _liquidity_warnings(
        liquidity: LiquidityAssessment,
    ) -> tuple[ActionabilityReason, ...]:
        if liquidity.level is LiquidityLevel.LOW:
            return (
                ActionabilityReason(
                    ReasonCode.LOW_LIQUIDITY,
                    "Reported history indicates Low liquidity; the apparent top-of-book "
                    "profit may not execute at useful volume.",
                    ReasonSeverity.WARNING,
                ),
            )
        if liquidity.level is LiquidityLevel.UNKNOWN:
            return (
                ActionabilityReason(
                    ReasonCode.UNKNOWN_LIQUIDITY,
                    "Liquidity is Unknown because complete recent history is unavailable "
                    "or inconclusive.",
                    ReasonSeverity.WARNING,
                ),
            )
        return ()

    @staticmethod
    def _permit_stale_station_assumption(calculation):
        """Keep stale-fee evidence visible while enabling explicit advisory analysis."""

        reasons = tuple(
            replace(reason, severity=ReasonSeverity.WARNING)
            if reason.code is ReasonCode.STALE_STATION_FEE
            else reason
            for reason in calculation.actionability.reasons
        )
        return replace(
            calculation,
            actionability=ActionabilityAssessment(reasons),
        )

    @staticmethod
    def _report(
        callback: ProgressCallback | None,
        stage: str,
        message: str,
        completed: int,
        total: int,
    ) -> None:
        if callback is not None:
            callback(ScanProgress(stage, message, completed, total))
