from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from albion_crafter.core.actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
    ReasonSeverity,
)
from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.freshness import FreshnessPolicy
from albion_crafter.core.mechanics import CURRENT_RULES, MechanicsRules
from albion_crafter.core.models import ActionKind, CraftingContext, CraftResult, SaleMethod
from albion_crafter.market.history import MarketHistoryInterval
from albion_crafter.market.liquidity import LiquidityAssessment, LiquidityLevel
from albion_crafter.market.models import MarketPrice, MarketSide, Region, UserPriceOverride
from albion_crafter.opportunity.pricing import PricingIndex

from .models import (
    ExecutionCapacityKey,
    FindMoneyConstraints,
    PlanCandidate,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    quantize_profit_down,
    quantize_resource_up,
)
from .preflight import EligibleRecipeRoute

LiquidityKey = tuple[Region, str, str, int]
ProgressCallback = Callable[[int, int], None]
CancellationCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class CandidateNearMiss:
    action_kind: ActionKind
    item_id: str
    display_name: str
    candidate_id: str
    route_text: str
    expected_profit: int | None
    reasons: tuple[PlanReason, ...]


@dataclass(frozen=True, slots=True)
class CandidateEvaluationResult:
    candidates: tuple[PlanCandidate, ...]
    near_misses: tuple[CandidateNearMiss, ...]
    rejection_counts: tuple[tuple[str, int], ...]
    scenarios_evaluated: int
    elapsed_seconds: float
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class CandidateShortlist:
    candidates: tuple[PlanCandidate, ...]
    selected_capacity_keys: tuple[ExecutionCapacityKey, ...]
    capacity_groups_considered: int
    capacity_groups_selected: int


@dataclass(frozen=True, slots=True)
class CandidatePruningResult:
    candidates: tuple[PlanCandidate, ...]
    dominated_count: int
    routes_before: int = 0
    routes_after: int = 0
    local_modes_removed: int = 0
    equivalent_routes_collapsed: int = 0


@dataclass(frozen=True, slots=True, order=True)
class CandidateMode:
    """One legal, integer-quantized per-craft mode used for safe pruning.

    This is deliberately smaller than :class:`CandidateEconomics`. Route
    pruning may compare only resources and value that the optimizer actually
    allocates per craft. Evidence and liquidity are compared separately before
    a strictly better route is allowed to remove another route.
    """

    pre_revenue_cash: int
    focus: int
    expected_profit: int


class CandidatePruningCancelled(RuntimeError):
    """Raised when route-frontier normalization or comparison is cancelled."""


@dataclass(frozen=True, slots=True)
class _CandidatePruningView:
    candidate: PlanCandidate
    modes: tuple[CandidateMode, ...]


class PlanCandidateEvaluator:
    """Evaluate one-craft routes without database or network access.

    The resulting integers are deliberately conservative: pre-revenue cash and
    Focus round upward, while expected profit rounds downward. A focused
    calculation can fail independently without discarding a valid non-Focus
    variant of the same route.
    """

    def __init__(self, rules: MechanicsRules = CURRENT_RULES) -> None:
        self.rules = rules
        self.calculator = CraftCalculator(rules)

    def evaluate(
        self,
        eligible: Iterable[EligibleRecipeRoute],
        market_prices: Iterable[MarketPrice],
        overrides: Iterable[UserPriceOverride],
        crafting_profile: CraftingSkillProfile,
        constraints: FindMoneyConstraints,
        *,
        history: Iterable[MarketHistoryInterval] = (),
        liquidity_by_key: Mapping[LiquidityKey, LiquidityAssessment] | None = None,
        as_of: datetime | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> CandidateEvaluationResult:
        started = time.perf_counter()
        evaluation_time = as_of or datetime.now(UTC)
        if evaluation_time.tzinfo is None:
            raise ValueError("candidate evaluation as_of must be timezone-aware")
        routes = tuple(sorted(eligible, key=_eligible_order))
        price_rows = tuple(market_prices)
        override_rows = tuple(overrides)
        price_index = PricingIndex(price_rows, override_rows, history)
        profile = replace(crafting_profile, available_focus=constraints.available_focus)
        market_policy = FreshnessPolicy(constraints.max_market_age)
        station_policy = FreshnessPolicy(constraints.max_station_fee_age)
        output_side = (
            MarketSide.SELL_ORDER
            if constraints.sale_method is SaleMethod.SELL_ORDER
            else MarketSide.BUY_ORDER
        )
        liquidity_index = liquidity_by_key or {}
        candidates: list[PlanCandidate] = []
        near_misses: list[CandidateNearMiss] = []
        rejected: Counter[str] = Counter()
        total = len(routes)

        for position, eligible_route in enumerate(routes, start=1):
            if cancelled is not None and cancelled():
                return CandidateEvaluationResult(
                    tuple(candidates),
                    tuple(near_misses),
                    tuple(sorted(rejected.items())),
                    position - 1,
                    max(time.perf_counter() - started, 0.0),
                    True,
                )
            recipe = eligible_route.recipe
            route = eligible_route.route
            pricing = price_index.resolve(
                recipe,
                material_city=route.material_city,
                craft_city=route.craft_city,
                sell_city=route.sell_city,
                region=constraints.region,
                output_quality=1,
                material_side=MarketSide.SELL_ORDER,
                output_side=output_side,
                freshness_policy=market_policy,
                as_of=evaluation_time,
                include_returned_material_prices=(
                    route.material_city.casefold() != route.craft_city.casefold()
                ),
            )
            data_quality = ActionabilityAssessment(pricing.data_quality_reasons)
            base_context = dict(
                craft_city=route.craft_city,
                sell_city=route.sell_city,
                material_buy_city=route.material_city,
                crafts=1,
                output_quality=1,
                premium=constraints.premium,
                station_fee_observation=eligible_route.station_fee,
                station_fee_freshness_policy=(
                    None if constraints.allow_stale_station_fees else station_policy
                ),
                as_of=evaluation_time,
                sale_method=constraints.sale_method,
                profile=profile,
            )
            nonfocused = self.calculator.calculate(
                recipe,
                pricing.material_prices,
                pricing.output_price,
                CraftingContext(use_focus=False, **base_context),
                data_quality=data_quality,
                returned_material_craft_city_prices=(
                    pricing.returned_material_craft_city_prices or None
                ),
            )
            focused: CraftResult | None = None
            if eligible_route.focused_variant_eligible:
                focused = self.calculator.calculate(
                    recipe,
                    pricing.material_prices,
                    pricing.output_price,
                    CraftingContext(use_focus=True, **base_context),
                    data_quality=data_quality,
                    returned_material_craft_city_prices=(
                        pricing.returned_material_craft_city_prices or None
                    ),
                )

            general_reasons = _plan_reasons(
                nonfocused.actionability.reasons,
                allow_stale_station=constraints.allow_stale_station_fees,
            )
            general_reasons = _deduplicate_reasons((*eligible_route.reasons, *general_reasons))
            candidate_id = _candidate_id(
                recipe.action_kind,
                recipe.output.item_id,
                route,
                constraints.sale_method,
            )
            if not _has_complete_economics(nonfocused):
                reasons = general_reasons or (
                    PlanReason(
                        PlanReasonCode.OTHER,
                        "One-craft economics are incomplete for this route.",
                    ),
                )
                near_misses.append(
                    _near_miss(candidate_id, eligible_route, nonfocused.profit, reasons)
                )
                for reason in reasons:
                    rejected[reason.code.value] += 1
                if progress is not None:
                    progress(position, total)
                continue

            focused_valid = bool(focused is not None and _has_complete_economics(focused))
            focused_reasons = (
                _plan_reasons(
                    focused.actionability.reasons,
                    allow_stale_station=constraints.allow_stale_station_fees,
                )
                if focused is not None
                else ()
            )
            if focused_valid and any(
                reason.severity is PlanReasonSeverity.BLOCKING for reason in focused_reasons
            ):
                focused_valid = False

            transport_cash = route.transport_cost_per_craft
            nonfocused_profit = quantize_profit_down(nonfocused.profit - transport_cash)
            focused_profit = (
                quantize_profit_down(focused.profit - transport_cash)
                if focused_valid and focused is not None and focused.profit is not None
                else None
            )
            gross_material_cash = quantize_resource_up(nonfocused.gross_material_purchase_cash)
            station_cash = quantize_resource_up(nonfocused.station_cash)
            setup_cash = quantize_resource_up(nonfocused.listing_setup_cash)
            pre_revenue = gross_material_cash + station_cash + setup_cash + transport_cash
            nonfocused_effective_cost = quantize_resource_up(
                nonfocused.effective_economic_cost + transport_cash
            )
            focused_effective_cost = (
                quantize_resource_up(focused.effective_economic_cost + transport_cash)
                if focused_valid
                and focused is not None
                and focused.effective_economic_cost is not None
                else None
            )
            nonfocused_roi = (
                None
                if nonfocused_effective_cost == 0
                else nonfocused_profit / nonfocused_effective_cost
            )
            focused_roi = (
                None
                if focused_profit is None or not focused_effective_cost
                else focused_profit / focused_effective_cost
            )
            focus_cost = (
                quantize_resource_up(focused.focus_used)
                if focused_valid and focused is not None and focused.focus_used is not None
                else None
            )
            liquidity_key = (
                constraints.region,
                recipe.output.item_id,
                route.sell_city,
                1,
            )
            liquidity = liquidity_index.get(liquidity_key)
            liquidity_level = liquidity.level if liquidity is not None else LiquidityLevel.UNKNOWN
            reasons = list(general_reasons)
            if constraints.history_enabled and liquidity_level is LiquidityLevel.UNKNOWN:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_LIQUIDITY,
                        "Complete recent output history is unavailable, so liquidity is Unknown.",
                        PlanReasonSeverity.WARNING,
                    )
                )

            evidence = _candidate_evidence(
                eligible_route,
                pricing.evidence,
                liquidity,
                self.rules,
                nonfocused=nonfocused,
                focused=focused if focused_valid else None,
                focused_valid=focused_valid,
            )
            candidate = PlanCandidate(
                candidate_id=candidate_id,
                item_id=recipe.output.item_id,
                display_name=recipe.output.display_name,
                route=route,
                economics=_candidate_economics(
                    nonfocused,
                    pre_revenue=pre_revenue,
                    nonfocused_profit=nonfocused_profit,
                    nonfocused_effective_cost=nonfocused_effective_cost,
                    focused=focused if focused_valid else None,
                    focused_profit=focused_profit,
                    focused_effective_cost=focused_effective_cost,
                    focus_cost=focus_cost,
                    transport_cash=transport_cash,
                    gross_material_cash=gross_material_cash,
                    station_cash=station_cash,
                    setup_cash=setup_cash,
                    nonfocused_eligible=not any(
                        reason.severity is PlanReasonSeverity.BLOCKING for reason in general_reasons
                    ),
                ),
                action_kind=recipe.action_kind,
                output_quantity_per_craft=recipe.output_quantity,
                quality=1,
                sale_method=constraints.sale_method,
                liquidity=liquidity_level,
                nonfocused_roi=nonfocused_roi,
                focused_roi=focused_roi,
                reasons=_deduplicate_reasons(tuple(reasons)),
                evidence=evidence,
                oldest_market_observed_at=pricing.oldest_required_timestamp,
                station_fee_observed_at=eligible_route.station_fee.observed_at,
            )
            candidates.append(candidate)

            if (
                not candidate.economics.nonfocused_eligible
                and not candidate.economics.has_focused_variant
            ):
                near_misses.append(
                    _near_miss(candidate_id, eligible_route, nonfocused.profit, candidate.reasons)
                )
                for reason in candidate.reasons:
                    if reason.severity is PlanReasonSeverity.BLOCKING:
                        rejected[reason.code.value] += 1
            if progress is not None:
                progress(position, total)

        return CandidateEvaluationResult(
            tuple(sorted(candidates, key=lambda value: value.canonical_key)),
            tuple(sorted(near_misses, key=_near_miss_order)),
            tuple(sorted(rejected.items())),
            total,
            max(time.perf_counter() - started, 0.0),
        )


def shortlist_candidates(
    candidates: Sequence[PlanCandidate],
    *,
    maximum_capacity_groups: int,
    constraints: FindMoneyConstraints | None = None,
) -> CandidateShortlist:
    """Select output/sell-city groups using transparent deterministic economics.

    Each capacity group is ranked by its best currently eligible route. Once a
    group is selected, every route for that same output market is retained so
    the optimizer can still choose among competing material/craft routes.
    """

    if isinstance(maximum_capacity_groups, bool) or maximum_capacity_groups < 1:
        raise ValueError("maximum_capacity_groups must be positive")
    grouped: dict[tuple[tuple[str, str, str, int, str, int], ...], list[PlanCandidate]] = (
        defaultdict(list)
    )
    for candidate in candidates:
        if _best_eligible_profit(candidate, constraints) > 0:
            grouped[candidate.capacity_signature].append(candidate)
    ranked = sorted(
        grouped,
        key=lambda key: (_group_rank(grouped[key], constraints), key),
    )
    selected_groups = tuple(ranked[:maximum_capacity_groups])
    selected_keys = tuple(
        sorted(
            {
                requirement.key
                for signature in selected_groups
                for candidate in grouped[signature]
                for requirement in candidate.capacity_requirements
            },
            key=_capacity_order,
        )
    )
    selected = tuple(
        sorted(
            (candidate for signature in selected_groups for candidate in grouped[signature]),
            key=lambda value: value.canonical_key,
        )
    )
    return CandidateShortlist(
        selected,
        selected_keys,
        len(grouped),
        len(selected_groups),
    )


def prune_dominated_candidates(
    candidates: Sequence[PlanCandidate],
    constraints: FindMoneyConstraints,
    *,
    cancelled: CancellationCheck | None = None,
) -> CandidatePruningResult:
    """Return an optimum-preserving subset of economically substitutable routes.

    The transformation has two explicit phases. First, each candidate's own
    legal modes are reduced to their strict Pareto frontier. A locally useless
    Focus mode therefore cannot manufacture apparent strictness in a later
    route-to-route comparison. Second, mutually covering economic frontiers are
    collapsed to one deterministic representative before strict route
    dominance is considered.

    Comparisons never cross output capacity, output-per-craft, or sale-method
    semantics. A retained route can replace each eliminated craft one-for-one:
    shared craft/output consumption is unchanged, cash and Focus do not
    increase, and expected profit does not decrease. ``dominated_count`` is
    retained as the compatible total number of eliminated routes; the more
    specific counters explain how that total was obtained.
    """

    routes_before = len(candidates)
    grouped: dict[
        tuple[tuple[tuple[str, str, str, int, str, int], ...], int, SaleMethod],
        list[_CandidatePruningView],
    ] = defaultdict(list)
    local_modes_removed = 0
    for candidate in sorted(candidates, key=lambda value: value.canonical_key):
        _check_pruning_cancelled(cancelled)
        raw_modes = _eligible_modes(candidate, constraints)
        modes = _pareto_candidate_modes(raw_modes, cancelled=cancelled)
        local_modes_removed += len(raw_modes) - len(modes)
        grouped[
            (
                candidate.capacity_signature,
                candidate.output_quantity_per_craft,
                candidate.sale_method,
            )
        ].append(_CandidatePruningView(candidate, modes))

    retained: list[PlanCandidate] = []
    equivalent_routes_collapsed = 0
    for key in sorted(grouped, key=_pruning_group_order):
        _check_pruning_cancelled(cancelled)
        views = grouped[key]
        representatives, collapsed = _collapse_equivalent_routes(
            views,
            cancelled=cancelled,
        )
        equivalent_routes_collapsed += collapsed
        for right_index, right in enumerate(representatives):
            _check_pruning_cancelled(cancelled)
            dominated = False
            for left_index, left in enumerate(representatives):
                if left_index == right_index:
                    continue
                _check_pruning_cancelled(cancelled)
                if _strictly_dominates_view(left, right):
                    dominated = True
                    break
            if not dominated:
                retained.append(right.candidate)

    routes_after = len(retained)
    return CandidatePruningResult(
        tuple(sorted(retained, key=lambda value: value.canonical_key)),
        routes_before - routes_after,
        routes_before,
        routes_after,
        local_modes_removed,
        equivalent_routes_collapsed,
    )


def candidate_mode_frontier(
    candidate: PlanCandidate,
    constraints: FindMoneyConstraints,
    *,
    cancelled: CancellationCheck | None = None,
) -> tuple[CandidateMode, ...]:
    """Return the candidate-local Pareto frontier used by route pruning."""

    _check_pruning_cancelled(cancelled)
    return _pareto_candidate_modes(
        _eligible_modes(candidate, constraints),
        cancelled=cancelled,
    )


def _pareto_candidate_modes(
    modes: Sequence[tuple[int, int, int]],
    *,
    cancelled: CancellationCheck | None,
) -> tuple[CandidateMode, ...]:
    # Exact duplicates are redundant even though neither is a *strict*
    # dominator. This also gives a canonical representation if a zero-cost
    # Focus mode is economically identical to the non-Focus mode.
    unique = tuple(
        CandidateMode(*values)
        for values in sorted(set(modes), key=lambda value: (value[0], value[1], -value[2]))
    )
    retained: list[CandidateMode] = []
    for right in unique:
        _check_pruning_cancelled(cancelled)
        if any(left is not right and _mode_strictly_dominates(left, right) for left in unique):
            continue
        retained.append(right)
    return tuple(retained)


def _mode_weakly_dominates(left: CandidateMode, right: CandidateMode) -> bool:
    return (
        left.pre_revenue_cash <= right.pre_revenue_cash
        and left.focus <= right.focus
        and left.expected_profit >= right.expected_profit
    )


def _mode_strictly_dominates(left: CandidateMode, right: CandidateMode) -> bool:
    return _mode_weakly_dominates(left, right) and left != right


def _frontier_covers(
    left: _CandidatePruningView,
    right: _CandidatePruningView,
) -> bool:
    if not left.modes or not right.modes:
        return False
    return all(
        any(_mode_weakly_dominates(left_mode, right_mode) for left_mode in left.modes)
        for right_mode in right.modes
    )


def _collapse_equivalent_routes(
    views: Sequence[_CandidatePruningView],
    *,
    cancelled: CancellationCheck | None,
) -> tuple[tuple[_CandidatePruningView, ...], int]:
    """Quotient mutual economic coverage before strict dominance.

    Treating equivalence separately is the survivor invariant missing from the
    original implementation: an equivalence class always contributes exactly
    one representative, so ``A removes B`` and ``B removes A`` cannot empty a
    profitable market.
    """

    parents = list(range(len(views)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(views):
        _check_pruning_cancelled(cancelled)
        if not left.modes:
            continue
        for right_index in range(left_index + 1, len(views)):
            _check_pruning_cancelled(cancelled)
            right = views[right_index]
            if _frontier_covers(left, right) and _frontier_covers(right, left):
                union(left_index, right_index)

    components: dict[int, list[_CandidatePruningView]] = defaultdict(list)
    for index, view in enumerate(views):
        components[find(index)].append(view)
    representatives = tuple(
        min(component, key=_equivalent_representative_key)
        for _, component in sorted(components.items())
    )
    return (
        tuple(sorted(representatives, key=lambda value: value.candidate.canonical_key)),
        len(views) - len(representatives),
    )


def _strictly_dominates_view(
    left: _CandidatePruningView,
    right: _CandidatePruningView,
) -> bool:
    if not _frontier_covers(left, right):
        return False
    if _frontier_covers(right, left):
        # Mutual coverage is handled only by the equivalence quotient above.
        return False
    return _evidence_no_worse(left.candidate, right.candidate)


def _equivalent_representative_key(view: _CandidatePruningView) -> tuple:
    modes = view.modes
    if not modes:
        return (
            _evidence_preference_key(view.candidate),
            10**30,
            10**30,
            10**30,
            view.candidate.canonical_key,
        )
    return (
        _evidence_preference_key(view.candidate),
        min(mode.pre_revenue_cash for mode in modes),
        min(mode.focus for mode in modes),
        -max(mode.expected_profit for mode in modes),
        view.candidate.canonical_key,
    )


def _evidence_preference_key(candidate: PlanCandidate) -> tuple:
    reasons = (*candidate.route.reasons, *candidate.reasons)
    blocking = sum(reason.severity is PlanReasonSeverity.BLOCKING for reason in reasons)
    warnings = sum(reason.severity is PlanReasonSeverity.WARNING for reason in reasons)
    return (
        2 if blocking else 1 if warnings else 0,
        blocking,
        warnings,
        -candidate.liquidity_rank,
        _newer_timestamp_key(candidate.oldest_market_observed_at),
        _newer_timestamp_key(candidate.station_fee_observed_at),
    )


def _evidence_no_worse(left: PlanCandidate, right: PlanCandidate) -> bool:
    left_reasons = (*left.route.reasons, *left.reasons)
    right_reasons = (*right.route.reasons, *right.reasons)
    left_blocking = sum(reason.severity is PlanReasonSeverity.BLOCKING for reason in left_reasons)
    right_blocking = sum(reason.severity is PlanReasonSeverity.BLOCKING for reason in right_reasons)
    left_warnings = sum(reason.severity is PlanReasonSeverity.WARNING for reason in left_reasons)
    right_warnings = sum(reason.severity is PlanReasonSeverity.WARNING for reason in right_reasons)
    return (
        left_blocking <= right_blocking
        and left_warnings <= right_warnings
        and left.liquidity_rank >= right.liquidity_rank
        and _timestamp_no_older(
            left.oldest_market_observed_at,
            right.oldest_market_observed_at,
        )
        and _timestamp_no_older(
            left.station_fee_observed_at,
            right.station_fee_observed_at,
        )
    )


def _timestamp_no_older(left: datetime | None, right: datetime | None) -> bool:
    if right is None:
        return True
    return left is not None and left >= right


def _newer_timestamp_key(value: datetime | None) -> tuple[int, float]:
    return (1, 0.0) if value is None else (0, -value.timestamp())


def _check_pruning_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise CandidatePruningCancelled("planning candidate pruning was cancelled")


def _pruning_group_order(
    key: tuple[tuple[tuple[str, str, str, int, str, int], ...], int, SaleMethod],
) -> tuple:
    capacity_signature, output_quantity, sale_method = key
    return (capacity_signature, output_quantity, sale_method.value)


def _eligible_modes(
    candidate: PlanCandidate,
    constraints: FindMoneyConstraints,
) -> tuple[tuple[int, int, int], ...]:
    economics = candidate.economics
    if candidate.has_blocker or economics.pre_revenue_cash_per_craft > constraints.silver_budget:
        return ()
    modes: list[tuple[int, int, int]] = []
    if economics.nonfocused_eligible and _passes_mode_filter(
        economics.nonfocused_profit_per_craft,
        candidate.nonfocused_roi,
        constraints,
    ):
        modes.append(
            (
                economics.pre_revenue_cash_per_craft,
                0,
                economics.nonfocused_profit_per_craft,
            )
        )
    if (
        constraints.use_focus
        and economics.has_focused_variant
        and (economics.focus_per_focused_craft or 0) <= constraints.focus_budget
        and _passes_mode_filter(
            economics.focused_profit_per_craft,
            candidate.focused_roi,
            constraints,
        )
    ):
        assert economics.focused_profit_per_craft is not None
        assert economics.focus_per_focused_craft is not None
        modes.append(
            (
                economics.pre_revenue_cash_per_craft,
                economics.focus_per_focused_craft,
                economics.focused_profit_per_craft,
            )
        )
    return tuple(modes)


def _passes_mode_filter(
    profit: int | None,
    roi: float | None,
    constraints: FindMoneyConstraints,
) -> bool:
    if profit is None or profit <= 0:
        return False
    if constraints.minimum_profit is not None and profit < constraints.minimum_profit:
        return False
    return constraints.minimum_roi is None or (roi is not None and roi >= constraints.minimum_roi)


def _reason_rank(candidate: PlanCandidate) -> int:
    if candidate.has_blocker:
        return 2
    if any(reason.severity is PlanReasonSeverity.WARNING for reason in candidate.reasons):
        return 1
    return 0


def _candidate_economics(
    nonfocused: CraftResult,
    *,
    pre_revenue: int,
    nonfocused_profit: int,
    nonfocused_effective_cost: int,
    focused: CraftResult | None,
    focused_profit: int | None,
    focused_effective_cost: int | None,
    focus_cost: int | None,
    transport_cash: int,
    gross_material_cash: int,
    station_cash: int,
    setup_cash: int,
    nonfocused_eligible: bool,
):
    from .models import CandidateEconomics

    return CandidateEconomics(
        pre_revenue_cash_per_craft=pre_revenue,
        nonfocused_profit_per_craft=nonfocused_profit,
        focused_profit_per_craft=focused_profit,
        focus_per_focused_craft=focus_cost,
        nonfocused_eligible=nonfocused_eligible,
        expected_revenue_per_craft=quantize_resource_up(nonfocused.gross_sale_value),
        nonfocused_effective_cost_per_craft=nonfocused_effective_cost,
        focused_effective_cost_per_craft=focused_effective_cost,
        gross_material_cash_per_craft=gross_material_cash,
        station_cash_per_craft=station_cash,
        setup_cash_per_craft=setup_cash,
        transport_cash_per_craft=transport_cash,
    )


def _has_complete_economics(result: CraftResult) -> bool:
    return all(
        value is not None
        for value in (
            result.profit,
            result.gross_sale_value,
            result.total_pre_revenue_cash_required,
            result.effective_economic_cost,
            result.gross_material_purchase_cash,
            result.station_cash,
            result.listing_setup_cash,
        )
    )


def _candidate_evidence(
    eligible: EligibleRecipeRoute,
    price_evidence,
    liquidity: LiquidityAssessment | None,
    rules: MechanicsRules,
    *,
    nonfocused: CraftResult,
    focused: CraftResult | None,
    focused_valid: bool,
) -> tuple[tuple[str, str], ...]:
    recipe = eligible.recipe
    prices = [
        {
            "item_id": line.item_id,
            "city": line.city,
            "side": line.side,
            "price": line.price,
            "observed_at": (
                line.observation_timestamp.astimezone(UTC).isoformat()
                if line.observation_timestamp is not None
                else None
            ),
            "provenance": line.provenance.value,
            "freshness": line.freshness.value,
            "role": line.role,
            "source": line.source.value,
            "confidence": line.confidence.value,
            "current_price": line.current_price,
            "current_observed_at": (
                line.current_timestamp.astimezone(UTC).isoformat()
                if line.current_timestamp is not None
                else None
            ),
            "historical_reference_price": line.historical_reference_price,
            "historical_days_used": line.historical_days_used,
            "historical_total_volume": line.historical_total_volume,
            "historical_avg_daily_volume_7d": line.historical_avg_daily_volume_7d,
        }
        for line in price_evidence
    ]
    station = eligible.station_fee
    fce = eligible.focus_resolution
    no_focus_bonus = rules.production_bonus_resolution(
        recipe.action_kind,
        recipe.output,
        eligible.route.production_city,
        use_focus=False,
    )
    focus_bonus = rules.production_bonus_resolution(
        recipe.action_kind,
        recipe.output,
        eligible.route.production_city,
        use_focus=True,
    )
    entries = {
        "recipe": json.dumps(
            {
                "action_kind": recipe.action_kind.value,
                "item_id": recipe.output.item_id,
                "display_name": recipe.output.display_name,
                "tier": recipe.output.tier,
                "enchantment": recipe.output.enchantment,
                "production_group": recipe.output.crafting_category.casefold(),
                "source_version": recipe.source_version,
                "item_value": recipe.item_value,
                "base_focus_cost": recipe.base_focus_cost,
                "output_quantity": recipe.output_quantity,
                "materials": [
                    {
                        "item_id": material.item_id,
                        "quantity": material.quantity,
                        "returnable": material.returnable,
                    }
                    for material in recipe.materials
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "prices": json.dumps(prices, sort_keys=True, separators=(",", ":")),
        "station_fee": json.dumps(
            {
                "city": station.city,
                "station_type": station.station_type.value,
                "displayed_fee": station.displayed_fee,
                "observed_at": station.observed_at.astimezone(UTC).isoformat(),
                "provenance": station.provenance.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "focus": json.dumps(
            {
                "eligible": focused_valid,
                "fce": fce.focus_cost_efficiency,
                "source": fce.source.value,
                "provenance": fce.provenance.value,
                "mapping_key": fce.mapping.mapping_key if fce.mapping is not None else None,
                "mapping_verified": fce.mapping.verified if fce.mapping is not None else False,
                "mapping_source_version": (
                    fce.mapping.source_version if fce.mapping is not None else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "mechanics": json.dumps(
            {
                "action_kind": recipe.action_kind.value,
                "ruleset_id": rules.ruleset_id,
                "status": rules.verification_status.value,
                "component_statuses": {
                    key: status.value for key, status in rules.verification_components
                },
                "city_bonus_dataset": rules.production_bonus_dataset_version(recipe.action_kind),
                "nonfocused_city_bonus": _city_bonus_evidence(no_focus_bonus),
                "focused_city_bonus": _city_bonus_evidence(focus_bonus),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "accounting": json.dumps(
            {
                "nonfocused_per_craft": _craft_result_evidence(nonfocused),
                "focused_per_craft": (
                    _craft_result_evidence(focused) if focused is not None else None
                ),
                "returned_material_policy": (
                    "Expected returns reduce acquisition-cost-basis consumption only; they "
                    "are not revenue and do not fund another plan action."
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "transport": json.dumps(
            {
                "policy": eligible.route.transport_policy.value,
                "cost_per_craft": eligible.route.transport_cost_per_craft,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if liquidity is not None:
        entries["liquidity"] = json.dumps(
            {
                "level": liquidity.level.value,
                "reported_volume": liquidity.reported_volume,
                "active_intervals": liquidity.active_intervals,
                "weighted_mean_price": liquidity.weighted_mean_price,
                "current_price_deviation": liquidity.current_price_deviation,
                "last_activity_at": (
                    liquidity.last_activity_at.astimezone(UTC).isoformat()
                    if liquidity.last_activity_at is not None
                    else None
                ),
                "reasons": list(liquidity.reasons),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return tuple(sorted(entries.items()))


def _city_bonus_evidence(value) -> dict[str, object]:
    return {
        "action_kind": value.action_kind.value if value.action_kind is not None else None,
        "production_group": value.production_group,
        "classification": value.classification.value,
        "baseline_bonus": value.baseline_bonus,
        "specialty_bonus": value.specialty_bonus,
        "focus_bonus": value.focus_bonus,
        "total_production_bonus": value.total_production_bonus,
        "verified_on": value.verified_on,
        "dataset_version": value.dataset_version,
        "source": value.source,
    }


def _craft_result_evidence(value: CraftResult) -> dict[str, float | None]:
    return {
        "return_rate": value.return_rate,
        "gross_material_purchase_cash": value.gross_material_purchase_cash,
        "returned_material_cost_basis_value": value.returned_material_cost_basis_value,
        "returned_material_craft_city_market_value": (
            value.returned_material_craft_city_market_value
        ),
        "effective_material_cost": value.effective_material_cost,
        "station_cash": value.station_cash,
        "listing_setup_cash": value.listing_setup_cash,
        "transaction_tax": value.transaction_tax,
        "market_fees": value.market_fees,
        "total_pre_revenue_cash_required": value.total_pre_revenue_cash_required,
        "effective_economic_cost": value.effective_economic_cost,
        "gross_sale_value": value.gross_sale_value,
        "net_sale_value": value.net_sale_value,
        "profit": value.profit,
        "focus_used": value.focus_used,
    }


def _plan_reasons(
    reasons: Sequence[ActionabilityReason],
    *,
    allow_stale_station: bool,
) -> tuple[PlanReason, ...]:
    converted: list[PlanReason] = []
    for reason in reasons:
        severity = (
            PlanReasonSeverity.WARNING
            if reason.severity is ReasonSeverity.WARNING
            or (allow_stale_station and reason.code is ReasonCode.STALE_STATION_FEE)
            else PlanReasonSeverity.BLOCKING
        )
        code = {
            ReasonCode.MISSING_MATERIAL_PRICE: PlanReasonCode.MISSING_MATERIAL_PRICE,
            ReasonCode.MISSING_OUTPUT_PRICE: PlanReasonCode.MISSING_OUTPUT_PRICE,
            ReasonCode.STALE_PRICE: PlanReasonCode.STALE_MARKET_DATA,
            ReasonCode.FUTURE_TIMESTAMP: PlanReasonCode.FUTURE_MARKET_DATA,
            ReasonCode.UNKNOWN_TIMESTAMP: PlanReasonCode.STALE_MARKET_DATA,
            ReasonCode.UNTRUSTED_PROVENANCE: PlanReasonCode.UNTRUSTED_PROVENANCE,
            ReasonCode.UNKNOWN_STATION_FEE: PlanReasonCode.MISSING_STATION_FEE,
            ReasonCode.STALE_STATION_FEE: PlanReasonCode.STALE_STATION_FEE,
            ReasonCode.FUTURE_STATION_FEE_TIMESTAMP: PlanReasonCode.FUTURE_STATION_FEE,
            ReasonCode.UNKNOWN_STATION_FEE_TIMESTAMP: PlanReasonCode.STALE_STATION_FEE,
            ReasonCode.INSUFFICIENT_FOCUS: PlanReasonCode.INSUFFICIENT_FOCUS,
            ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION: PlanReasonCode.UNKNOWN_FCE,
            ReasonCode.UNKNOWN_REFINING_SPECIALIZATION: PlanReasonCode.UNKNOWN_FCE,
            ReasonCode.UNKNOWN_CITY_BONUS_CLASSIFICATION: PlanReasonCode.UNKNOWN_CITY_BONUS,
            ReasonCode.UNSUPPORTED_OUTPUT_QUALITY: PlanReasonCode.UNSUPPORTED_OUTPUT_QUALITY,
            ReasonCode.PROVISIONAL_MECHANICS: PlanReasonCode.UNVERIFIED_MECHANICS,
            ReasonCode.TRANSPORT_ASSUMPTION: PlanReasonCode.UNMODELED_TRANSPORT,
            ReasonCode.UNKNOWN_LIQUIDITY: PlanReasonCode.UNKNOWN_LIQUIDITY,
            ReasonCode.LOW_LIQUIDITY: PlanReasonCode.LOW_LIQUIDITY,
        }.get(reason.code, PlanReasonCode.OTHER)
        converted.append(PlanReason(code, reason.message, severity))
    return _deduplicate_reasons(tuple(converted))


def _candidate_id(
    action_kind: ActionKind,
    item_id: str,
    route,
    sale_method: SaleMethod,
) -> str:
    return "|".join(
        (
            action_kind.value,
            item_id,
            route.region.value,
            route.material_city,
            route.craft_city,
            route.sell_city,
            sale_method.value,
        )
    )


def _near_miss(
    candidate_id: str,
    eligible: EligibleRecipeRoute,
    profit: float | None,
    reasons: tuple[PlanReason, ...],
) -> CandidateNearMiss:
    route = eligible.route
    return CandidateNearMiss(
        eligible.action_kind,
        eligible.recipe.output.item_id,
        eligible.recipe.output.display_name,
        candidate_id,
        f"{route.material_city} -> {route.craft_city} -> {route.sell_city}",
        quantize_profit_down(profit) if profit is not None else None,
        reasons,
    )


def _eligible_order(value: EligibleRecipeRoute) -> tuple:
    return (value.recipe.output.item_id, value.route.canonical_key)


def _near_miss_order(value: CandidateNearMiss) -> tuple:
    return (
        -(value.expected_profit if value.expected_profit is not None else -(10**30)),
        value.item_id,
        value.candidate_id,
    )


def _best_eligible_profit(
    candidate: PlanCandidate,
    constraints: FindMoneyConstraints | None = None,
) -> int:
    if constraints is not None:
        modes = _eligible_modes(candidate, constraints)
        return max((value[2] for value in modes), default=-(10**30))
    values = (
        candidate.economics.nonfocused_profit_per_craft
        if candidate.economics.nonfocused_eligible
        else -(10**30),
        candidate.economics.focused_profit_per_craft
        if candidate.economics.has_focused_variant
        else -(10**30),
    )
    return max(value for value in values if value is not None)


def _group_rank(
    candidates: Sequence[PlanCandidate],
    constraints: FindMoneyConstraints | None = None,
) -> tuple:
    best_profit = max(_best_eligible_profit(value, constraints) for value in candidates)
    best_roi = max(
        (
            max(
                value.nonfocused_roi
                if value.economics.nonfocused_eligible
                and (
                    constraints is None
                    or _passes_mode_filter(
                        value.economics.nonfocused_profit_per_craft,
                        value.nonfocused_roi,
                        constraints,
                    )
                )
                else float("-inf"),
                value.focused_roi
                if value.focused_roi is not None
                and (
                    constraints is None
                    or any(mode[1] > 0 for mode in _eligible_modes(value, constraints))
                )
                else float("-inf"),
            )
            for value in candidates
        ),
        default=float("-inf"),
    )
    best_spf = max(
        (
            value.economics.incremental_focus_profit_per_craft
            / value.economics.focus_per_focused_craft
            for value in candidates
            if value.economics.incremental_focus_profit_per_craft is not None
            and value.economics.focus_per_focused_craft
            and (
                constraints is None
                or any(mode[1] > 0 for mode in _eligible_modes(value, constraints))
            )
        ),
        default=float("-inf"),
    )
    minimum_cash = min(value.economics.pre_revenue_cash_per_craft for value in candidates)
    return (-best_profit, -best_roi, -best_spf, minimum_cash)


def _capacity_order(key: ExecutionCapacityKey) -> tuple[str, str, str, int]:
    return (key[0].value, key[1], key[2].casefold(), key[3])


def _deduplicate_reasons(reasons: tuple[PlanReason, ...]) -> tuple[PlanReason, ...]:
    unique = {(reason.code, reason.message, reason.severity): reason for reason in reasons}
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda value: (value[2].value, value[0].value, value[1]),
        )
    )
