from __future__ import annotations

import time
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .models import (
    ExecutionCapacityKey,
    FindMoneyConstraints,
    OptimizationDiagnostics,
    OptimizationResult,
    OptimizationStatus,
    PlanAction,
    PlanCandidate,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanStatus,
)
from .quantity import (
    GroupQuantityOption,
    QuantityCeiling,
    QuantityEnumerationCancelled,
    build_group_quantity_options,
    estimate_quantity_bundle_count,
)


class PlanningCancelled(RuntimeError):
    """Raised when a caller cancels optimization at a safe transition boundary."""


@dataclass(frozen=True, slots=True)
class OptimizerLimits:
    max_states: int = 2_000
    max_quantity_transitions: int = 2_000_000
    max_portfolio_transitions: int = 2_000_000

    def __post_init__(self) -> None:
        if isinstance(self.max_states, bool) or self.max_states < 2:
            raise ValueError("optimizer max_states must be at least 2")
        for name in ("max_quantity_transitions", "max_portfolio_transitions"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 2:
                raise ValueError(f"optimizer {name} must be at least 2")


DEFAULT_OPTIMIZER_LIMITS = OptimizerLimits()


@dataclass(frozen=True, slots=True)
class _GlobalState:
    group_options: tuple[GroupQuantityOption, ...]
    pre_revenue_cash: int
    focus: int
    expected_profit: int
    minimum_liquidity_rank: int
    action_count: int
    canonical_signature: tuple[tuple[str, int, int], ...]

    @classmethod
    def empty(cls) -> _GlobalState:
        return cls((), 0, 0, 0, 4, 0, ())


class PlanningOptimizer:
    """Exact grouped Pareto allocation with a deterministic bounded fallback.

    ``EXACT`` means exhaustive for the supplied integer-quantized planning
    model. It does not claim that Albion's currently unverified in-game Focus
    rounding is exact. When a retained frontier exceeds ``max_states``, the
    optimizer keeps a deterministic sample of feasible Pareto states and marks
    the complete result ``APPROXIMATE``.
    """

    def optimize(
        self,
        candidates: Sequence[PlanCandidate],
        ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
        constraints: FindMoneyConstraints,
        *,
        limits: OptimizerLimits = DEFAULT_OPTIMIZER_LIMITS,
        cancelled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> OptimizationResult:
        started = clock()
        _check_cancelled(cancelled)
        ordered_candidates = tuple(sorted(candidates, key=lambda value: value.canonical_key))
        _check_cancelled(cancelled)
        candidate_ids = [candidate.candidate_id for candidate in ordered_candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("planning candidate IDs must be unique")

        if any(len(candidate.capacity_requirements) > 1 for candidate in ordered_candidates):
            from .multicapacity import (
                MultiCapacityCancelled,
                MultiCapacityLimits,
                optimize_multicapacity,
            )

            try:
                built = optimize_multicapacity(
                    ordered_candidates,
                    ceilings,
                    constraints,
                    limits=MultiCapacityLimits(
                        limits.max_states,
                        limits.max_quantity_transitions,
                        limits.max_portfolio_transitions,
                    ),
                    cancelled=cancelled,
                    elapsed_seconds=0.0,
                )
            except MultiCapacityCancelled as error:
                raise PlanningCancelled("planning optimization was cancelled") from error
            return replace_elapsed(built.result, max(clock() - started, 0.0))

        grouped: dict[ExecutionCapacityKey, list[PlanCandidate]] = defaultdict(list)
        for index, candidate in enumerate(ordered_candidates):
            if index % 128 == 0:
                _check_cancelled(cancelled)
            if candidate.route.region is not constraints.region:
                raise ValueError("planning candidate region must match constraints")
            assert candidate.execution_capacity_key is not None
            grouped[candidate.execution_capacity_key].append(candidate)
        missing = tuple(key for key in grouped if key not in ceilings)
        if missing:
            raise ValueError(f"missing quantity ceilings for {len(missing)} capacity groups")
        capacity_group_count = len(grouped)

        total_bundle_count = 0
        for index, candidate in enumerate(ordered_candidates):
            if index % 128 == 0:
                _check_cancelled(cancelled)
            assert candidate.execution_capacity_key is not None
            total_bundle_count += estimate_quantity_bundle_count(
                candidate,
                ceilings[candidate.execution_capacity_key],
                constraints,
            )
        quantity_frontier_limit = _effective_quantity_frontier_limit(
            configured_limit=limits.max_states,
            transition_limit=limits.max_quantity_transitions,
            candidate_count=len(ordered_candidates),
            bundle_count=total_bundle_count,
        )
        quantity_limit_reason = (
            "quantity_transition_limit"
            if quantity_frontier_limit < limits.max_states
            else "frontier_state_limit"
        )
        minimum_quantity_work = 4 * total_bundle_count + 6 * len(ordered_candidates)
        quantity_work_exhausted = minimum_quantity_work > limits.max_quantity_transitions

        group_frontiers: list[tuple[ExecutionCapacityKey, tuple[GroupQuantityOption, ...]]] = []
        quantity_decisions = 0
        quantity_states_generated = 0
        quantity_states_after_pruning = 0
        quantity_states_pruned = 0
        portfolio_states_considered = 0
        portfolio_states_pruned = 0
        peak_frontier_size = 1
        approximate = False
        limit_reached = False
        approximation_reasons: set[str] = set()
        if quantity_work_exhausted:
            for index, key in enumerate(sorted(grouped, key=_capacity_order)):
                if index % 128 == 0:
                    _check_cancelled(cancelled)
                group_frontiers.append((key, (GroupQuantityOption.empty(key),)))
            quantity_states_after_pruning = len(group_frontiers)
            approximate = True
            limit_reached = True
            approximation_reasons.add("quantity_transition_limit")
        else:
            for key in sorted(grouped, key=_capacity_order):
                _check_cancelled(cancelled)
                ceiling = ceilings[key]
                if ceiling.execution_capacity_key != key:
                    raise ValueError("quantity ceiling mapping key does not match its value")
                try:
                    built = build_group_quantity_options(
                        tuple(grouped[key]),
                        ceiling,
                        constraints,
                        state_limit=quantity_frontier_limit,
                        state_limit_reason=quantity_limit_reason,
                        cancelled=cancelled,
                    )
                except QuantityEnumerationCancelled as error:
                    raise PlanningCancelled("planning optimization was cancelled") from error
                group_frontiers.append((key, built.options))
                quantity_decisions += built.quantity_decisions_considered
                quantity_states_generated += built.quantity_states_generated
                quantity_states_after_pruning += built.quantity_states_after_pruning
                quantity_states_pruned += built.states_pruned
                peak_frontier_size = max(peak_frontier_size, built.peak_frontier_size)
                approximate = approximate or built.approximate
                limit_reached = limit_reached or built.state_limit_reached
                approximation_reasons.update(built.approximation_reasons)

        (
            group_frontiers,
            options_bounded,
            preportfolio_states_pruned,
        ) = _bound_group_options_for_portfolio_work(
            group_frontiers,
            limits.max_portfolio_transitions,
            cancelled=cancelled,
        )
        portfolio_states_pruned += preportfolio_states_pruned
        if options_bounded:
            approximate = True
            limit_reached = True
            approximation_reasons.add("portfolio_transition_limit")

        frontier = [_GlobalState.empty()]
        total_group_options = sum(len(options) for _, options in group_frontiers)
        portfolio_frontier_limit = min(
            limits.max_states,
            max(
                2,
                limits.max_portfolio_transitions // max(total_group_options, 1),
            ),
        )
        streaming_threshold = max(portfolio_frontier_limit * 2, 256)
        for _, group_options in group_frontiers:
            _check_cancelled(cancelled)
            next_frontier: list[_GlobalState] = []
            for prior in frontier:
                for option_index, option in enumerate(group_options):
                    if option_index % 128 == 0:
                        _check_cancelled(cancelled)
                    portfolio_states_considered += 1
                    combined = _combine(prior, option)
                    if combined.pre_revenue_cash > constraints.silver_budget:
                        continue
                    if combined.focus > constraints.focus_budget:
                        continue
                    next_frontier.append(combined)
                    peak_frontier_size = max(peak_frontier_size, len(next_frontier))
                    if len(next_frontier) >= streaming_threshold:
                        reduced = _pareto_prune(next_frontier, cancelled=cancelled)
                        portfolio_states_pruned += len(next_frontier) - len(reduced)
                        next_frontier = reduced
                        if len(next_frontier) > portfolio_frontier_limit:
                            before_bounding = len(next_frontier)
                            next_frontier = _bounded_frontier(
                                next_frontier,
                                portfolio_frontier_limit,
                                cancelled=cancelled,
                            )
                            portfolio_states_pruned += before_bounding - len(next_frontier)
                            approximate = True
                            limit_reached = True
                            approximation_reasons.add(
                                "portfolio_transition_limit"
                                if portfolio_frontier_limit < limits.max_states
                                else "frontier_state_limit"
                            )
                _check_cancelled(cancelled)
            reduced = _pareto_prune(next_frontier, cancelled=cancelled)
            portfolio_states_pruned += len(next_frontier) - len(reduced)
            if len(reduced) > portfolio_frontier_limit:
                before_bounding = len(reduced)
                reduced = _bounded_frontier(
                    reduced,
                    portfolio_frontier_limit,
                    cancelled=cancelled,
                )
                portfolio_states_pruned += before_bounding - len(reduced)
                approximate = True
                limit_reached = True
                approximation_reasons.add(
                    "portfolio_transition_limit"
                    if portfolio_frontier_limit < limits.max_states
                    else "frontier_state_limit"
                )
            frontier = reduced

        best = min(frontier, key=_objective_order) if frontier else _GlobalState.empty()
        actions = _actions(best, ceilings, cancelled=cancelled)
        total_cash = sum(action.pre_revenue_cash_required for action in actions)
        total_focus = sum(action.focus_required for action in actions)
        total_profit = sum(action.expected_profit for action in actions)
        reasons = list(
            _deduplicate_reasons(reason for action in actions for reason in action.reasons)
        )
        status = OptimizationStatus.APPROXIMATE if approximate else OptimizationStatus.EXACT
        if approximate:
            cause_text = "; ".join(
                _approximation_reason_text(value) for value in sorted(approximation_reasons)
            )
            reasons.append(
                PlanReason(
                    PlanReasonCode.APPROXIMATE_OPTIMIZATION,
                    "A deterministic feasible Pareto sample was used because "
                    f"{cause_text}. Silver, Focus, and shared quantity constraints remain "
                    "enforced; only optimality is uncertain.",
                    PlanReasonSeverity.WARNING,
                )
            )
        if not actions:
            reasons.append(
                PlanReason(
                    PlanReasonCode.NO_FEASIBLE_ACTIONS,
                    "No positive-profit action fit the selected trust, quantity, silver, and "
                    "Focus constraints.",
                )
            )
            plan_status = PlanStatus.NON_ACTIONABLE
        elif any(reason.severity is PlanReasonSeverity.BLOCKING for reason in reasons):
            plan_status = PlanStatus.NON_ACTIONABLE
        elif any(reason.severity is PlanReasonSeverity.WARNING for reason in reasons):
            plan_status = PlanStatus.ADVISORY
        else:
            plan_status = PlanStatus.DECISION_GRADE

        elapsed = max(clock() - started, 0.0)
        states_considered = quantity_decisions + portfolio_states_considered
        states_pruned = quantity_states_pruned + portfolio_states_pruned
        diagnostics = OptimizationDiagnostics(
            method="binary_quantity_grouped_pareto_v2",
            status=status,
            candidate_count=len(ordered_candidates),
            group_count=capacity_group_count,
            quantity_decision_count=quantity_decisions,
            states_considered=states_considered,
            states_pruned=states_pruned,
            state_limit=limits.max_states,
            state_limit_reached=limit_reached,
            elapsed_seconds=elapsed,
            quantity_states_generated=quantity_states_generated,
            quantity_states_after_pruning=quantity_states_after_pruning,
            portfolio_states_considered=portfolio_states_considered,
            portfolio_states_pruned=portfolio_states_pruned,
            peak_frontier_size=peak_frontier_size,
            candidate_routes_before_pruning=len(ordered_candidates),
            candidate_routes_after_pruning=len(ordered_candidates),
            approximation_reasons=tuple(sorted(approximation_reasons)),
            effective_state_limit=min(quantity_frontier_limit, portfolio_frontier_limit),
            quantity_transition_limit=limits.max_quantity_transitions,
            portfolio_transition_limit=limits.max_portfolio_transitions,
        )
        return OptimizationResult(
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


def _effective_quantity_frontier_limit(
    *,
    configured_limit: int,
    transition_limit: int,
    candidate_count: int,
    bundle_count: int,
) -> int:
    """Choose the largest frontier whose conservative transition bound fits.

    Candidate-local binary expansion costs at most ``2 * bundles * states``;
    shared-group route combination costs at most
    ``candidates * states * (states + 1)``. This count-based policy is stable
    across machines and prevents a high craft cap from bypassing ``max_states``
    before the retained frontier exists.
    """

    if candidate_count == 0:
        return configured_limit

    def estimated_work(state_count: int) -> int:
        return 2 * bundle_count * state_count + candidate_count * state_count * (state_count + 1)

    if estimated_work(configured_limit) <= transition_limit:
        return configured_limit
    low = 2
    high = configured_limit
    while low < high:
        middle = (low + high + 1) // 2
        if estimated_work(middle) <= transition_limit:
            low = middle
        else:
            high = middle - 1
    return low


def _bound_group_options_for_portfolio_work(
    group_frontiers: list[tuple[ExecutionCapacityKey, tuple[GroupQuantityOption, ...]]],
    transition_limit: int,
    *,
    cancelled: Callable[[], bool] | None,
) -> tuple[
    list[tuple[ExecutionCapacityKey, tuple[GroupQuantityOption, ...]]],
    bool,
    int,
]:
    """Bound group-option input so a two-state portfolio fits its work policy."""

    total_options = sum(len(options) for _, options in group_frontiers)
    if 2 * total_options <= transition_limit:
        return (group_frontiers, False, 0)
    group_count = len(group_frontiers)
    if group_count == 0:
        return (group_frontiers, False, 0)
    if group_count > transition_limit:
        return ([], True, total_options)

    # Two retained options preserve a resource-minimal endpoint and the local
    # objective best. If there are too many groups even for that, retaining
    # each group's empty state yields a deterministic valid (empty) fallback.
    maximum_options_per_group = transition_limit // (2 * group_count)
    bounded: list[tuple[ExecutionCapacityKey, tuple[GroupQuantityOption, ...]]] = []
    pruned = 0
    for index, (key, options) in enumerate(group_frontiers):
        if index % 128 == 0:
            _check_cancelled(cancelled)
        if maximum_options_per_group < 2:
            selected = (GroupQuantityOption.empty(key),)
        elif len(options) > maximum_options_per_group:
            selected = tuple(
                _bounded_group_options(
                    list(options),
                    maximum_options_per_group,
                    cancelled=cancelled,
                )
            )
        else:
            selected = options
        pruned += len(options) - len(selected)
        bounded.append((key, selected))
    return (bounded, True, pruned)


def _bounded_group_options(
    options: list[GroupQuantityOption],
    limit: int,
    *,
    cancelled: Callable[[], bool] | None,
) -> list[GroupQuantityOption]:
    _check_cancelled(cancelled)
    ordered = sorted(options, key=_group_frontier_order)
    _check_cancelled(cancelled)
    best = min(options, key=_group_objective_order)
    minimum = ordered[0]
    selected = [option for option in ordered if option is minimum or option is best]
    pool = [option for option in ordered if option is not minimum and option is not best]
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
    return sorted(selected, key=_group_frontier_order)[:limit]


def _group_frontier_order(option: GroupQuantityOption) -> tuple:
    return (
        option.pre_revenue_cash,
        option.focus,
        -option.expected_profit,
        -option.minimum_liquidity_rank,
        option.action_count,
        option.canonical_signature,
    )


def _group_objective_order(option: GroupQuantityOption) -> tuple:
    return (
        -option.expected_profit,
        option.pre_revenue_cash,
        option.focus,
        -option.minimum_liquidity_rank,
        option.action_count,
        option.canonical_signature,
    )


def _approximation_reason_text(reason: str) -> str:
    return {
        "frontier_state_limit": "the configured Pareto frontier state limit was reached",
        "quantity_transition_limit": (
            "the named quantity-transition workload limit bounded the quantity frontier"
        ),
        "portfolio_transition_limit": (
            "the named portfolio-transition workload limit bounded the portfolio frontier"
        ),
    }.get(reason, reason.replace("_", " "))


def optimize_plan(
    candidates: Sequence[PlanCandidate],
    ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
    constraints: FindMoneyConstraints,
    *,
    limits: OptimizerLimits = DEFAULT_OPTIMIZER_LIMITS,
    cancelled: Callable[[], bool] | None = None,
) -> OptimizationResult:
    """Convenience entry point for callers that do not need an optimizer instance."""

    return PlanningOptimizer().optimize(
        candidates,
        ceilings,
        constraints,
        limits=limits,
        cancelled=cancelled,
    )


def _actions(
    state: _GlobalState,
    ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[PlanAction, ...]:
    actions: list[PlanAction] = []
    for option_index, option in enumerate(state.group_options):
        if option_index % 128 == 0:
            _check_cancelled(cancelled)
        actions.extend(
            allocation.to_plan_action(ceilings[option.execution_capacity_key])
            for allocation in option.allocations
        )
    _check_cancelled(cancelled)
    return tuple(sorted(actions, key=lambda action: action.canonical_key))


def _combine(prior: _GlobalState, option: GroupQuantityOption) -> _GlobalState:
    selected_options = (*prior.group_options, option) if option.allocations else prior.group_options
    return _GlobalState(
        selected_options,
        prior.pre_revenue_cash + option.pre_revenue_cash,
        prior.focus + option.focus,
        prior.expected_profit + option.expected_profit,
        min(prior.minimum_liquidity_rank, option.minimum_liquidity_rank),
        prior.action_count + option.action_count,
        (*prior.canonical_signature, *option.canonical_signature),
    )


def _pareto_prune(
    states: list[_GlobalState],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[_GlobalState]:
    """Return the exact two-resource frontier in O(n log n).

    Sorting by capital, Focus, and descending profit ensures every possible
    dominator has already been visited. A Fenwick prefix maximum then answers
    whether any visited state uses no more Focus and earns at least as much.
    Equal numeric triples are reduced first with the documented tie-breaker.
    """

    unique: dict[tuple[int, int, int], _GlobalState] = {}
    for index, state in enumerate(states):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        key = (state.pre_revenue_cash, state.focus, state.expected_profit)
        existing = unique.get(key)
        if existing is None or _tie_preferred(state, existing):
            unique[key] = state
    _check_cancelled(cancelled)
    candidates = sorted(unique.values(), key=_frontier_order)
    _check_cancelled(cancelled)
    focus_values = sorted({state.focus for state in candidates})
    prefix_maximum: list[int | None] = [None] * (len(focus_values) + 1)
    retained: list[_GlobalState] = []
    for index, state in enumerate(candidates):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        focus_index = bisect_left(focus_values, state.focus) + 1
        best_profit = _fenwick_query(prefix_maximum, focus_index)
        if best_profit is not None and best_profit >= state.expected_profit:
            continue
        retained.append(state)
        _fenwick_update(prefix_maximum, focus_index, state.expected_profit)
    return retained


def _fenwick_query(tree: list[int | None], index: int) -> int | None:
    result: int | None = None
    while index > 0:
        value = tree[index]
        if value is not None and (result is None or value > result):
            result = value
        index -= index & -index
    return result


def _fenwick_update(tree: list[int | None], index: int, value: int) -> None:
    while index < len(tree):
        existing = tree[index]
        if existing is None or value > existing:
            tree[index] = value
        index += index & -index


def _dominates(left: _GlobalState, right: _GlobalState) -> bool:
    if left is right:
        return False
    if left.pre_revenue_cash > right.pre_revenue_cash or left.focus > right.focus:
        return False
    if left.expected_profit < right.expected_profit:
        return False
    if left.expected_profit > right.expected_profit:
        return True
    if left.pre_revenue_cash < right.pre_revenue_cash:
        return True
    if left.focus < right.focus:
        return True
    return _tie_preferred(left, right)


def _tie_preferred(left: _GlobalState, right: _GlobalState) -> bool:
    return (
        -left.minimum_liquidity_rank,
        left.action_count,
        left.canonical_signature,
    ) < (
        -right.minimum_liquidity_rank,
        right.action_count,
        right.canonical_signature,
    )


def _frontier_order(state: _GlobalState) -> tuple:
    return (
        state.pre_revenue_cash,
        state.focus,
        -state.expected_profit,
        -state.minimum_liquidity_rank,
        state.action_count,
        state.canonical_signature,
    )


def _objective_order(state: _GlobalState) -> tuple:
    return (
        -state.expected_profit,
        state.pre_revenue_cash,
        state.focus,
        -state.minimum_liquidity_rank,
        state.action_count,
        state.canonical_signature,
    )


def _bounded_frontier(
    states: list[_GlobalState],
    limit: int,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[_GlobalState]:
    """Keep feasible frontier endpoints and an even deterministic resource sample."""

    _check_cancelled(cancelled)
    ordered = sorted(states, key=_frontier_order)
    _check_cancelled(cancelled)
    best = min(states, key=_objective_order)
    required_ids = {id(ordered[0]), id(best)}
    selected = [state for state in ordered if id(state) in required_ids]
    remaining_slots = limit - len(selected)
    pool = [state for state in ordered if id(state) not in required_ids]
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


def _capacity_order(key: ExecutionCapacityKey) -> tuple[str, str, str, int]:
    return (key[0].value, key[1], key[2].casefold(), key[3])


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise PlanningCancelled("planning optimization was cancelled")


def _deduplicate_reasons(reasons) -> tuple[PlanReason, ...]:
    unique = {(reason.code, reason.message, reason.severity): reason for reason in reasons}
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[2].value, item[0].value, item[1]),
        )
    )


def replace_elapsed(result: OptimizationResult, elapsed: float) -> OptimizationResult:
    """Attach caller-clock elapsed time without coupling the component solver to a clock."""

    from dataclasses import replace

    return replace(
        result,
        diagnostics=replace(result.diagnostics, elapsed_seconds=elapsed),
    )
