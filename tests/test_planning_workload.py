from albion_crafter.planning.optimizer import DEFAULT_OPTIMIZER_LIMITS
from albion_crafter.planning.workload import (
    DEFAULT_PLANNING_WORKLOAD_POLICY,
    PlanningWorkloadPolicy,
    assess_planning_workload,
)


def test_normal_workload_is_reported_without_warning() -> None:
    result = assess_planning_workload(
        candidate_routes=36,
        capacity_groups=4,
        focused_routes=36,
        requested_craft_cap=10,
    )

    assert result.conceptual_quantity_states == 2_340
    assert result.quantity_bundle_count == 288
    assert not result.likely_approximate
    assert result.warning is None


def test_ten_thousand_craft_cap_is_not_clamped_and_warns_about_approximation() -> None:
    result = assess_planning_workload(
        candidate_routes=1,
        capacity_groups=1,
        focused_routes=1,
        requested_craft_cap=10_000,
    )

    assert result.requested_craft_cap == 10_000
    assert result.conceptual_quantity_states == 50_015_000
    assert result.quantity_bundle_count == 28
    assert result.likely_approximate
    assert "50,015,000 conceptual Focus splits" in (result.warning or "")
    assert "Approximate" in (result.warning or "")
    assert "silver, Focus, and shared-quantity" in (result.warning or "")


def test_workload_policy_is_named_validated_and_visible() -> None:
    assert DEFAULT_PLANNING_WORKLOAD_POLICY.frontier_state_limit == 2_000
    assert DEFAULT_PLANNING_WORKLOAD_POLICY.quantity_transition_limit == 2_000_000
    assert DEFAULT_PLANNING_WORKLOAD_POLICY.portfolio_transition_limit == 2_000_000
    assert (
        DEFAULT_PLANNING_WORKLOAD_POLICY.frontier_state_limit == DEFAULT_OPTIMIZER_LIMITS.max_states
    )
    assert (
        DEFAULT_PLANNING_WORKLOAD_POLICY.quantity_transition_limit
        == DEFAULT_OPTIMIZER_LIMITS.max_quantity_transitions
    )
    assert (
        DEFAULT_PLANNING_WORKLOAD_POLICY.portfolio_transition_limit
        == DEFAULT_OPTIMIZER_LIMITS.max_portfolio_transitions
    )

    try:
        PlanningWorkloadPolicy(frontier_state_limit=0)
    except ValueError as error:
        assert "frontier_state_limit" in str(error)
    else:
        raise AssertionError("invalid workload policy was accepted")
