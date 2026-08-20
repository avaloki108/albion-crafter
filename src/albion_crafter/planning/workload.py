from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlanningWorkloadPolicy:
    """Named V0.6 limits for accidental planner-workload protection.

    These values mirror the production optimizer defaults.  They are an
    explicit safety policy, not hidden changes to a user's requested quantity:
    frontiers exceeding the limits use deterministic feasible approximation
    and retain the original action-unit/batch cap as evidence.
    """

    frontier_state_limit: int = 2_000
    quantity_transition_limit: int = 2_000_000
    portfolio_transition_limit: int = 2_000_000
    candidate_route_warning_threshold: int = 5_000
    capacity_group_warning_threshold: int = 1_000

    def __post_init__(self) -> None:
        for name in (
            "frontier_state_limit",
            "quantity_transition_limit",
            "portfolio_transition_limit",
            "candidate_route_warning_threshold",
            "capacity_group_warning_threshold",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_PLANNING_WORKLOAD_POLICY = PlanningWorkloadPolicy()


@dataclass(frozen=True, slots=True)
class PlanningWorkloadAssessment:
    candidate_routes: int
    capacity_groups: int
    focused_routes: int
    requested_craft_cap: int
    conceptual_quantity_states: int
    quantity_bundle_count: int
    estimated_portfolio_frontier_work: int
    likely_approximate: bool
    warning: str | None

    def __post_init__(self) -> None:
        for name in (
            "candidate_routes",
            "capacity_groups",
            "focused_routes",
            "requested_craft_cap",
            "conceptual_quantity_states",
            "quantity_bundle_count",
            "estimated_portfolio_frontier_work",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.focused_routes > self.candidate_routes:
            raise ValueError("focused_routes cannot exceed candidate_routes")
        if self.candidate_routes and self.requested_craft_cap < 1:
            raise ValueError("requested_craft_cap must be positive when routes exist")


def assess_planning_workload(
    *,
    candidate_routes: int,
    capacity_groups: int,
    focused_routes: int,
    requested_craft_cap: int,
    policy: PlanningWorkloadPolicy = DEFAULT_PLANNING_WORKLOAD_POLICY,
) -> PlanningWorkloadAssessment:
    """Estimate pre-optimization shape without pretending to predict runtime.

    ``conceptual_quantity_states`` is the number of non-empty Focus/non-Focus
    splits a naive generator would materialize.  The production generator uses
    logarithmic bounded-integer bundles instead, so this value is a transparent
    warning signal rather than work that will actually be allocated.
    """

    for name, value in (
        ("candidate_routes", candidate_routes),
        ("capacity_groups", capacity_groups),
        ("focused_routes", focused_routes),
        ("requested_craft_cap", requested_craft_cap),
    ):
        if isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if focused_routes > candidate_routes:
        raise ValueError("focused_routes cannot exceed candidate_routes")
    if candidate_routes and requested_craft_cap < 1:
        raise ValueError("requested_craft_cap must be positive when routes exist")

    nonfocused_routes = candidate_routes - focused_routes
    mixed_states_per_route = requested_craft_cap * (requested_craft_cap + 3) // 2
    conceptual_states = (
        focused_routes * mixed_states_per_route + nonfocused_routes * requested_craft_cap
    )
    bundles_per_mode = math.ceil(math.log2(requested_craft_cap + 1)) if requested_craft_cap else 0
    bundle_count = bundles_per_mode * (candidate_routes + focused_routes)
    estimated_portfolio_work = capacity_groups * policy.frontier_state_limit

    reasons: list[str] = []
    if conceptual_states > policy.quantity_transition_limit:
        reasons.append(
            f"the requested route/quantity shape has {conceptual_states:,} conceptual Focus splits"
        )
    if estimated_portfolio_work > policy.portfolio_transition_limit:
        reasons.append(f"the portfolio frontier estimate is {estimated_portfolio_work:,} states")
    if candidate_routes > policy.candidate_route_warning_threshold:
        reasons.append(f"preflight retained {candidate_routes:,} candidate routes")
    if capacity_groups > policy.capacity_group_warning_threshold:
        reasons.append(f"preflight retained {capacity_groups:,} market-capacity keys")

    warning = None
    if reasons:
        warning = (
            "This configuration may require a very large optimization search because "
            + "; ".join(reasons)
            + ". The planner will preserve every silver, Focus, and shared-quantity "
            f"constraint, but its documented {policy.frontier_state_limit:,}-state and "
            f"{policy.quantity_transition_limit:,}/{policy.portfolio_transition_limit:,} "
            "transition safety limits may make the result Approximate. Narrow cities, items, "
            "or the per-market action-unit/batch cap when an Exact result is required."
        )
    return PlanningWorkloadAssessment(
        candidate_routes,
        capacity_groups,
        focused_routes,
        requested_craft_cap,
        conceptual_states,
        bundle_count,
        estimated_portfolio_work,
        bool(reasons),
        warning,
    )
