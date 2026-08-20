from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .models import (
    ExecutionCapacityKey,
    FindMoneyConstraints,
    OptimizationDiagnostics,
    OptimizationResult,
    OptimizationStatus,
    PlanCandidate,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanStatus,
)
from .quantity import (
    CandidateAllocation,
    QuantityCeiling,
    QuantityMaterializationLimitExceeded,
    enumerate_candidate_allocations,
)

CancellationCheck = Callable[[], bool]


class MultiCapacityCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MultiCapacityLimits:
    max_states: int
    max_quantity_transitions: int
    max_portfolio_transitions: int


@dataclass(frozen=True, slots=True)
class _State:
    allocations: tuple[CandidateAllocation, ...]
    cash: int
    focus: int
    profit: int
    capacity: tuple[tuple[ExecutionCapacityKey, int], ...]
    minimum_liquidity_rank: int
    signature: tuple[tuple[str, int, int], ...]

    @classmethod
    def empty(cls) -> _State:
        return cls((), 0, 0, 0, (), 4, ())

    @property
    def action_count(self) -> int:
        return len(self.allocations)


@dataclass(frozen=True, slots=True)
class MultiCapacityBuild:
    result: OptimizationResult
    component_count: int


def optimize_multicapacity(
    candidates: Sequence[PlanCandidate],
    ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
    constraints: FindMoneyConstraints,
    *,
    limits: MultiCapacityLimits,
    cancelled: CancellationCheck | None = None,
    elapsed_seconds: float = 0.0,
) -> MultiCapacityBuild:
    """Solve capacity-connected components, then combine them under cash/Focus."""

    ordered = tuple(sorted(candidates, key=lambda value: value.canonical_key))
    _check_cancelled(cancelled)
    missing = {
        requirement.key
        for candidate in ordered
        for requirement in candidate.capacity_requirements
        if requirement.key not in ceilings
    }
    if missing:
        raise ValueError(f"missing quantity ceilings for {len(missing)} capacity keys")
    if any(candidate.route.region is not constraints.region for candidate in ordered):
        raise ValueError("planning candidate region must match constraints")
    if any(ceilings[key].execution_capacity_key != key for key in ceilings):
        raise ValueError("quantity ceiling mapping key does not match its value")
    components = _connected_components(ordered, cancelled=cancelled)
    approximate_reasons: set[str] = set()
    transitions = 0
    pruned = 0
    peak = 1
    component_frontiers: list[list[_State]] = []
    compaction_threshold = max(limits.max_states * 2, 512)
    materialization_limit = min(
        limits.max_quantity_transitions,
        max(limits.max_states * 4, 512),
    )

    for component in components:
        frontier = [_State.empty()]
        for candidate in component:
            _check_cancelled(cancelled)
            primary = ceilings[candidate.execution_capacity_key]
            try:
                allocations = enumerate_candidate_allocations(
                    candidate,
                    primary,
                    constraints,
                    state_limit=materialization_limit,
                    cancelled=cancelled,
                )
            except QuantityMaterializationLimitExceeded:
                allocations = _bounded_candidate_allocations(candidate, primary, constraints)
                approximate_reasons.add("candidate_quantity_state_limit")
            choices: tuple[CandidateAllocation | None, ...] = (None, *allocations)
            next_frontier: list[_State] = []
            for prior in frontier:
                for choice in choices:
                    transitions += 1
                    if transitions % 256 == 0:
                        _check_cancelled(cancelled)
                    if transitions > limits.max_quantity_transitions:
                        approximate_reasons.add("quantity_transition_limit")
                        break
                    if choice is None:
                        next_frontier.append(prior)
                    else:
                        combined = _combine(prior, choice)
                        if (
                            combined.cash <= constraints.silver_budget
                            and combined.focus <= constraints.focus_budget
                            and _capacity_feasible(combined.capacity, ceilings)
                        ):
                            next_frontier.append(combined)
                    if len(next_frontier) >= compaction_threshold:
                        compacted = _pareto(next_frontier, include_capacity=True)
                        pruned += len(next_frontier) - len(compacted)
                        peak = max(peak, len(next_frontier), len(compacted))
                        if len(compacted) > limits.max_states:
                            pruned += len(compacted) - limits.max_states
                            compacted = _bounded(
                                compacted,
                                limits.max_states,
                                include_capacity=True,
                            )
                            approximate_reasons.add("component_frontier_state_limit")
                        next_frontier = compacted
                if transitions > limits.max_quantity_transitions:
                    break
            peak = max(peak, len(next_frontier))
            reduced = _pareto(next_frontier, include_capacity=True)
            pruned += len(next_frontier) - len(reduced)
            peak = max(peak, len(reduced))
            if len(reduced) > limits.max_states:
                pruned += len(reduced) - limits.max_states
                reduced = _bounded(reduced, limits.max_states, include_capacity=True)
                approximate_reasons.add("component_frontier_state_limit")
            frontier = reduced
            if transitions > limits.max_quantity_transitions:
                break
        final_component = _pareto(frontier, include_capacity=False)
        if len(final_component) > limits.max_states:
            pruned += len(final_component) - limits.max_states
            final_component = _bounded(
                final_component,
                limits.max_states,
                include_capacity=False,
            )
            approximate_reasons.add("component_frontier_state_limit")
        component_frontiers.append(final_component)

    global_frontier = [_State.empty()]
    portfolio_transitions = 0
    for component in component_frontiers:
        _check_cancelled(cancelled)
        combined_states: list[_State] = []
        for prior in global_frontier:
            for option in component:
                portfolio_transitions += 1
                merged = _merge_components(prior, option)
                if (
                    merged.cash <= constraints.silver_budget
                    and merged.focus <= constraints.focus_budget
                ):
                    combined_states.append(merged)
                if portfolio_transitions > limits.max_portfolio_transitions:
                    approximate_reasons.add("portfolio_transition_limit")
                    break
            if len(combined_states) >= compaction_threshold:
                compacted = _pareto(combined_states, include_capacity=False)
                pruned += len(combined_states) - len(compacted)
                peak = max(peak, len(combined_states), len(compacted))
                if len(compacted) > limits.max_states:
                    pruned += len(compacted) - limits.max_states
                    compacted = _bounded(
                        compacted,
                        limits.max_states,
                        include_capacity=False,
                    )
                    approximate_reasons.add("portfolio_frontier_state_limit")
                combined_states = compacted
            if portfolio_transitions > limits.max_portfolio_transitions:
                break
        peak = max(peak, len(combined_states))
        reduced = _pareto(combined_states, include_capacity=False)
        pruned += len(combined_states) - len(reduced)
        peak = max(peak, len(reduced))
        if len(reduced) > limits.max_states:
            pruned += len(reduced) - limits.max_states
            reduced = _bounded(reduced, limits.max_states, include_capacity=False)
            approximate_reasons.add("portfolio_frontier_state_limit")
        global_frontier = reduced or [_State.empty()]

    best = min(global_frontier, key=_objective)
    actions = tuple(
        sorted(
            (
                allocation.to_plan_action(
                    ceilings[allocation.candidate.execution_capacity_key],
                    ceilings,
                )
                for allocation in best.allocations
            ),
            key=lambda value: value.canonical_key,
        )
    )
    reasons = list(_deduplicate_reasons(reason for action in actions for reason in action.reasons))
    status = OptimizationStatus.APPROXIMATE if approximate_reasons else OptimizationStatus.EXACT
    if approximate_reasons:
        reasons.append(
            PlanReason(
                PlanReasonCode.APPROXIMATE_OPTIMIZATION,
                "A named multi-capacity search bound was reached: "
                + ", ".join(sorted(approximate_reasons))
                + ". Every returned silver, Focus, and market-capacity constraint remains "
                "feasible, but optimality is not proven.",
                PlanReasonSeverity.WARNING,
            )
        )
    if not actions:
        reasons.append(
            PlanReason(
                PlanReasonCode.NO_FEASIBLE_ACTIONS,
                "No positive-profit action fit the selected evidence and resource constraints.",
            )
        )
        plan_status = PlanStatus.NON_ACTIONABLE
    elif any(reason.severity is PlanReasonSeverity.BLOCKING for reason in reasons):
        plan_status = PlanStatus.NON_ACTIONABLE
    elif any(reason.severity is PlanReasonSeverity.WARNING for reason in reasons):
        plan_status = PlanStatus.ADVISORY
    else:
        plan_status = PlanStatus.DECISION_GRADE
    total_cash = sum(action.pre_revenue_cash_required for action in actions)
    total_focus = sum(action.focus_required for action in actions)
    total_profit = sum(action.expected_profit for action in actions)
    diagnostics = OptimizationDiagnostics(
        method="capacity_component_pareto_v1",
        status=status,
        candidate_count=len(ordered),
        group_count=len(components),
        quantity_decision_count=transitions,
        states_considered=transitions + portfolio_transitions,
        states_pruned=pruned,
        state_limit=limits.max_states,
        state_limit_reached=bool(approximate_reasons),
        elapsed_seconds=elapsed_seconds,
        quantity_states_generated=transitions,
        quantity_states_after_pruning=sum(len(value) for value in component_frontiers),
        portfolio_states_considered=portfolio_transitions,
        portfolio_states_pruned=pruned,
        peak_frontier_size=peak,
        candidate_routes_before_pruning=len(ordered),
        candidate_routes_after_pruning=len(ordered),
        approximation_reasons=tuple(sorted(approximate_reasons)),
        effective_state_limit=limits.max_states,
        quantity_transition_limit=limits.max_quantity_transitions,
        portfolio_transition_limit=limits.max_portfolio_transitions,
    )
    result = OptimizationResult(
        actions,
        total_cash,
        total_focus,
        total_profit,
        constraints.available_silver - total_cash,
        constraints.available_focus - total_focus,
        plan_status,
        _deduplicate_reasons(reasons),
        diagnostics,
    )
    return MultiCapacityBuild(result, len(components))


def _connected_components(
    candidates: tuple[PlanCandidate, ...],
    *,
    cancelled: CancellationCheck | None,
) -> tuple[tuple[PlanCandidate, ...], ...]:
    if not candidates:
        return ()
    parents = list(range(len(candidates)))

    def find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: int, right: int) -> None:
        first, second = find(left), find(right)
        if first != second:
            parents[max(first, second)] = min(first, second)

    owners: dict[ExecutionCapacityKey, int] = {}
    for index, candidate in enumerate(candidates):
        if index % 128 == 0:
            _check_cancelled(cancelled)
        for requirement in candidate.capacity_requirements:
            prior = owners.setdefault(requirement.key, index)
            union(index, prior)
    groups: dict[int, list[PlanCandidate]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        groups[find(index)].append(candidate)
    return tuple(
        tuple(sorted(group, key=lambda value: value.canonical_key))
        for _, group in sorted(groups.items())
    )


def _combine(prior: _State, allocation: CandidateAllocation) -> _State:
    usage = dict(prior.capacity)
    for requirement in allocation.candidate.capacity_requirements:
        usage[requirement.key] = usage.get(requirement.key, 0) + (
            allocation.total_crafts * requirement.units_per_action_unit
        )
    return _State(
        (*prior.allocations, allocation),
        prior.cash + allocation.pre_revenue_cash,
        prior.focus + allocation.focus,
        prior.profit + allocation.expected_profit,
        tuple(sorted(usage.items(), key=lambda value: _capacity_order(value[0]))),
        min(prior.minimum_liquidity_rank, allocation.candidate.liquidity_rank),
        (*prior.signature, allocation.canonical_signature),
    )


def _merge_components(left: _State, right: _State) -> _State:
    return _State(
        (*left.allocations, *right.allocations),
        left.cash + right.cash,
        left.focus + right.focus,
        left.profit + right.profit,
        (*left.capacity, *right.capacity),
        min(left.minimum_liquidity_rank, right.minimum_liquidity_rank),
        (*left.signature, *right.signature),
    )


def _capacity_feasible(
    usage: tuple[tuple[ExecutionCapacityKey, int], ...],
    ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
) -> bool:
    return all(units <= _capacity_limit(ceilings[key]) for key, units in usage)


def _capacity_limit(ceiling: QuantityCeiling) -> int:
    return (
        ceiling.maximum_output_units
        if ceiling.maximum_output_units is not None
        else ceiling.maximum_crafts
    )


def _pareto(states: list[_State], *, include_capacity: bool) -> list[_State]:
    unique: dict[tuple, _State] = {}
    for state in states:
        key = (
            state.cash,
            state.focus,
            state.profit,
            state.capacity if include_capacity else (),
            _tie(state),
        )
        unique.setdefault(key, state)
    ordered = sorted(unique.values(), key=lambda value: _frontier_order(value, include_capacity))
    retained: list[_State] = []
    for state in ordered:
        if any(_dominates(prior, state, include_capacity=include_capacity) for prior in retained):
            continue
        retained = [
            prior
            for prior in retained
            if not _dominates(state, prior, include_capacity=include_capacity)
        ]
        retained.append(state)
    return sorted(retained, key=lambda value: _frontier_order(value, include_capacity))


def _dominates(left: _State, right: _State, *, include_capacity: bool) -> bool:
    if left.cash > right.cash or left.focus > right.focus or left.profit < right.profit:
        return False
    if include_capacity:
        left_usage = dict(left.capacity)
        right_usage = dict(right.capacity)
        if any(
            left_usage.get(key, 0) > right_usage.get(key, 0)
            for key in left_usage.keys() | right_usage.keys()
        ):
            return False
    return left.cash < right.cash or left.focus < right.focus or left.profit > right.profit


def _bounded(states: list[_State], limit: int, *, include_capacity: bool) -> list[_State]:
    ordered = sorted(states, key=lambda value: _frontier_order(value, include_capacity))
    best = min(states, key=_objective)
    selected = [ordered[0]]
    if best is not ordered[0]:
        selected.append(best)
    pool = [value for value in ordered if value not in selected]
    slots = limit - len(selected)
    if slots > 0 and pool:
        if slots >= len(pool):
            selected.extend(pool)
        elif slots == 1:
            selected.append(pool[len(pool) // 2])
        else:
            indexes = {round(index * (len(pool) - 1) / (slots - 1)) for index in range(slots)}
            selected.extend(pool[index] for index in sorted(indexes))
    return sorted(selected, key=lambda value: _frontier_order(value, include_capacity))[:limit]


def _bounded_candidate_allocations(
    candidate: PlanCandidate,
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
) -> tuple[CandidateAllocation, ...]:
    maximum = min(ceiling.maximum_crafts, constraints.per_item_craft_cap)
    quantities = sorted({1, maximum, *(2**power for power in range(maximum.bit_length()))})
    result: list[CandidateAllocation] = []
    for quantity in quantities:
        if not 1 <= quantity <= maximum:
            continue
        if candidate.economics.nonfocused_eligible:
            result.append(CandidateAllocation(candidate, 0, quantity))
        if (
            candidate.economics.has_focused_variant
            and candidate.economics.focus_per_focused_craft
            and quantity * candidate.economics.focus_per_focused_craft <= constraints.focus_budget
        ):
            result.append(CandidateAllocation(candidate, quantity, 0))
    return tuple(result)


def _objective(state: _State) -> tuple:
    return (-state.profit, state.cash, state.focus, *(_tie(state)))


def _tie(state: _State) -> tuple:
    return (-state.minimum_liquidity_rank, state.action_count, state.signature)


def _frontier_order(state: _State, include_capacity: bool) -> tuple:
    return (
        state.cash,
        state.focus,
        -state.profit,
        state.capacity if include_capacity else (),
        *_tie(state),
    )


def _capacity_order(key: ExecutionCapacityKey) -> tuple[str, str, str, int]:
    return (key[0].value, key[1], key[2].casefold(), key[3])


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise MultiCapacityCancelled("multi-capacity optimization was cancelled")


def _deduplicate_reasons(reasons) -> tuple[PlanReason, ...]:
    unique = {(reason.code, reason.message, reason.severity): reason for reason in reasons}
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda item: (item[2].value, item[0].value, item[1]))
    )
