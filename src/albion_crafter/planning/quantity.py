from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from .models import (
    ExecutionCapacityKey,
    FindMoneyConstraints,
    PlanAction,
    PlanCandidate,
    PlanReason,
    PlanReasonSeverity,
    ResourceAmount,
)

CancellationCheck = Callable[[], bool]


class QuantityEnumerationCancelled(RuntimeError):
    """Internal signal that bounded quantity/frontier work was cancelled."""


class QuantityMaterializationLimitExceeded(RuntimeError):
    """Raised when a caller requests an unsafe explicit allocation tuple."""


DEFAULT_EXPLICIT_QUANTITY_STATE_LIMIT = 2_000_000


class QuantityCeilingSource(StrEnum):
    EXPLICIT_CAP = "explicit_cap"
    HISTORICAL_VOLUME_SHARE = "historical_volume_share"
    EXPLICIT_FALLBACK_NO_HISTORY = "explicit_fallback_no_history"


@dataclass(frozen=True, slots=True)
class QuantityCeiling:
    """Finite capacity shared by every variant of one output-market key.

    ``maximum_crafts`` is the legacy field name for the user's explicit shared
    action-unit/batch cap.
    ``maximum_output_units`` is an optional historical-volume heuristic in
    output items, not order-book depth. Both limits apply when present.
    """

    execution_capacity_key: ExecutionCapacityKey
    maximum_crafts: int
    maximum_output_units: int | None
    source: QuantityCeilingSource
    reported_24h_volume: int | None = None
    historical_volume_share: float | None = None
    explanation: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_crafts, bool)
            or not isinstance(self.maximum_crafts, int)
            or self.maximum_crafts < 1
        ):
            raise ValueError("maximum_crafts must be a positive integer")
        if self.maximum_output_units is not None and (
            isinstance(self.maximum_output_units, bool)
            or not isinstance(self.maximum_output_units, int)
            or self.maximum_output_units < 0
        ):
            raise ValueError("maximum_output_units must be a non-negative integer")
        if self.reported_24h_volume is not None and (
            isinstance(self.reported_24h_volume, bool)
            or not isinstance(self.reported_24h_volume, int)
            or self.reported_24h_volume < 0
        ):
            raise ValueError("reported_24h_volume must be a non-negative integer")
        if self.historical_volume_share is not None and (
            isinstance(self.historical_volume_share, bool)
            or not isinstance(self.historical_volume_share, (int, float))
            or not math.isfinite(self.historical_volume_share)
            or not 0 < self.historical_volume_share <= 1
        ):
            raise ValueError("historical_volume_share must be greater than 0 and at most 1")
        if not self.explanation.strip():
            raise ValueError("quantity ceiling explanation is required")


@dataclass(frozen=True, slots=True)
class CandidateAllocation:
    candidate: PlanCandidate
    focused_crafts: int
    nonfocused_crafts: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.focused_crafts, self.nonfocused_crafts)
        ):
            raise ValueError("allocation quantities must be non-negative integers")
        if self.total_crafts < 1:
            raise ValueError("candidate allocation must contain at least one craft")
        if self.focused_crafts and not self.candidate.economics.has_focused_variant:
            raise ValueError("focused allocation requires focused candidate economics")

    @property
    def total_crafts(self) -> int:
        return self.focused_crafts + self.nonfocused_crafts

    @property
    def output_units(self) -> int:
        return self.total_crafts * self.candidate.output_quantity_per_craft

    @property
    def pre_revenue_cash(self) -> ResourceAmount:
        return self.total_crafts * self.candidate.economics.pre_revenue_cash_per_craft

    @property
    def focus(self) -> ResourceAmount:
        per_craft = self.candidate.economics.focus_per_focused_craft or 0
        return self.focused_crafts * per_craft

    @property
    def expected_profit(self) -> ResourceAmount:
        focused = self.candidate.economics.focused_profit_per_craft or 0
        return (
            self.focused_crafts * focused
            + self.nonfocused_crafts * self.candidate.economics.nonfocused_profit_per_craft
        )

    @property
    def incremental_focus_profit(self) -> ResourceAmount:
        uplift = self.candidate.economics.incremental_focus_profit_per_craft or 0
        return self.focused_crafts * uplift

    @property
    def expected_revenue(self) -> ResourceAmount | None:
        value = self.candidate.economics.expected_revenue_per_craft
        return None if value is None else self.total_crafts * value

    @property
    def effective_economic_cost(self) -> ResourceAmount | None:
        focused = self.candidate.economics.focused_effective_cost_per_craft
        nonfocused = self.candidate.economics.nonfocused_effective_cost_per_craft
        if self.focused_crafts and focused is None:
            return None
        if self.nonfocused_crafts and nonfocused is None:
            return None
        return self.focused_crafts * (focused or 0) + self.nonfocused_crafts * (nonfocused or 0)

    @property
    def canonical_signature(self) -> tuple[str, int, int]:
        return (
            self.candidate.candidate_id,
            self.focused_crafts,
            self.nonfocused_crafts,
        )

    def to_plan_action(
        self,
        ceiling: QuantityCeiling,
        capacity_ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling] | None = None,
    ) -> PlanAction:
        candidate_reasons = self.candidate.reasons
        if self.nonfocused_crafts == 0 and not self.candidate.economics.nonfocused_eligible:
            # Candidate evaluators use nonfocused_eligible to scope blocking
            # non-Focus reasons when a separately evaluated Focus mode is valid.
            candidate_reasons = tuple(
                reason
                for reason in candidate_reasons
                if reason.severity is not PlanReasonSeverity.BLOCKING
            )
        reasons = _deduplicate_reasons((*self.candidate.route.reasons, *candidate_reasons))
        assert self.candidate.execution_capacity_key is not None
        evidence = dict(self.candidate.evidence)
        evidence["quantity_ceiling"] = json.dumps(
            {
                "source": ceiling.source.value,
                "maximum_crafts": ceiling.maximum_crafts,
                "maximum_output_units": ceiling.maximum_output_units,
                "reported_24h_volume": ceiling.reported_24h_volume,
                "historical_volume_share": ceiling.historical_volume_share,
                "explanation": ceiling.explanation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if capacity_ceilings is not None:
            evidence["capacity_ceilings"] = json.dumps(
                [
                    {
                        "key": [key[0].value, key[1], key[2], key[3]],
                        "role": requirement.role.value,
                        "units_per_action_unit": requirement.units_per_action_unit,
                        "maximum_action_units": value.maximum_crafts,
                        "maximum_market_units": value.maximum_output_units,
                        "source": value.source.value,
                        "reported_24h_volume": value.reported_24h_volume,
                        "historical_volume_share": value.historical_volume_share,
                        "explanation": value.explanation,
                    }
                    for requirement in sorted(
                        self.candidate.capacity_requirements,
                        key=lambda item: item.canonical_key,
                    )
                    for key, value in ((requirement.key, capacity_ceilings[requirement.key]),)
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        return PlanAction(
            candidate_id=self.candidate.candidate_id,
            item_id=self.candidate.item_id,
            display_name=self.candidate.display_name,
            route=self.candidate.route,
            quantity=self.total_crafts,
            focused_quantity=self.focused_crafts,
            nonfocused_quantity=self.nonfocused_crafts,
            output_units=self.output_units,
            quality=self.candidate.quality,
            sale_method=self.candidate.sale_method,
            pre_revenue_cash_required=self.pre_revenue_cash,
            focus_required=self.focus,
            expected_profit=self.expected_profit,
            liquidity=self.candidate.liquidity,
            execution_capacity_key=self.candidate.execution_capacity_key,
            quantity_ceiling=ceiling.maximum_crafts,
            action_kind=self.candidate.action_kind,
            capacity_requirements=self.candidate.capacity_requirements,
            execution_ceiling_output_units=ceiling.maximum_output_units,
            expected_revenue=self.expected_revenue,
            effective_economic_cost=self.effective_economic_cost,
            incremental_focus_profit=self.incremental_focus_profit,
            reasons=reasons,
            evidence=tuple(sorted(evidence.items())),
            oldest_market_observed_at=self.candidate.oldest_market_observed_at,
            station_fee_observed_at=self.candidate.station_fee_observed_at,
        )


@dataclass(frozen=True, slots=True)
class GroupQuantityOption:
    execution_capacity_key: ExecutionCapacityKey
    allocations: tuple[CandidateAllocation, ...]
    total_crafts: int
    total_output_units: int
    pre_revenue_cash: ResourceAmount
    focus: ResourceAmount
    expected_profit: ResourceAmount
    minimum_liquidity_rank: int

    @classmethod
    def empty(cls, key: ExecutionCapacityKey) -> GroupQuantityOption:
        return cls(key, (), 0, 0, 0, 0, 0, 4)

    @property
    def action_count(self) -> int:
        return len(self.allocations)

    @property
    def canonical_signature(self) -> tuple[tuple[str, int, int], ...]:
        return tuple(allocation.canonical_signature for allocation in self.allocations)


@dataclass(frozen=True, slots=True)
class GroupOptionBuildResult:
    options: tuple[GroupQuantityOption, ...]
    quantity_decisions_considered: int
    states_pruned: int
    approximate: bool
    state_limit_reached: bool
    quantity_states_generated: int = 0
    quantity_states_after_pruning: int = 0
    peak_frontier_size: int = 0
    quantity_bundle_count: int = 0
    approximation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CandidateAllocationBuildResult:
    allocations: tuple[CandidateAllocation, ...]
    decisions_considered: int
    states_generated: int
    states_pruned: int
    peak_frontier_size: int
    bundle_count: int
    approximate: bool
    state_limit_reached: bool
    approximation_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CandidateQuantityState:
    candidate: PlanCandidate
    focused_crafts: int = 0
    nonfocused_crafts: int = 0

    @property
    def total_crafts(self) -> int:
        return self.focused_crafts + self.nonfocused_crafts

    @property
    def output_units(self) -> int:
        return self.total_crafts * self.candidate.output_quantity_per_craft

    @property
    def pre_revenue_cash(self) -> int:
        return self.total_crafts * self.candidate.economics.pre_revenue_cash_per_craft

    @property
    def focus(self) -> int:
        per_craft = self.candidate.economics.focus_per_focused_craft or 0
        return self.focused_crafts * per_craft

    @property
    def expected_profit(self) -> int:
        focused = self.candidate.economics.focused_profit_per_craft or 0
        return (
            self.focused_crafts * focused
            + self.nonfocused_crafts * self.candidate.economics.nonfocused_profit_per_craft
        )

    @property
    def canonical_signature(self) -> tuple[int, int]:
        return (self.focused_crafts, self.nonfocused_crafts)

    def add(self, *, focused: int = 0, nonfocused: int = 0) -> _CandidateQuantityState:
        return _CandidateQuantityState(
            self.candidate,
            self.focused_crafts + focused,
            self.nonfocused_crafts + nonfocused,
        )

    def to_allocation(self) -> CandidateAllocation:
        return CandidateAllocation(
            self.candidate,
            self.focused_crafts,
            self.nonfocused_crafts,
        )


def calculate_quantity_ceiling(
    execution_capacity_key: ExecutionCapacityKey,
    *,
    explicit_craft_cap: int,
    history_enabled: bool,
    reported_24h_volume: int | None,
    historical_volume_share: float | None,
) -> QuantityCeiling:
    """Create a finite, transparent ceiling without calling history live depth."""

    if (
        isinstance(explicit_craft_cap, bool)
        or not isinstance(explicit_craft_cap, int)
        or explicit_craft_cap < 1
    ):
        raise ValueError("explicit_craft_cap must be a positive integer")
    if reported_24h_volume is not None and (
        isinstance(reported_24h_volume, bool)
        or not isinstance(reported_24h_volume, int)
        or reported_24h_volume < 0
    ):
        raise ValueError("reported_24h_volume must be a non-negative integer")
    if historical_volume_share is not None and (
        isinstance(historical_volume_share, bool)
        or not isinstance(historical_volume_share, (int, float))
        or not math.isfinite(historical_volume_share)
        or not 0 < historical_volume_share <= 1
    ):
        raise ValueError("historical_volume_share must be greater than 0 and at most 1")

    if (
        history_enabled
        and historical_volume_share is not None
        and reported_24h_volume is not None
        and reported_24h_volume > 0
    ):
        output_cap = int(
            (
                Decimal(reported_24h_volume) * Decimal(str(historical_volume_share))
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        return QuantityCeiling(
            execution_capacity_key,
            explicit_craft_cap,
            output_cap,
            QuantityCeilingSource.HISTORICAL_VOLUME_SHARE,
            reported_24h_volume,
            historical_volume_share,
            "Shared execution ceiling is the lower of the explicit action-unit/batch cap and "
            f"{historical_volume_share:.1%} of {reported_24h_volume:,} reported 24h "
            f"output units ({output_cap:,}); reported history is not live order depth.",
        )

    if history_enabled:
        explanation = (
            "No positive, reliable reported 24h volume was available; the explicit shared "
            f"cap of {explicit_craft_cap:,} action units/batches is the conservative fallback."
        )
        source = QuantityCeilingSource.EXPLICIT_FALLBACK_NO_HISTORY
    else:
        explanation = (
            f"History enrichment is disabled; the explicit shared cap is "
            f"{explicit_craft_cap:,} action units/batches."
        )
        source = QuantityCeilingSource.EXPLICIT_CAP
    return QuantityCeiling(
        execution_capacity_key,
        explicit_craft_cap,
        None,
        source,
        reported_24h_volume,
        historical_volume_share,
        explanation,
    )


def enumerate_candidate_allocations(
    candidate: PlanCandidate,
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
    *,
    state_limit: int = DEFAULT_EXPLICIT_QUANTITY_STATE_LIMIT,
    cancelled: CancellationCheck | None = None,
) -> tuple[CandidateAllocation, ...]:
    """Materialize the exact legal choices for small/reference callers.

    Production optimization uses binary-decomposed mode frontiers in
    :func:`build_group_quantity_options`; it does not call this inherently
    two-dimensional reference helper. The direct implementation still avoids
    scanning impossible Focus values for Focus-only candidates.
    """

    nonfocused_limit, focused_limit = _candidate_mode_limits(
        candidate,
        ceiling,
        constraints,
    )
    if not nonfocused_limit and not focused_limit:
        return ()
    if isinstance(state_limit, bool) or not isinstance(state_limit, int) or state_limit < 1:
        raise ValueError("explicit quantity state_limit must be a positive integer")

    result: list[CandidateAllocation] = []
    maximum_crafts = _effective_maximum_crafts(candidate, ceiling, constraints)
    allocation_count = _explicit_allocation_count(
        maximum_crafts,
        nonfocused_limit,
        focused_limit,
        cancelled=cancelled,
    )
    if allocation_count > state_limit:
        raise QuantityMaterializationLimitExceeded(
            f"Explicit quantity enumeration would materialize {allocation_count:,} states, "
            f"exceeding the named {state_limit:,}-state limit. Use the production optimizer "
            "for bounded binary-frontier approximation or request a smaller batch cap."
        )
    for total in range(1, maximum_crafts + 1):
        _check_cancelled(cancelled)
        if nonfocused_limit and focused_limit:
            minimum_focused = max(total - nonfocused_limit, 0)
            maximum_focused = min(total, focused_limit)
            focused_values = range(minimum_focused, maximum_focused + 1)
        elif focused_limit and total <= focused_limit:
            focused_values = (total,)
        elif nonfocused_limit and total <= nonfocused_limit:
            focused_values = (0,)
        else:
            continue
        for index, focused in enumerate(focused_values):
            if index % 128 == 0:
                _check_cancelled(cancelled)
            result.append(CandidateAllocation(candidate, focused, total - focused))
    return tuple(result)


def _explicit_allocation_count(
    maximum_crafts: int,
    nonfocused_limit: int,
    focused_limit: int,
    *,
    cancelled: CancellationCheck | None,
) -> int:
    result = 0
    for total in range(1, maximum_crafts + 1):
        if total % 256 == 0:
            _check_cancelled(cancelled)
        if nonfocused_limit and focused_limit:
            minimum_focused = max(total - nonfocused_limit, 0)
            maximum_focused = min(total, focused_limit)
            result += max(maximum_focused - minimum_focused + 1, 0)
        elif focused_limit and total <= focused_limit:
            result += 1
        elif nonfocused_limit and total <= nonfocused_limit:
            result += 1
    return result


def estimate_quantity_bundle_count(
    candidate: PlanCandidate,
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
) -> int:
    """Return the exact number of binary bounded-mode bundles for a route."""

    nonfocused_limit, focused_limit = _candidate_mode_limits(
        candidate,
        ceiling,
        constraints,
    )
    return len(_binary_chunks(nonfocused_limit)) + len(_binary_chunks(focused_limit))


def _build_candidate_allocation_frontier(
    candidate: PlanCandidate,
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
    *,
    state_limit: int,
    state_limit_reason: str,
    cancelled: CancellationCheck | None,
) -> _CandidateAllocationBuildResult:
    """Build a bounded-integer route frontier without enumerating all splits."""

    nonfocused_limit, focused_limit = _candidate_mode_limits(
        candidate,
        ceiling,
        constraints,
    )
    modes: list[tuple[int, bool, int]] = []
    if nonfocused_limit:
        modes.append(
            (
                candidate.economics.nonfocused_profit_per_craft,
                False,
                nonfocused_limit,
            )
        )
    if focused_limit:
        focused_profit = candidate.economics.focused_profit_per_craft
        assert focused_profit is not None
        modes.append((focused_profit, True, focused_limit))
    bundles = tuple(
        (focused, chunk)
        for _, focused, maximum in sorted(
            modes,
            key=lambda value: (-value[0], value[1]),
        )
        for chunk in _binary_chunks(maximum)
    )
    if not bundles:
        return _CandidateAllocationBuildResult((), 0, 0, 0, 1, 0, False, False, ())

    frontier = [_CandidateQuantityState(candidate)]
    considered = 0
    generated = 0
    pruned = 0
    peak_frontier = 1
    approximate = False
    limit_reached = False
    approximation_reasons: set[str] = set()
    maximum_crafts = _effective_maximum_crafts(candidate, ceiling, constraints)

    for focused, bundle_quantity in bundles:
        _check_cancelled(cancelled)
        next_frontier: list[_CandidateQuantityState] = []
        for index, prior in enumerate(frontier):
            if index % 128 == 0:
                _check_cancelled(cancelled)
            considered += 1
            next_frontier.append(prior)
            generated += 1

            considered += 1
            combined = prior.add(
                focused=bundle_quantity if focused else 0,
                nonfocused=0 if focused else bundle_quantity,
            )
            if (
                combined.total_crafts <= maximum_crafts
                and combined.pre_revenue_cash <= constraints.silver_budget
                and combined.focus <= constraints.focus_budget
                and (
                    ceiling.maximum_output_units is None
                    or combined.output_units <= ceiling.maximum_output_units
                )
            ):
                next_frontier.append(combined)
                generated += 1

        peak_frontier = max(peak_frontier, len(next_frontier))
        reduced = _pareto_prune_candidate_states(next_frontier, cancelled=cancelled)
        pruned += len(next_frontier) - len(reduced)
        if len(reduced) > state_limit:
            bounded = _bounded_candidate_frontier(
                reduced,
                state_limit,
                cancelled=cancelled,
            )
            pruned += len(reduced) - len(bounded)
            reduced = bounded
            approximate = True
            limit_reached = True
            approximation_reasons.add(state_limit_reason)
        frontier = reduced

    _check_cancelled(cancelled)
    allocations = tuple(
        sorted(
            (state.to_allocation() for state in frontier if state.total_crafts),
            key=lambda allocation: allocation.canonical_signature,
        )
    )
    _check_cancelled(cancelled)
    return _CandidateAllocationBuildResult(
        allocations,
        considered,
        generated,
        pruned,
        peak_frontier,
        len(bundles),
        approximate,
        limit_reached,
        tuple(sorted(approximation_reasons)),
    )


def _candidate_mode_limits(
    candidate: PlanCandidate,
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
) -> tuple[int, int]:
    """Return maximum individually legal non-Focus and Focus quantities."""

    if candidate.execution_capacity_key != ceiling.execution_capacity_key:
        raise ValueError("candidate and quantity ceiling capacity keys differ")
    if (
        candidate.route.region is not constraints.region
        or candidate.sale_method is not constraints.sale_method
    ):
        return (0, 0)
    if any(reason.severity is PlanReasonSeverity.BLOCKING for reason in candidate.route.reasons):
        return (0, 0)
    if candidate.liquidity_rank < constraints.minimum_liquidity.minimum_rank:
        return (0, 0)

    nonfocused_allowed = (
        _mode_passes_filters(
            candidate.economics.nonfocused_profit_per_craft,
            candidate.nonfocused_roi,
            constraints,
        )
        and candidate.economics.nonfocused_eligible
        and not candidate.has_blocker
    )
    focused_allowed = (
        constraints.use_focus
        and candidate.economics.has_focused_variant
        and (not candidate.has_blocker or not candidate.economics.nonfocused_eligible)
        and _mode_passes_filters(
            candidate.economics.focused_profit_per_craft,
            candidate.focused_roi,
            constraints,
        )
    )
    maximum_crafts = _effective_maximum_crafts(candidate, ceiling, constraints)
    nonfocused_limit = maximum_crafts if nonfocused_allowed else 0
    focused_limit = maximum_crafts if focused_allowed else 0
    if focused_limit:
        focus_per_craft = candidate.economics.focus_per_focused_craft
        assert focus_per_craft is not None and focus_per_craft > 0
        focused_limit = min(focused_limit, constraints.focus_budget // focus_per_craft)
    return (nonfocused_limit, focused_limit)


def _effective_maximum_crafts(
    candidate: PlanCandidate,
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
) -> int:
    maximum = min(ceiling.maximum_crafts, constraints.per_item_craft_cap)
    if ceiling.maximum_output_units is not None:
        maximum = min(
            maximum,
            ceiling.maximum_output_units // candidate.output_quantity_per_craft,
        )
    cash_per_craft = candidate.economics.pre_revenue_cash_per_craft
    if cash_per_craft:
        maximum = min(maximum, constraints.silver_budget // cash_per_craft)
    return maximum


def _binary_chunks(maximum: int) -> tuple[int, ...]:
    """Represent every integer in ``0..maximum`` with logarithmically many bundles."""

    if maximum <= 0:
        return ()
    chunks: list[int] = []
    next_power = 1
    remaining = maximum
    while remaining:
        chunk = min(next_power, remaining)
        chunks.append(chunk)
        remaining -= chunk
        next_power *= 2
    return tuple(chunks)


def _pareto_prune_candidate_states(
    states: list[_CandidateQuantityState],
    *,
    cancelled: CancellationCheck | None,
) -> list[_CandidateQuantityState]:
    """Exact one-route frontier in O(n log n).

    Cash and output use are monotone in total crafts for one candidate, so the
    remaining dominance dimensions are total crafts, Focus, and profit.
    """

    unique: dict[tuple[int, int, int], _CandidateQuantityState] = {}
    for index, state in enumerate(states):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        key = (state.total_crafts, state.focus, state.expected_profit)
        existing = unique.get(key)
        if existing is None or state.canonical_signature < existing.canonical_signature:
            unique[key] = state
    _check_cancelled(cancelled)
    candidates = sorted(unique.values(), key=_candidate_frontier_order)
    _check_cancelled(cancelled)
    focus_values = sorted({state.focus for state in candidates})
    prefix_maximum: list[int | None] = [None] * (len(focus_values) + 1)
    retained: list[_CandidateQuantityState] = []
    for index, state in enumerate(candidates):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        focus_index = bisect_left(focus_values, state.focus) + 1
        best_profit = _candidate_fenwick_query(prefix_maximum, focus_index)
        if best_profit is not None and best_profit >= state.expected_profit:
            continue
        retained.append(state)
        _candidate_fenwick_update(prefix_maximum, focus_index, state.expected_profit)
    return retained


def _candidate_fenwick_query(tree: list[int | None], index: int) -> int | None:
    result: int | None = None
    while index > 0:
        value = tree[index]
        if value is not None and (result is None or value > result):
            result = value
        index -= index & -index
    return result


def _candidate_fenwick_update(tree: list[int | None], index: int, value: int) -> None:
    while index < len(tree):
        existing = tree[index]
        if existing is None or value > existing:
            tree[index] = value
        index += index & -index


def _candidate_frontier_order(state: _CandidateQuantityState) -> tuple:
    return (
        state.total_crafts,
        state.focus,
        -state.expected_profit,
        state.canonical_signature,
    )


def _candidate_objective_order(state: _CandidateQuantityState) -> tuple:
    return (
        -state.expected_profit,
        state.pre_revenue_cash,
        state.focus,
        0 if state.total_crafts == 0 else 1,
        state.canonical_signature,
    )


def _bounded_candidate_frontier(
    states: list[_CandidateQuantityState],
    limit: int,
    *,
    cancelled: CancellationCheck | None,
) -> list[_CandidateQuantityState]:
    _check_cancelled(cancelled)
    ordered = sorted(states, key=_candidate_frontier_order)
    _check_cancelled(cancelled)
    best = min(states, key=_candidate_objective_order)
    empty = next((state for state in ordered if state.total_crafts == 0), None)
    required_ids = {id(best)}
    if empty is not None:
        required_ids.add(id(empty))
    selected = [state for state in ordered if id(state) in required_ids]
    pool = [state for state in ordered if id(state) not in required_ids]
    remaining_slots = limit - len(selected)
    if remaining_slots > 0 and pool:
        if remaining_slots >= len(pool):
            selected.extend(pool)
        elif remaining_slots == 1:
            selected.append(pool[len(pool) // 2])
        else:
            indexes = {
                round(index * (len(pool) - 1) / (remaining_slots - 1))
                for index in range(remaining_slots)
            }
            for index, pool_index in enumerate(sorted(indexes)):
                if index % 256 == 0:
                    _check_cancelled(cancelled)
                selected.append(pool[pool_index])
    _check_cancelled(cancelled)
    return sorted(selected, key=_candidate_frontier_order)[:limit]


def build_group_quantity_options(
    candidates: tuple[PlanCandidate, ...],
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
    *,
    state_limit: int,
    state_limit_reason: str = "frontier_state_limit",
    cancelled: CancellationCheck | None = None,
) -> GroupOptionBuildResult:
    """Build one exact-or-bounded frontier for a shared output market.

    Each eligible Focus/non-Focus mode is a bounded integer variable. Binary
    decomposition represents every integer quantity using ``O(log cap)``
    bundles. Candidate-local Pareto pruning occurs after every bundle, before
    route alternatives are combined. Consequently a 10,000-craft cap never
    materializes the roughly 50 million raw Focus splits used by the V0.4
    implementation. If a frontier exceeds ``state_limit``, a deterministic
    feasible sample is retained and the result is explicitly approximate.
    """

    if state_limit < 2:
        raise ValueError("state_limit must be at least 2")
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.canonical_key))
    if any(
        candidate.execution_capacity_key != ceiling.execution_capacity_key for candidate in ordered
    ):
        raise ValueError("every candidate in a group must share the ceiling capacity key")

    frontier = [GroupQuantityOption.empty(ceiling.execution_capacity_key)]
    considered = 0
    generated = 0
    pruned = 0
    approximate = False
    limit_reached = False
    peak_frontier = 1
    bundle_count = 0
    approximation_reasons: set[str] = set()
    streaming_threshold = max(state_limit * 2, 256)

    for candidate in ordered:
        _check_cancelled(cancelled)
        local = _build_candidate_allocation_frontier(
            candidate,
            ceiling,
            constraints,
            state_limit=state_limit,
            state_limit_reason=state_limit_reason,
            cancelled=cancelled,
        )
        considered += local.decisions_considered
        generated += local.states_generated
        pruned += local.states_pruned
        peak_frontier = max(peak_frontier, local.peak_frontier_size)
        bundle_count += local.bundle_count
        approximate = approximate or local.approximate
        limit_reached = limit_reached or local.state_limit_reached
        approximation_reasons.update(local.approximation_reasons)
        choices: tuple[CandidateAllocation | None, ...] = (None, *local.allocations)
        next_frontier: list[GroupQuantityOption] = []
        for prior in frontier:
            _check_cancelled(cancelled)
            for choice_index, choice in enumerate(choices):
                considered += 1
                if choice_index % 128 == 0:
                    _check_cancelled(cancelled)
                if choice is None:
                    combined = prior
                else:
                    total_crafts = prior.total_crafts + choice.total_crafts
                    if total_crafts > ceiling.maximum_crafts:
                        continue
                    total_output_units = prior.total_output_units + choice.output_units
                    if (
                        ceiling.maximum_output_units is not None
                        and total_output_units > ceiling.maximum_output_units
                    ):
                        continue
                    pre_revenue_cash = prior.pre_revenue_cash + choice.pre_revenue_cash
                    if pre_revenue_cash > constraints.silver_budget:
                        continue
                    focus = prior.focus + choice.focus
                    if focus > constraints.focus_budget:
                        continue
                    combined = _combine(
                        prior,
                        choice,
                        total_crafts=total_crafts,
                        total_output_units=total_output_units,
                        pre_revenue_cash=pre_revenue_cash,
                        focus=focus,
                    )
                next_frontier.append(combined)
                generated += 1
                peak_frontier = max(peak_frontier, len(next_frontier))
                if len(next_frontier) >= streaming_threshold:
                    if approximate:
                        pruned += len(next_frontier) - state_limit
                        next_frontier = _bounded_frontier(
                            next_frontier,
                            state_limit,
                            cancelled=cancelled,
                        )
                    else:
                        reduced = _pareto_prune(
                            next_frontier,
                            include_capacity=True,
                            cancelled=cancelled,
                        )
                        pruned += len(next_frontier) - len(reduced)
                        next_frontier = reduced
                    if len(next_frontier) > state_limit:
                        before_bounding = len(next_frontier)
                        next_frontier = _bounded_frontier(
                            next_frontier,
                            state_limit,
                            cancelled=cancelled,
                        )
                        pruned += before_bounding - len(next_frontier)
                        approximate = True
                        limit_reached = True
                        approximation_reasons.add(state_limit_reason)

        reduced = _pareto_prune(
            next_frontier,
            include_capacity=True,
            cancelled=cancelled,
        )
        pruned += len(next_frontier) - len(reduced)
        if len(reduced) > state_limit:
            before_bounding = len(reduced)
            reduced = _bounded_frontier(
                reduced,
                state_limit,
                cancelled=cancelled,
            )
            pruned += before_bounding - len(reduced)
            approximate = True
            limit_reached = True
            approximation_reasons.add(state_limit_reason)
        frontier = reduced

    reduced = _pareto_prune(
        frontier,
        include_capacity=False,
        cancelled=cancelled,
    )
    pruned += len(frontier) - len(reduced)
    if len(reduced) > state_limit:
        before_bounding = len(reduced)
        reduced = _bounded_frontier(
            reduced,
            state_limit,
            cancelled=cancelled,
        )
        pruned += before_bounding - len(reduced)
        approximate = True
        limit_reached = True
        approximation_reasons.add(state_limit_reason)
    _check_cancelled(cancelled)
    ordered_result = tuple(sorted(reduced, key=_frontier_order))
    _check_cancelled(cancelled)
    return GroupOptionBuildResult(
        ordered_result,
        considered,
        pruned,
        approximate,
        limit_reached,
        generated,
        len(reduced),
        peak_frontier,
        bundle_count,
        tuple(sorted(approximation_reasons)),
    )


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise QuantityEnumerationCancelled("planning quantity enumeration was cancelled")


def _mode_passes_filters(
    profit: int | None,
    roi: float | None,
    constraints: FindMoneyConstraints,
) -> bool:
    if profit is None:
        return False
    if constraints.minimum_profit is not None and profit < constraints.minimum_profit:
        return False
    return constraints.minimum_roi is None or (roi is not None and roi >= constraints.minimum_roi)


def _combine(
    prior: GroupQuantityOption,
    allocation: CandidateAllocation,
    *,
    total_crafts: int | None = None,
    total_output_units: int | None = None,
    pre_revenue_cash: ResourceAmount | None = None,
    focus: ResourceAmount | None = None,
) -> GroupQuantityOption:
    allocations = (*prior.allocations, allocation)
    return GroupQuantityOption(
        prior.execution_capacity_key,
        allocations,
        prior.total_crafts + allocation.total_crafts if total_crafts is None else total_crafts,
        prior.total_output_units + allocation.output_units
        if total_output_units is None
        else total_output_units,
        prior.pre_revenue_cash + allocation.pre_revenue_cash
        if pre_revenue_cash is None
        else pre_revenue_cash,
        prior.focus + allocation.focus if focus is None else focus,
        prior.expected_profit + allocation.expected_profit,
        min(prior.minimum_liquidity_rank, allocation.candidate.liquidity_rank),
    )


@dataclass(frozen=True, slots=True)
class _DominanceEntry:
    option: GroupQuantityOption
    resource_profit_rank: tuple[int, int, int]
    tie_key: tuple

    @classmethod
    def from_option(cls, option: GroupQuantityOption) -> _DominanceEntry:
        return cls(
            option,
            (
                option.expected_profit,
                -option.pre_revenue_cash,
                -option.focus,
            ),
            _tie_key(option),
        )


@dataclass(slots=True)
class _FocusPrefixMaximum:
    """Fenwick prefix maximum for one exact capacity-consumption bucket."""

    coordinates: tuple[int, ...]
    tree: list[_DominanceEntry | None]

    @classmethod
    def create(cls, focuses: set[int]) -> _FocusPrefixMaximum:
        coordinates = tuple(sorted(focuses))
        return cls(coordinates, [None] * (len(coordinates) + 1))

    def update(self, entry: _DominanceEntry) -> None:
        index = bisect_left(self.coordinates, entry.option.focus) + 1
        while index < len(self.tree):
            current = self.tree[index]
            if current is None or _dominance_entry_preferred(entry, current):
                self.tree[index] = entry
            index += index & -index

    def query(self, maximum_focus: int) -> _DominanceEntry | None:
        index = bisect_right(self.coordinates, maximum_focus)
        result: _DominanceEntry | None = None
        while index > 0:
            current = self.tree[index]
            if current is not None and (
                result is None or _dominance_entry_preferred(current, result)
            ):
                result = current
            index -= index & -index
        return result


def _pareto_prune(
    options: list[GroupQuantityOption],
    *,
    include_capacity: bool,
    cancelled: CancellationCheck | None = None,
) -> list[GroupQuantityOption]:
    """Return the exact group frontier without a quadratic dominance scan.

    Options are processed in ascending cash/Focus order. For each exact
    craft/output-capacity bucket, a Fenwick tree records the best prior option
    at or below a queried Focus cost. Querying only buckets whose capacity use
    is no greater than the current option implements the complete dominance
    relation. The retained result is identical to an all-pairs scan, including
    liquidity/action-count/canonical tie-breaking for equal numeric states.
    """

    unique: dict[
        tuple[int, int, int, int, int],
        GroupQuantityOption,
    ] = {}
    for index, option in enumerate(options):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        numeric_key = (
            option.pre_revenue_cash,
            option.focus,
            option.expected_profit,
            option.total_crafts if include_capacity else 0,
            option.total_output_units if include_capacity else 0,
        )
        existing = unique.get(numeric_key)
        if existing is None or _tie_preferred(option, existing):
            unique[numeric_key] = option

    _check_cancelled(cancelled)
    candidates = sorted(unique.values(), key=_frontier_order)
    _check_cancelled(cancelled)
    entries = tuple(_DominanceEntry.from_option(option) for option in candidates)
    bucket_focuses: dict[tuple[int, int], set[int]] = {}
    for index, entry in enumerate(entries):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        option = entry.option
        bucket = _capacity_bucket(option, include_capacity=include_capacity)
        bucket_focuses.setdefault(bucket, set()).add(option.focus)
    _check_cancelled(cancelled)
    prefix_maxima = {
        bucket: _FocusPrefixMaximum.create(focuses) for bucket, focuses in bucket_focuses.items()
    }
    buckets_by_crafts: dict[int, tuple[tuple[int, tuple[int, int]], ...]] = {}
    for crafts in sorted({bucket[0] for bucket in prefix_maxima}):
        buckets_by_crafts[crafts] = tuple(
            sorted(
                (output_units, (bucket_crafts, output_units))
                for bucket_crafts, output_units in prefix_maxima
                if bucket_crafts == crafts
            )
        )

    retained: list[GroupQuantityOption] = []
    comparisons = 0
    for index, entry in enumerate(entries):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        option = entry.option
        maximum_crafts, maximum_output_units = _capacity_bucket(
            option,
            include_capacity=include_capacity,
        )
        best: _DominanceEntry | None = None
        for crafts, buckets in buckets_by_crafts.items():
            if crafts > maximum_crafts:
                break
            for output_units, bucket in buckets:
                if output_units > maximum_output_units:
                    break
                comparisons += 1
                if comparisons % 1024 == 0:
                    _check_cancelled(cancelled)
                current = prefix_maxima[bucket].query(option.focus)
                if current is not None and (
                    best is None or _dominance_entry_preferred(current, best)
                ):
                    best = current
        if best is None or not _dominance_entry_preferred(best, entry):
            retained.append(option)
        prefix_maxima[_capacity_bucket(option, include_capacity=include_capacity)].update(entry)
    return retained


def _capacity_bucket(
    option: GroupQuantityOption,
    *,
    include_capacity: bool,
) -> tuple[int, int]:
    if not include_capacity:
        return (0, 0)
    return (option.total_crafts, option.total_output_units)


def _dominance_entry_preferred(
    left: _DominanceEntry,
    right: _DominanceEntry,
) -> bool:
    if left.resource_profit_rank != right.resource_profit_rank:
        return left.resource_profit_rank > right.resource_profit_rank
    return left.tie_key < right.tie_key


def _tie_preferred(left: GroupQuantityOption, right: GroupQuantityOption) -> bool:
    return _tie_key(left) < _tie_key(right)


def _tie_key(option: GroupQuantityOption) -> tuple:
    return (
        -option.minimum_liquidity_rank,
        option.action_count,
        option.canonical_signature,
    )


def _frontier_order(option: GroupQuantityOption) -> tuple:
    return (
        option.pre_revenue_cash,
        option.focus,
        -option.expected_profit,
        -option.minimum_liquidity_rank,
        option.action_count,
        option.canonical_signature,
    )


def _objective_order(option: GroupQuantityOption) -> tuple:
    return (
        -option.expected_profit,
        option.pre_revenue_cash,
        option.focus,
        -option.minimum_liquidity_rank,
        option.action_count,
        option.canonical_signature,
    )


def _bounded_frontier(
    options: list[GroupQuantityOption],
    limit: int,
    *,
    cancelled: CancellationCheck | None = None,
) -> list[GroupQuantityOption]:
    """Deterministically sample a Pareto frontier without relaxing feasibility."""

    _check_cancelled(cancelled)
    ordered = sorted(options, key=_frontier_order)
    _check_cancelled(cancelled)
    best = min(options, key=_objective_order)
    required_ids = {id(ordered[0]), id(best)}
    selected = [option for option in ordered if id(option) in required_ids]
    remaining_slots = limit - len(selected)
    pool = [option for option in ordered if id(option) not in required_ids]
    if remaining_slots > 0 and pool:
        if remaining_slots >= len(pool):
            selected.extend(pool)
        elif remaining_slots == 1:
            selected.append(pool[len(pool) // 2])
        else:
            indexes = {
                round(index * (len(pool) - 1) / (remaining_slots - 1))
                for index in range(remaining_slots)
            }
            for index, pool_index in enumerate(sorted(indexes)):
                if index % 256 == 0:
                    _check_cancelled(cancelled)
                selected.append(pool[pool_index])
    _check_cancelled(cancelled)
    return sorted(selected, key=_frontier_order)[:limit]


def _deduplicate_reasons(reasons: tuple[PlanReason, ...]) -> tuple[PlanReason, ...]:
    unique = {(reason.code, reason.message, reason.severity): reason for reason in reasons}
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[0].value, item[2].value, item[1]),
        )
    )
