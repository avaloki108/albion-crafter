from __future__ import annotations

import itertools
import random

import pytest

import albion_crafter.planning.quantity as quantity_module
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning import (
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationStatus,
    OptimizerLimits,
    PlanCandidate,
    PlanningCancelled,
    PlanReason,
    PlanReasonCode,
    PlanStatus,
    QuantityCeiling,
    QuantityCeilingSource,
    QuantityMaterializationLimitExceeded,
    TransportPolicy,
    optimize_plan,
)


def _route(
    *,
    material: str = "Bridgewatch",
    craft: str = "Bridgewatch",
    sell: str = "Bridgewatch",
) -> CandidateRoute:
    policy = (
        TransportPolicy.LOCAL_ONLY
        if len({material, craft, sell}) == 1
        else TransportPolicy.ACKNOWLEDGED_UNCOSTED
    )
    return CandidateRoute(Region.AMERICAS, material, craft, sell, policy)


def _candidate(
    candidate_id: str,
    *,
    item_id: str | None = None,
    cash: int,
    nonfocus_profit: int,
    focus_profit: int | None = None,
    focus_cost: int | None = None,
    nonfocused_eligible: bool = True,
    liquidity: LiquidityLevel = LiquidityLevel.MODERATE,
    output_quantity: int = 1,
    route: CandidateRoute | None = None,
) -> PlanCandidate:
    selected_route = route or _route()
    return PlanCandidate(
        candidate_id,
        item_id or f"ITEM_{candidate_id}",
        candidate_id,
        selected_route,
        CandidateEconomics(
            cash,
            nonfocus_profit,
            focus_profit,
            focus_cost,
            nonfocused_eligible,
        ),
        output_quantity_per_craft=output_quantity,
        liquidity=liquidity,
    )


def _ceilings(
    candidates: tuple[PlanCandidate, ...],
    *,
    crafts: int,
    output_units: int | None = None,
) -> dict:
    return {
        candidate.execution_capacity_key: QuantityCeiling(
            candidate.execution_capacity_key,
            crafts,
            output_units,
            QuantityCeilingSource.EXPLICIT_CAP,
            explanation="Test fixture explicit shared capacity.",
        )
        for candidate in candidates
    }


def _signature(result) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (action.candidate_id, action.focused_quantity, action.nonfocused_quantity)
        for action in result.actions
    )


def _brute_force(candidates, ceilings, constraints):
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.canonical_key))
    choices = []
    for candidate in ordered:
        ceiling = ceilings[candidate.execution_capacity_key]
        values = [(0, 0)]
        for total in range(1, ceiling.maximum_crafts + 1):
            for focused in range(total + 1):
                nonfocused = total - focused
                if focused and (
                    not constraints.use_focus or not candidate.economics.has_focused_variant
                ):
                    continue
                if nonfocused and not candidate.economics.nonfocused_eligible:
                    continue
                values.append((focused, nonfocused))
        choices.append(values)

    legal = []
    for allocation in itertools.product(*choices):
        cash = 0
        focus = 0
        profit = 0
        liquidity = 4
        action_count = 0
        signature = []
        crafts_by_key = {}
        output_by_key = {}
        for candidate, (focused, nonfocused) in zip(ordered, allocation, strict=True):
            total = focused + nonfocused
            if not total:
                continue
            action_count += 1
            signature.append((candidate.candidate_id, focused, nonfocused))
            cash += total * candidate.economics.pre_revenue_cash_per_craft
            focus += focused * (candidate.economics.focus_per_focused_craft or 0)
            profit += nonfocused * candidate.economics.nonfocused_profit_per_craft
            profit += focused * (candidate.economics.focused_profit_per_craft or 0)
            liquidity = min(liquidity, candidate.liquidity_rank)
            key = candidate.execution_capacity_key
            crafts_by_key[key] = crafts_by_key.get(key, 0) + total
            output_by_key[key] = (
                output_by_key.get(key, 0) + total * candidate.output_quantity_per_craft
            )
        if cash > constraints.silver_budget or focus > constraints.focus_budget:
            continue
        if any(
            crafts_by_key.get(key, 0) > ceiling.maximum_crafts for key, ceiling in ceilings.items()
        ):
            continue
        if any(
            ceiling.maximum_output_units is not None
            and output_by_key.get(key, 0) > ceiling.maximum_output_units
            for key, ceiling in ceilings.items()
        ):
            continue
        legal.append(
            (
                (
                    -profit,
                    cash,
                    focus,
                    -liquidity,
                    action_count,
                    tuple(signature),
                ),
                (cash, focus, profit, tuple(signature)),
            )
        )
    return min(legal)[1]


def _assert_brute_force_parity(candidates, ceilings, constraints) -> None:
    result = optimize_plan(candidates, ceilings, constraints)
    cash, focus, profit, signature = _brute_force(candidates, ceilings, constraints)
    assert result.diagnostics.status is OptimizationStatus.EXACT
    assert (
        result.total_pre_revenue_cash,
        result.total_focus,
        result.total_expected_profit,
        _signature(result),
    ) == (cash, focus, profit, signature)


def test_exact_optimizer_matches_brute_force_for_silver_only() -> None:
    candidates = (
        _candidate("a", cash=6, nonfocus_profit=9),
        _candidate("b", cash=5, nonfocus_profit=8),
    )
    constraints = FindMoneyConstraints(10, 0, use_focus=False, per_item_craft_cap=1)
    _assert_brute_force_parity(candidates, _ceilings(candidates, crafts=1), constraints)


def test_exact_optimizer_matches_brute_force_for_focus_only_and_both_resources() -> None:
    focus_candidates = (
        _candidate(
            "a",
            cash=0,
            nonfocus_profit=-1,
            focus_profit=10,
            focus_cost=2,
            nonfocused_eligible=False,
        ),
        _candidate(
            "b",
            cash=0,
            nonfocus_profit=-1,
            focus_profit=7,
            focus_cost=1,
            nonfocused_eligible=False,
        ),
    )
    focus_constraints = FindMoneyConstraints(0, 2, per_item_craft_cap=1)
    _assert_brute_force_parity(
        focus_candidates,
        _ceilings(focus_candidates, crafts=1),
        focus_constraints,
    )

    combined = (
        _candidate("cash", cash=5, nonfocus_profit=8),
        _candidate(
            "focus",
            cash=3,
            nonfocus_profit=-1,
            focus_profit=10,
            focus_cost=2,
            nonfocused_eligible=False,
        ),
    )
    constraints = FindMoneyConstraints(8, 2, per_item_craft_cap=1)
    _assert_brute_force_parity(combined, _ceilings(combined, crafts=1), constraints)


def test_mixed_focus_and_nonfocus_are_one_action_and_share_global_focus() -> None:
    candidates = (
        _candidate(
            "mixed",
            cash=100,
            nonfocus_profit=10,
            focus_profit=20,
            focus_cost=1,
        ),
    )
    constraints = FindMoneyConstraints(300, 2, per_item_craft_cap=3)
    ceilings = _ceilings(candidates, crafts=3)
    _assert_brute_force_parity(candidates, ceilings, constraints)
    result = optimize_plan(candidates, ceilings, constraints)
    assert _signature(result) == (("mixed", 2, 1),)
    assert result.total_expected_profit == 50
    assert result.actions[0].incremental_focus_profit == 20
    assert result.actions[0].silver_per_focus == 10
    assert "quantity_ceiling" in dict(result.actions[0].evidence)


def test_routes_and_focus_modes_share_output_market_capacity_for_selected_sale_method() -> None:
    routes = (
        _candidate(
            "martlock-route",
            item_id="SAME_ITEM",
            cash=1,
            nonfocus_profit=8,
            output_quantity=2,
            route=_route(material="Martlock"),
        ),
        _candidate(
            "thetford-route",
            item_id="SAME_ITEM",
            cash=1,
            nonfocus_profit=10,
            output_quantity=3,
            route=_route(material="Thetford"),
        ),
    )
    assert routes[0].execution_capacity_key == routes[1].execution_capacity_key
    constraints = FindMoneyConstraints(
        10,
        0,
        use_focus=False,
        material_cities=("Martlock", "Thetford"),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        per_item_craft_cap=2,
    )
    ceilings = _ceilings(routes, crafts=2, output_units=4)
    _assert_brute_force_parity(routes, ceilings, constraints)
    result = optimize_plan(routes, ceilings, constraints)
    assert sum(action.output_units for action in result.actions) <= 4


def test_zero_and_negative_profit_candidates_leave_the_plan_empty() -> None:
    candidates = (
        _candidate("negative", cash=0, nonfocus_profit=-1),
        _candidate("zero", cash=0, nonfocus_profit=0),
    )
    constraints = FindMoneyConstraints(0, 0, use_focus=False, per_item_craft_cap=2)
    _assert_brute_force_parity(candidates, _ceilings(candidates, crafts=2), constraints)
    result = optimize_plan(candidates, _ceilings(candidates, crafts=2), constraints)
    assert result.actions == ()
    assert result.plan_status is PlanStatus.NON_ACTIONABLE


def test_variant_scoped_nonfocus_rejection_keeps_focused_mode() -> None:
    candidate = _candidate(
        "focus-only",
        cash=1,
        nonfocus_profit=999,
        focus_profit=10,
        focus_cost=1,
        nonfocused_eligible=False,
    )
    candidate = PlanCandidate(
        candidate.candidate_id,
        candidate.item_id,
        candidate.display_name,
        candidate.route,
        candidate.economics,
        reasons=(
            PlanReason(
                PlanReasonCode.OTHER,
                "Non-Focus variant failed a mode-specific trust check.",
            ),
        ),
    )
    constraints = FindMoneyConstraints(2, 2, per_item_craft_cap=2)
    result = optimize_plan((candidate,), _ceilings((candidate,), crafts=2), constraints)
    assert _signature(result) == (("focus-only", 2, 0),)
    assert result.total_expected_profit == 20
    assert result.plan_status is PlanStatus.DECISION_GRADE


def test_deterministic_ties_prefer_liquidity_then_canonical_id() -> None:
    same_item = "TIE_ITEM"
    candidates = (
        _candidate(
            "z-low",
            item_id=same_item,
            cash=5,
            nonfocus_profit=10,
            liquidity=LiquidityLevel.LOW,
        ),
        _candidate(
            "b-high",
            item_id=same_item,
            cash=5,
            nonfocus_profit=10,
            liquidity=LiquidityLevel.HIGH,
        ),
        _candidate(
            "a-high",
            item_id=same_item,
            cash=5,
            nonfocus_profit=10,
            liquidity=LiquidityLevel.HIGH,
        ),
    )
    constraints = FindMoneyConstraints(5, 0, use_focus=False, per_item_craft_cap=1)
    ceilings = _ceilings(candidates, crafts=1)
    result = optimize_plan(tuple(reversed(candidates)), ceilings, constraints)
    assert _signature(result) == (("a-high", 0, 1),)


def test_deterministic_ties_prefer_lower_resources_then_fewer_actions() -> None:
    same_item = "RESOURCE_TIE"
    cheaper = (
        _candidate("expensive", item_id=same_item, cash=6, nonfocus_profit=10),
        _candidate("cheap", item_id=same_item, cash=5, nonfocus_profit=10),
    )
    constraints = FindMoneyConstraints(6, 0, use_focus=False, per_item_craft_cap=1)
    result = optimize_plan(cheaper, _ceilings(cheaper, crafts=1), constraints)
    assert _signature(result) == (("cheap", 0, 1),)

    one_action = _candidate(
        "one-action",
        item_id=same_item,
        cash=10,
        nonfocus_profit=-1,
        focus_profit=20,
        focus_cost=1,
        nonfocused_eligible=False,
        output_quantity=2,
    )
    focused_half = _candidate(
        "focused-half",
        item_id=same_item,
        cash=4,
        nonfocus_profit=-1,
        focus_profit=10,
        focus_cost=1,
        nonfocused_eligible=False,
    )
    plain_half = _candidate(
        "plain-half",
        item_id=same_item,
        cash=6,
        nonfocus_profit=10,
    )
    candidates = (one_action, focused_half, plain_half)
    constraints = FindMoneyConstraints(10, 1, per_item_craft_cap=2)
    ceilings = _ceilings(candidates, crafts=2, output_units=2)
    _assert_brute_force_parity(candidates, ceilings, constraints)
    result = optimize_plan(candidates, ceilings, constraints)
    assert _signature(result) == (("one-action", 1, 0),)


def test_state_cap_fallback_is_explicit_deterministic_and_feasible() -> None:
    candidate = _candidate("many", cash=1, nonfocus_profit=2)
    constraints = FindMoneyConstraints(20, 0, use_focus=False, per_item_craft_cap=10)
    ceiling = _ceilings((candidate,), crafts=10)
    first = optimize_plan(
        (candidate,),
        ceiling,
        constraints,
        limits=OptimizerLimits(max_states=2),
    )
    second = optimize_plan(
        (candidate,),
        ceiling,
        constraints,
        limits=OptimizerLimits(max_states=2),
    )
    assert first.diagnostics.status is OptimizationStatus.APPROXIMATE
    assert first.diagnostics.state_limit_reached
    assert first.plan_status is PlanStatus.ADVISORY
    assert _signature(first) == _signature(second)
    assert first.total_pre_revenue_cash <= constraints.silver_budget


def test_optimizer_cancellation_is_not_an_approximate_result() -> None:
    candidate = _candidate("a", cash=1, nonfocus_profit=2)
    with pytest.raises(PlanningCancelled):
        optimize_plan(
            (candidate,),
            _ceilings((candidate,), crafts=2),
            FindMoneyConstraints(2, 0, use_focus=False),
            cancelled=lambda: True,
        )


def test_optimizer_observes_cancellation_inside_group_quantity_enumeration() -> None:
    candidates = tuple(
        _candidate(
            f"route-{index}",
            item_id="SHARED_OUTPUT",
            cash=100 + index,
            nonfocus_profit=200 + index,
            focus_profit=250 + index,
            focus_cost=10,
        )
        for index in range(27)
    )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 5

    with pytest.raises(PlanningCancelled):
        optimize_plan(
            candidates,
            _ceilings(candidates, crafts=10),
            FindMoneyConstraints(100_000, 10_000, per_item_craft_cap=10),
            cancelled=cancelled,
        )
    assert checks > 5


def test_indexed_group_pareto_matches_pairwise_reference() -> None:
    randomizer = random.Random(20260818)
    key_candidate = _candidate(
        "pareto-key",
        item_id="PARETO_OUTPUT",
        cash=1,
        nonfocus_profit=1,
    )
    key = key_candidate.execution_capacity_key
    assert key is not None
    options = []
    for index in range(250):
        candidate = _candidate(
            f"pareto-{index:03}",
            item_id="PARETO_OUTPUT",
            cash=1,
            nonfocus_profit=1,
        )
        allocation = quantity_module.CandidateAllocation(candidate, 0, 1)
        crafts = randomizer.randint(1, 5)
        options.append(
            quantity_module.GroupQuantityOption(
                key,
                (allocation,),
                crafts,
                crafts * randomizer.randint(1, 3),
                randomizer.randint(0, 30),
                randomizer.randint(0, 15),
                randomizer.randint(-5, 50),
                randomizer.randint(0, 4),
            )
        )

    def tie_key(option):
        return (
            -option.minimum_liquidity_rank,
            option.action_count,
            option.canonical_signature,
        )

    def reference(include_capacity):
        unique = {}
        for option in options:
            numeric_key = (
                option.pre_revenue_cash,
                option.focus,
                option.expected_profit,
                option.total_crafts if include_capacity else 0,
                option.total_output_units if include_capacity else 0,
            )
            current = unique.get(numeric_key)
            if current is None or tie_key(option) < tie_key(current):
                unique[numeric_key] = option
        candidates = sorted(
            unique.values(),
            key=lambda option: (
                option.pre_revenue_cash,
                option.focus,
                -option.expected_profit,
                *tie_key(option),
            ),
        )

        def dominates(left, right):
            if left is right:
                return False
            if (
                left.pre_revenue_cash > right.pre_revenue_cash
                or left.focus > right.focus
                or left.expected_profit < right.expected_profit
            ):
                return False
            if include_capacity and (
                left.total_crafts > right.total_crafts
                or left.total_output_units > right.total_output_units
            ):
                return False
            return (
                left.expected_profit > right.expected_profit
                or left.pre_revenue_cash < right.pre_revenue_cash
                or left.focus < right.focus
                or tie_key(left) < tie_key(right)
            )

        return tuple(
            option
            for option in candidates
            if not any(dominates(other, option) for other in candidates)
        )

    for include_capacity in (False, True):
        assert tuple(
            quantity_module._pareto_prune(
                options,
                include_capacity=include_capacity,
            )
        ) == reference(include_capacity)


def test_indexed_group_pareto_observes_cancellation_during_pruning() -> None:
    candidate = _candidate(
        "cancel-prune",
        item_id="CANCEL_OUTPUT",
        cash=1,
        nonfocus_profit=1,
    )
    key = candidate.execution_capacity_key
    assert key is not None
    allocation = quantity_module.CandidateAllocation(candidate, 0, 1)
    options = [
        quantity_module.GroupQuantityOption(
            key,
            (allocation,),
            1,
            1,
            index,
            index % 17,
            index * 2,
            2,
        )
        for index in range(2_048)
    ]
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 16

    with pytest.raises(quantity_module.QuantityEnumerationCancelled):
        quantity_module._pareto_prune(
            options,
            include_capacity=True,
            cancelled=cancelled,
        )
    assert checks > 16


def test_competing_route_group_uses_subquadratic_dominance_index(monkeypatch) -> None:
    """Guard operation growth without relying on a machine-specific stopwatch."""

    candidates = tuple(
        _candidate(
            f"route-{index}",
            item_id="SHARED_OUTPUT",
            cash=100 + index,
            nonfocus_profit=200 + index,
            focus_profit=250 + index,
            focus_cost=10,
        )
        for index in range(27)
    )
    constraints = FindMoneyConstraints(
        100_000,
        10_000,
        per_item_craft_cap=10,
    )
    comparisons = 0
    original = quantity_module._dominance_entry_preferred

    def counted_comparison(left, right):
        nonlocal comparisons
        comparisons += 1
        return original(left, right)

    monkeypatch.setattr(
        quantity_module,
        "_dominance_entry_preferred",
        counted_comparison,
    )
    result = optimize_plan(
        candidates,
        _ceilings(candidates, crafts=10),
        constraints,
        limits=OptimizerLimits(max_states=2_000),
    )

    assert result.diagnostics.quantity_decision_count < 500_000
    assert comparisons < 1_200_000
    assert _signature(result) == (("route-26", 10, 0),)
    assert result.diagnostics.quantity_states_generated > 0
    assert result.diagnostics.quantity_states_after_pruning > 0
    assert result.diagnostics.peak_frontier_size > 0


def test_very_large_mixed_craft_cap_uses_bounded_binary_quantity_frontier() -> None:
    candidate = _candidate(
        "large-cap",
        cash=1,
        nonfocus_profit=2,
        focus_profit=3,
        focus_cost=1,
    )
    constraints = FindMoneyConstraints(
        10_000,
        10_000,
        per_item_craft_cap=10_000,
    )

    first = optimize_plan(
        (candidate,),
        _ceilings((candidate,), crafts=10_000),
        constraints,
    )
    second = optimize_plan(
        (candidate,),
        _ceilings((candidate,), crafts=10_000),
        constraints,
    )

    assert first.diagnostics.status is OptimizationStatus.APPROXIMATE
    assert first.diagnostics.approximation_reasons == ("quantity_transition_limit",)
    assert first.diagnostics.quantity_decision_count < 100_000
    assert first.diagnostics.quantity_states_generated < 100_000
    assert first.diagnostics.effective_state_limit >= 2
    assert first.total_pre_revenue_cash <= constraints.silver_budget
    assert first.total_focus <= constraints.focus_budget
    assert sum(action.quantity for action in first.actions) <= 10_000
    assert _signature(first) == _signature(second)


def test_direct_large_allocation_materialization_fails_before_building_millions() -> None:
    candidate = _candidate(
        "explicit-large-cap",
        cash=1,
        nonfocus_profit=2,
        focus_profit=3,
        focus_cost=1,
    )
    constraints = FindMoneyConstraints(10_000, 10_000, per_item_craft_cap=10_000)
    ceiling = _ceilings((candidate,), crafts=10_000)[candidate.execution_capacity_key]

    with pytest.raises(
        QuantityMaterializationLimitExceeded,
        match="50,015,000 states.*production optimizer",
    ):
        quantity_module.enumerate_candidate_allocations(candidate, ceiling, constraints)


def test_low_portfolio_work_limit_retains_two_endpoints_and_feasible_result() -> None:
    candidates = tuple(
        _candidate(
            f"limited-{index:02}",
            cash=1 + index,
            nonfocus_profit=2 + index,
            focus_profit=3 + index,
            focus_cost=1,
        )
        for index in range(20)
    )
    constraints = FindMoneyConstraints(100, 10, per_item_craft_cap=1)
    limits = OptimizerLimits(
        max_states=2_000,
        max_quantity_transitions=2_000_000,
        max_portfolio_transitions=100,
    )

    first = optimize_plan(candidates, _ceilings(candidates, crafts=1), constraints, limits=limits)
    second = optimize_plan(
        tuple(reversed(candidates)),
        _ceilings(candidates, crafts=1),
        constraints,
        limits=limits,
    )

    assert first.diagnostics.status is OptimizationStatus.APPROXIMATE
    assert "portfolio_transition_limit" in first.diagnostics.approximation_reasons
    assert first.diagnostics.effective_state_limit == 2
    assert first.diagnostics.portfolio_states_considered <= 100
    assert first.total_pre_revenue_cash <= constraints.silver_budget
    assert first.total_focus <= constraints.focus_budget
    assert _signature(first) == _signature(second)


def test_exhausted_quantity_work_policy_returns_explicit_feasible_fallback() -> None:
    candidates = (
        _candidate(
            "quantity-limit-a",
            cash=1,
            nonfocus_profit=2,
            focus_profit=3,
            focus_cost=1,
        ),
        _candidate(
            "quantity-limit-b",
            cash=1,
            nonfocus_profit=2,
            focus_profit=3,
            focus_cost=1,
        ),
    )
    constraints = FindMoneyConstraints(100, 100, per_item_craft_cap=100)
    result = optimize_plan(
        candidates,
        _ceilings(candidates, crafts=100),
        constraints,
        limits=OptimizerLimits(
            max_states=2_000,
            max_quantity_transitions=100,
            max_portfolio_transitions=2_000_000,
        ),
    )

    assert result.diagnostics.status is OptimizationStatus.APPROXIMATE
    assert result.diagnostics.approximation_reasons == ("quantity_transition_limit",)
    assert result.diagnostics.quantity_decision_count == 0
    assert result.actions == ()
    assert result.total_pre_revenue_cash == 0
    assert result.total_focus == 0


def test_cancellation_is_checked_inside_large_cap_binary_quantity_generation() -> None:
    candidate = _candidate(
        "cancel-large-cap",
        cash=1,
        nonfocus_profit=2,
        focus_profit=3,
        focus_cost=1,
    )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 20

    with pytest.raises(PlanningCancelled):
        optimize_plan(
            (candidate,),
            _ceilings((candidate,), crafts=10_000),
            FindMoneyConstraints(10_000, 10_000, per_item_craft_cap=10_000),
            cancelled=cancelled,
        )
    assert checks > 20


def test_seeded_small_fixtures_match_brute_force() -> None:
    randomizer = random.Random(20260818)
    for fixture in range(20):
        candidates = tuple(
            _candidate(
                f"{fixture}-{index}",
                item_id=f"GROUP_{index % 2}",
                cash=randomizer.randint(0, 5),
                nonfocus_profit=randomizer.randint(-2, 9),
                focus_profit=randomizer.randint(-2, 12),
                focus_cost=randomizer.randint(1, 3),
                liquidity=randomizer.choice(tuple(LiquidityLevel)),
            )
            for index in range(randomizer.randint(1, 5))
        )
        constraints = FindMoneyConstraints(
            randomizer.randint(0, 12),
            randomizer.randint(0, 6),
            per_item_craft_cap=randomizer.randint(1, 3),
        )
        ceilings = _ceilings(candidates, crafts=constraints.per_item_craft_cap)
        _assert_brute_force_parity(candidates, ceilings, constraints)


def test_realistic_200_candidate_frontier_is_bounded_and_reports_work() -> None:
    candidates = tuple(
        _candidate(
            f"performance-{index:03}",
            cash=1_000 + (index % 13) * 100,
            nonfocus_profit=100 + (index % 17) * 10,
            focus_profit=150 + (index % 19) * 12,
            focus_cost=50 + (index % 7) * 5,
        )
        for index in range(200)
    )
    constraints = FindMoneyConstraints(
        200_000,
        5_000,
        per_item_craft_cap=3,
        history_enabled=False,
    )
    result = optimize_plan(
        candidates,
        _ceilings(candidates, crafts=3),
        constraints,
        limits=OptimizerLimits(max_states=500),
    )

    assert result.diagnostics.candidate_count == 200
    assert result.diagnostics.states_considered > 200
    assert result.diagnostics.states_pruned > 0
    assert result.diagnostics.state_limit_reached
    assert result.diagnostics.status is OptimizationStatus.APPROXIMATE
    assert result.total_pre_revenue_cash <= constraints.silver_budget
    assert result.total_focus <= constraints.focus_budget
    assert result.diagnostics.elapsed_seconds >= 0
