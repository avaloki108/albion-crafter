#!/usr/bin/env python3
"""Print non-gating synthetic V0.6 planner performance diagnostics."""

from __future__ import annotations

import time
from dataclasses import dataclass

from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning import (
    ActionKind,
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizerLimits,
    PlanCandidate,
    QuantityCeiling,
    QuantityCeilingSource,
    TransportPolicy,
    optimize_plan,
)


@dataclass(frozen=True, slots=True)
class ProfileCase:
    name: str
    candidates: tuple[PlanCandidate, ...]
    ceilings: dict
    constraints: FindMoneyConstraints
    limits: OptimizerLimits


def _candidate(index: int, *, item_id: str, shared_group: bool) -> PlanCandidate:
    sell_city = "Bridgewatch"
    material_city = f"Route {index}" if shared_group else "Bridgewatch"
    policy = TransportPolicy.ACKNOWLEDGED_UNCOSTED if shared_group else TransportPolicy.LOCAL_ONLY
    return PlanCandidate(
        f"{(ActionKind.CRAFT if index % 2 == 0 else ActionKind.REFINE).value}|"
        f"candidate-{item_id}-{index}",
        item_id,
        item_id,
        CandidateRoute(
            Region.AMERICAS,
            material_city,
            "Bridgewatch",
            sell_city,
            policy,
        ),
        CandidateEconomics(
            900 + index % 17 * 25,
            100 + index % 23 * 7,
            145 + index % 29 * 9,
            40 + index % 11,
        ),
        action_kind=ActionKind.CRAFT if index % 2 == 0 else ActionKind.REFINE,
        liquidity=LiquidityLevel.MODERATE,
    )


def _ceilings(candidates: tuple[PlanCandidate, ...], cap: int) -> dict:
    return {
        candidate.execution_capacity_key: QuantityCeiling(
            candidate.execution_capacity_key,
            cap,
            cap,
            QuantityCeilingSource.EXPLICIT_CAP,
            explanation="Synthetic V0.6 unified-planner performance diagnostic.",
        )
        for candidate in candidates
    }


def competing_routes(count: int) -> ProfileCase:
    candidates = tuple(
        _candidate(index, item_id="SHARED_OUTPUT", shared_group=True) for index in range(count)
    )
    return ProfileCase(
        f"competing_routes_{count}",
        candidates,
        _ceilings(candidates, 10),
        FindMoneyConstraints(100_000, 10_000, per_item_craft_cap=10),
        OptimizerLimits(max_states=2_000),
    )


def portfolio(count: int) -> ProfileCase:
    candidates = tuple(
        _candidate(index, item_id=f"OUTPUT_{index:04}", shared_group=False)
        for index in range(count)
    )
    return ProfileCase(
        f"portfolio_{count}",
        candidates,
        _ceilings(candidates, 3),
        FindMoneyConstraints(250_000, 20_000, per_item_craft_cap=3),
        OptimizerLimits(max_states=500),
    )


def large_cap() -> ProfileCase:
    candidates = (_candidate(0, item_id="LARGE_CAP", shared_group=False),)
    return ProfileCase(
        "large_cap_10000",
        candidates,
        _ceilings(candidates, 10_000),
        FindMoneyConstraints(20_000_000, 1_000_000, per_item_craft_cap=10_000),
        OptimizerLimits(max_states=2_000),
    )


def main() -> None:
    cases = (
        *(competing_routes(count) for count in (9, 18, 36)),
        *(portfolio(count) for count in (200, 500, 1_000)),
        large_cap(),
    )
    print(
        "case,routes,groups,cap,status,quantity_generated,quantity_retained,"
        "portfolio_considered,portfolio_pruned,peak_frontier,transitions,elapsed_seconds"
    )
    for case in cases:
        started = time.perf_counter()
        result = optimize_plan(
            case.candidates,
            case.ceilings,
            case.constraints,
            limits=case.limits,
        )
        elapsed = time.perf_counter() - started
        diagnostics = result.diagnostics
        print(
            f"{case.name},{len(case.candidates)},{diagnostics.group_count},"
            f"{case.constraints.per_item_craft_cap},{diagnostics.status.value},"
            f"{diagnostics.quantity_states_generated},"
            f"{diagnostics.quantity_states_after_pruning},"
            f"{diagnostics.portfolio_states_considered},"
            f"{diagnostics.portfolio_states_pruned},{diagnostics.peak_frontier_size},"
            f"{diagnostics.states_considered},{elapsed:.6f}"
        )


if __name__ == "__main__":
    main()
