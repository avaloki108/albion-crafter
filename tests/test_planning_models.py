from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from albion_crafter.core.models import SaleMethod
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning import (
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationDiagnostics,
    OptimizationResult,
    OptimizationStatus,
    PlanAction,
    PlanDataHealth,
    PlanSnapshot,
    PlanStatus,
    QuantityCeilingSource,
    RefreshStatistics,
    TransportPolicy,
    calculate_quantity_ceiling,
    default_plan_assumptions,
    generate_candidate_routes,
    quantize_profit_down,
    quantize_resource_up,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def test_constraints_round_trip_preserves_reserves_search_and_station_policy() -> None:
    constraints = FindMoneyConstraints(
        available_silver=9_000_000,
        available_focus=10_000,
        silver_reserve=2_000_000,
        focus_reserve=1_000,
        item_query="sword",
        material_cities=("Martlock", "Thetford"),
        craft_cities=("Thetford",),
        sell_cities=("Bridgewatch",),
        allow_stale_station_fees=True,
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        per_item_craft_cap=25,
        force_current_price_refresh=True,
    )

    assert constraints.silver_budget == 7_000_000
    assert constraints.focus_budget == 9_000
    assert FindMoneyConstraints.from_dict(constraints.to_dict()) == constraints


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"silver_reserve": 101}, "silver_reserve"),
        ({"focus_reserve": 101}, "focus_reserve"),
        ({"transport_policy": TransportPolicy.EXPLICIT_COST}, "transport_cost"),
        ({"per_item_craft_cap": 0}, "per_item_craft_cap"),
        ({"historical_volume_share": 1.1}, "historical_volume_share"),
        ({"historical_volume_share": float("nan")}, "historical_volume_share"),
        ({"historical_volume_share": float("inf")}, "historical_volume_share"),
        ({"historical_volume_share": True}, "historical_volume_share"),
        ({"minimum_roi": float("nan")}, "minimum_roi"),
        ({"minimum_roi": float("inf")}, "minimum_roi"),
        ({"minimum_roi": True}, "minimum_roi"),
    ],
)
def test_constraints_reject_invalid_finite_bounds(changes: dict, message: str) -> None:
    values = {"available_silver": 100, "available_focus": 100, **changes}
    with pytest.raises(ValueError, match=message):
        FindMoneyConstraints(**values)


def test_resource_quantization_is_conservative_and_rejects_nonfinite() -> None:
    assert quantize_resource_up(Decimal("10.0001")) == 11
    assert quantize_profit_down(Decimal("10.9999")) == 10
    assert quantize_profit_down(Decimal("-10.0001")) == -11
    with pytest.raises(ValueError, match="finite"):
        quantize_resource_up(float("nan"))


def test_route_generation_applies_transport_policy_before_candidate_arithmetic() -> None:
    local = FindMoneyConstraints(
        1_000,
        0,
        material_cities=("Bridgewatch", "Martlock"),
        craft_cities=("Bridgewatch", "Martlock"),
        sell_cities=("Bridgewatch", "Martlock"),
    )
    local_result = generate_candidate_routes(local)
    assert len(local_result.routes) == 2
    assert local_result.combinations_considered == 8
    assert local_result.combinations_pruned == 6
    assert all(not route.is_cross_city for route in local_result.routes)

    uncosted = FindMoneyConstraints(
        1_000,
        0,
        material_cities=("Martlock",),
        craft_cities=("Thetford",),
        sell_cities=("Bridgewatch",),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
    )
    route = generate_candidate_routes(uncosted).routes[0]
    assert route.is_cross_city
    assert route.reasons[0].severity.value == "warning"

    explicit = FindMoneyConstraints(
        1_000,
        0,
        material_cities=("Martlock",),
        craft_cities=("Thetford",),
        sell_cities=("Bridgewatch",),
        transport_policy=TransportPolicy.EXPLICIT_COST,
        transport_cost_per_craft=75,
    )
    route = generate_candidate_routes(explicit).routes[0]
    assert route.transport_cost_per_craft == 75
    assert route.reasons[0].severity.value == "info"


def test_route_generation_observes_cancellation_inside_cartesian_expansion() -> None:
    constraints = FindMoneyConstraints(
        1_000,
        0,
        material_cities=tuple(f"Material {index}" for index in range(10)),
        craft_cities=tuple(f"Craft {index}" for index in range(10)),
        sell_cities=tuple(f"Sell {index}" for index in range(10)),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
    )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(RuntimeError, match="route generation was cancelled"):
        generate_candidate_routes(constraints, cancelled=cancelled)

    assert checks == 3


def test_quantity_ceiling_uses_output_units_and_zero_history_is_not_live_depth() -> None:
    key = (Region.AMERICAS, "T4_ITEM", "Bridgewatch", 1)
    historical = calculate_quantity_ceiling(
        key,
        explicit_craft_cap=50,
        history_enabled=True,
        reported_24h_volume=41,
        historical_volume_share=0.20,
    )
    assert historical.maximum_crafts == 50
    assert historical.maximum_output_units == 8
    assert historical.source is QuantityCeilingSource.HISTORICAL_VOLUME_SHARE

    empty_report = calculate_quantity_ceiling(
        key,
        explicit_craft_cap=3,
        history_enabled=True,
        reported_24h_volume=0,
        historical_volume_share=0.20,
    )
    assert empty_report.maximum_output_units is None
    assert empty_report.source is QuantityCeilingSource.EXPLICIT_FALLBACK_NO_HISTORY


@pytest.mark.parametrize(
    ("craft_cap", "reported_volume", "share", "message"),
    [
        (1.5, 10, 0.2, "explicit_craft_cap"),
        (True, 10, 0.2, "explicit_craft_cap"),
        (10, 1.5, 0.2, "reported_24h_volume"),
        (10, True, 0.2, "reported_24h_volume"),
        (10, 10, True, "historical_volume_share"),
        (10, 10, float("inf"), "historical_volume_share"),
    ],
)
def test_quantity_ceiling_rejects_noninteger_counts_and_nonfinite_share(
    craft_cap,
    reported_volume,
    share,
    message: str,
) -> None:
    key = (Region.AMERICAS, "T4_ITEM", "Bridgewatch", 1)
    with pytest.raises(ValueError, match=message):
        calculate_quantity_ceiling(
            key,
            explicit_craft_cap=craft_cap,
            history_enabled=True,
            reported_24h_volume=reported_volume,
            historical_volume_share=share,
        )


def test_snapshot_canonical_round_trip_preserves_action_evidence() -> None:
    constraints = FindMoneyConstraints(1_000, 100, history_enabled=False)
    route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Bridgewatch",
        TransportPolicy.LOCAL_ONLY,
    )
    key = (Region.AMERICAS, "T4_SWORD", "Bridgewatch", 1)
    action = PlanAction(
        "candidate-a",
        "T4_SWORD",
        "Broadsword",
        route,
        quantity=2,
        focused_quantity=1,
        nonfocused_quantity=1,
        output_units=2,
        quality=1,
        sale_method=SaleMethod.SELL_ORDER,
        pre_revenue_cash_required=200,
        focus_required=25,
        expected_profit=80,
        liquidity=LiquidityLevel.MODERATE,
        execution_capacity_key=key,
        quantity_ceiling=3,
        expected_revenue=500,
        effective_economic_cost=420,
        incremental_focus_profit=30,
        evidence=(("recipe", "T4_SWORD<-T4_METALBAR"), ("station", "500@2026-08-18")),
        oldest_market_observed_at=NOW - timedelta(minutes=20),
        station_fee_observed_at=NOW - timedelta(hours=2),
    )
    diagnostics = OptimizationDiagnostics(
        "grouped_pareto_integer_v1",
        OptimizationStatus.EXACT,
        1,
        1,
        4,
        5,
        2,
        20_000,
        False,
        0.01,
        quantity_states_generated=12,
        quantity_states_after_pruning=4,
        portfolio_states_considered=5,
        portfolio_states_pruned=2,
        peak_frontier_size=8,
        candidate_routes_before_pruning=2,
        candidate_routes_after_pruning=1,
        candidate_local_modes_removed=1,
        equivalent_routes_collapsed=1,
        effective_state_limit=2_000,
        quantity_transition_limit=2_000_000,
        portfolio_transition_limit=2_000_000,
    )
    result = OptimizationResult(
        (action,),
        200,
        25,
        80,
        800,
        75,
        PlanStatus.DECISION_GRADE,
        (),
        diagnostics,
    )
    snapshot = PlanSnapshot.from_optimization(
        snapshot_id="plan-1",
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        constraints=constraints,
        result=result,
        catalog_source_version="a" * 40,
        mechanics_ruleset_id="rules-v1",
        assumptions=default_plan_assumptions(constraints),
        data_health=PlanDataHealth(3, 3, 0, 0, 1, 1, 0, "verified"),
        current_refresh=RefreshStatistics(3, 1, 1, 0, 3, 0.2),
        metadata=(("rejections", "{}"),),
    )

    payload = snapshot.to_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    loaded = PlanSnapshot.from_dict(json.loads(encoded))
    assert loaded == snapshot
    assert loaded.actions[0].evidence == action.evidence
    assert loaded.actions[0].silver_per_focus == pytest.approx(1.2)
    assert loaded.optimizer == diagnostics
    assert loaded.optimizer.quantity_states_generated == 12
    assert payload["snapshot_format_version"] == 3

    legacy_payload = json.loads(encoded)
    for key in (
        "quantity_states_generated",
        "quantity_states_after_pruning",
        "portfolio_states_considered",
        "portfolio_states_pruned",
        "peak_frontier_size",
        "candidate_routes_before_pruning",
        "candidate_routes_after_pruning",
        "candidate_local_modes_removed",
        "equivalent_routes_collapsed",
        "approximation_reasons",
        "effective_state_limit",
        "quantity_transition_limit",
        "portfolio_transition_limit",
    ):
        legacy_payload["optimizer"].pop(key, None)
    legacy = PlanSnapshot.from_dict(legacy_payload)
    assert legacy.optimizer.quantity_states_generated == 0
    assert legacy.optimizer.approximation_reasons == ()
    assert legacy.to_dict()["optimizer"] == legacy_payload["optimizer"]


def test_explicit_transport_cost_must_be_present_in_candidate_economics() -> None:
    route = CandidateRoute(
        Region.AMERICAS,
        "Martlock",
        "Thetford",
        "Bridgewatch",
        TransportPolicy.EXPLICIT_COST,
        75,
    )
    with pytest.raises(ValueError, match="transport cost"):
        from albion_crafter.planning import PlanCandidate

        PlanCandidate(
            "candidate",
            "T4_SWORD",
            "Sword",
            route,
            CandidateEconomics(100, 20),
        )


def test_focus_profit_uplift_requires_positive_focus_commitment() -> None:
    with pytest.raises(ValueError, match="focus_per_focused_craft.*positive"):
        CandidateEconomics(
            pre_revenue_cash_per_craft=5,
            nonfocused_profit_per_craft=1,
            focused_profit_per_craft=2,
            focus_per_focused_craft=0,
        )
