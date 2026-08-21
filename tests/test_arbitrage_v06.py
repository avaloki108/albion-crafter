from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.arbitrage import calculate_arbitrage_economics
from albion_crafter.core.mechanics import CURRENT_RULES
from albion_crafter.core.models import (
    ActionKind,
    Item,
    MaterialRequirement,
    Recipe,
    SaleMethod,
)
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    HistoryCoverage,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import MarketPrice, MarketSide, Region
from albion_crafter.planning.arbitrage import ArbitrageCandidateEvaluator
from albion_crafter.planning.export import export_plan_csv
from albion_crafter.planning.models import (
    OUTER_ROYAL_CITIES,
    CandidateEconomics,
    CandidateRoute,
    CapacityRequirement,
    CapacityRole,
    FindMoneyConstraints,
    OptimizationStatus,
    PlanCandidate,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanSnapshot,
    PlanStatus,
    TransportPolicy,
)
from albion_crafter.planning.optimizer import OptimizerLimits, PlanningOptimizer
from albion_crafter.planning.preflight import (
    EligibleArbitrageRoute,
    FindMoneyPreflightPlanner,
)
from albion_crafter.planning.quantity import QuantityCeiling, QuantityCeilingSource
from albion_crafter.planning.routes import generate_arbitrage_routes
from albion_crafter.planning.validation import (
    action_evidence_hook,
    default_freshness_hooks,
    validate_plan,
)

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def _constraints(**changes) -> FindMoneyConstraints:
    values = {
        "available_silver": 10_000,
        "available_focus": 10_000,
        "action_kinds": frozenset({ActionKind.ARBITRAGE}),
        "transport_policy": TransportPolicy.EXPLICIT_COST,
        "transport_cost_per_craft": 5,
        "history_enabled": False,
        "arbitrage_source_cities": ("Bridgewatch",),
        "arbitrage_destination_cities": ("Thetford",),
    }
    values.update(changes)
    return FindMoneyConstraints(**values)


def _market(item_id: str, city: str, sell: int, buy: int) -> MarketPrice:
    return MarketPrice(
        item_id,
        city,
        1,
        Region.AMERICAS,
        sell,
        NOW,
        buy,
        NOW,
        NOW,
        Provenance.AODP_CACHED,
    )


def _market_at(
    item_id: str,
    city: str,
    sell: int,
    buy: int,
    observed_at: datetime,
) -> MarketPrice:
    return MarketPrice(
        item_id,
        city,
        1,
        Region.AMERICAS,
        sell,
        observed_at,
        buy,
        observed_at,
        NOW,
        Provenance.AODP_CACHED,
    )


def _ceiling(key, maximum: int) -> QuantityCeiling:
    return QuantityCeiling(
        key,
        maximum,
        maximum,
        QuantityCeilingSource.HISTORICAL_VOLUME_SHARE,
        maximum,
        0.2,
        "Test history ceiling.",
    )


def test_arbitrage_routes_are_outer_royal_cross_city_and_transport_explicit() -> None:
    constraints = _constraints(
        arbitrage_source_cities=OUTER_ROYAL_CITIES,
        arbitrage_destination_cities=OUTER_ROYAL_CITIES,
    )
    result = generate_arbitrage_routes(constraints)
    assert result.combinations_considered == 25
    assert len(result.routes) == 20
    assert all(route.buy_city != route.sell_city for route in result.routes)
    assert all(route.transport_cost_per_action_unit == 5 for route in result.routes)
    assert {route.buy_city for route in result.routes} == set(OUTER_ROYAL_CITIES)
    assert (
        generate_arbitrage_routes(
            _constraints(
                transport_policy=TransportPolicy.LOCAL_ONLY,
                transport_cost_per_craft=None,
            )
        ).routes
        == ()
    )
    with pytest.raises(ValueError, match="outer Royal"):
        _constraints(arbitrage_source_cities=("Caerleon",))


@pytest.mark.parametrize(
    ("sale_method", "destination_side"),
    (
        (SaleMethod.SELL_ORDER, MarketSide.SELL_ORDER),
        (SaleMethod.INSTANT_SELL, MarketSide.BUY_ORDER),
    ),
)
def test_arbitrage_preflight_is_network_free_sparse_and_has_no_station_or_fce(
    tmp_path,
    sale_method,
    destination_side,
) -> None:
    database = Database(tmp_path / f"arbitrage-{sale_method.value}.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    stations = StationFeeRepository(database)
    profiles = CraftingProfileRepository(database)
    history = MarketHistoryRepository(database)
    output = Item("T5_MAIN_SWORD", "Clarent Blade", 5, crafting_category="sword")
    material = Item("T5_METALBAR", "Titanium Steel Bar", 5)
    recipe = Recipe(
        output,
        1,
        (MaterialRequirement(material.item_id, 16, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="arbitrage-fixture",
    )
    catalog.replace_all(
        (
            CatalogItem(output, 100, True, Provenance.STATIC_GAME_DATA, "fixture"),
            CatalogItem(material, 10, False, Provenance.STATIC_GAME_DATA, "fixture"),
        ),
        (recipe,),
        CatalogImport("fixture", "memory://fixture", "fixture", NOW, NOW, 2, 1),
    )
    history.set_coverage(
        HistoryCoverage(
            Region.AMERICAS,
            output.item_id,
            "Bridgewatch",
            1,
            HistoryTimeScale.SIX_HOURLY,
            NOW.replace(day=12),
            NOW,
            NOW,
            "success",
            28,
        )
    )
    planner = FindMoneyPreflightPlanner(
        catalog,
        market,
        overrides,
        stations,
        profiles,
        history,
    )
    constraints = _constraints(
        sale_method=sale_method,
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        transport_cost_per_craft=None,
        arbitrage_source_cities=("Bridgewatch", "Martlock"),
        arbitrage_destination_cities=("Martlock", "Thetford"),
    )
    preflight = planner.build(constraints, as_of=NOW)
    assert preflight.summary.arbitrage_items == 1
    assert preflight.summary.arbitrage_routes == 3
    assert preflight.eligible == ()
    assert preflight.station_requirements == ()
    assert preflight.focus_requirements == ()
    assert len(preflight.market_refresh.refresh_keys) == 3
    assert preflight.summary.missing_current_requirements == 3
    assert preflight.summary.arbitrage_source_history_keys == 2
    assert preflight.summary.arbitrage_destination_history_keys == 2
    assert preflight.summary.history_capacity_keys == 3
    assert preflight.summary.cached_history_keys == 1
    assert preflight.summary.history_gaps == 2
    assert preflight.summary.estimated_capacity_components == 1
    assert {route.destination_price_side for route in preflight.arbitrage_routes} == {
        destination_side
    }
    assert {route.source_price_side for route in preflight.arbitrage_routes} == {
        MarketSide.SELL_ORDER
    }


def test_arbitrage_economics_keeps_tax_out_of_pre_revenue_cash() -> None:
    result = calculate_arbitrage_economics(
        100,
        200,
        premium=True,
        sale_method=SaleMethod.SELL_ORDER,
        transport_cash_per_unit=5,
    )
    assert result.purchase_cash == 100
    assert result.setup_cash == 5
    assert result.transaction_tax == 8
    assert result.transport_cash == 5
    assert result.pre_revenue_cash == 110
    assert result.expected_profit == 82
    assert result.effective_economic_cost == 118


@pytest.mark.parametrize(
    ("sale_method", "destination_price", "expected_profit"),
    (
        (SaleMethod.SELL_ORDER, 200, 82),
        (SaleMethod.INSTANT_SELL, 180, 67),
    ),
)
def test_arbitrage_evaluator_and_final_validator_recompute_immutable_evidence(
    tmp_path,
    sale_method,
    destination_price,
    expected_profit,
) -> None:
    constraints = _constraints(sale_method=sale_method)
    route = generate_arbitrage_routes(constraints).routes[0]
    destination_side = (
        MarketSide.SELL_ORDER if sale_method is SaleMethod.SELL_ORDER else MarketSide.BUY_ORDER
    )
    eligible = EligibleArbitrageRoute(
        Item("T5_METALBAR", "Titanium Steel Bar", 5, crafting_category="ore"),
        route,
        MarketSide.SELL_ORDER,
        destination_side,
    )
    evaluation = ArbitrageCandidateEvaluator().evaluate(
        (eligible,),
        (
            _market("T5_METALBAR", "Bridgewatch", 100, 90),
            _market("T5_METALBAR", "Thetford", 200, 180),
        ),
        (),
        constraints,
        as_of=NOW,
    )
    candidate = evaluation.candidates[0]
    assert candidate.economics.nonfocused_profit_per_craft == expected_profit
    assert [value.role for value in candidate.capacity_requirements] == [
        CapacityRole.ACQUISITION,
        CapacityRole.LIQUIDATION,
    ]
    assert candidate.station_fee_observed_at is None
    ceilings = {
        requirement.key: _ceiling(requirement.key, 2)
        for requirement in candidate.capacity_requirements
    }
    optimized = PlanningOptimizer().optimize((candidate,), ceilings, constraints)
    assert optimized.diagnostics.status is OptimizationStatus.EXACT
    assert optimized.actions[0].quantity == 2
    validation = validate_plan(
        optimized,
        constraints,
        ceilings,
        as_of=NOW,
        freshness_hooks=(
            *default_freshness_hooks(constraints),
            action_evidence_hook(constraints, CURRENT_RULES),
        ),
    )
    assert validation.status is PlanStatus.DECISION_GRADE
    assert validation.total_expected_profit == expected_profit * 2
    action = optimized.actions[0]
    evidence = dict(action.evidence)
    capacity_rows = json.loads(evidence["capacity_ceilings"])
    capacity_rows[0]["maximum_market_units"] += 1
    evidence["capacity_ceilings"] = json.dumps(
        capacity_rows,
        sort_keys=True,
        separators=(",", ":"),
    )
    corrupted = replace(optimized, actions=(replace(action, evidence=tuple(evidence.items())),))
    rejected = validate_plan(
        corrupted,
        constraints,
        ceilings,
        as_of=NOW,
        freshness_hooks=(
            *default_freshness_hooks(constraints),
            action_evidence_hook(constraints, CURRENT_RULES),
        ),
    )
    assert rejected.status is PlanStatus.NON_ACTIONABLE
    assert PlanReasonCode.INVALID_ACTION_EVIDENCE in {reason.code for reason in rejected.reasons}
    snapshot = PlanSnapshot.from_optimization(
        snapshot_id=f"arbitrage-{sale_method.value}",
        created_at=NOW,
        completed_at=NOW,
        constraints=constraints,
        result=optimized,
        catalog_source_version="fixture",
        mechanics_ruleset_id=CURRENT_RULES.ruleset_id,
    )
    payload = snapshot.to_dict()
    assert payload["snapshot_format_version"] == 3
    assert payload["actions"][0]["action_kind"] == "arbitrage"
    assert [value["role"] for value in payload["actions"][0]["capacity_requirements"]] == [
        "acquisition",
        "liquidation",
    ]
    assert PlanSnapshot.from_dict(payload) == snapshot
    export_path = export_plan_csv(snapshot, tmp_path / f"{sale_method.value}.csv")
    with export_path.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["action_kind"] == "arbitrage"
    assert row["source_city"] == "Bridgewatch"
    assert row["destination_city"] == "Thetford"
    assert row["production_city"] == ""
    assert {value["role"] for value in json.loads(row["capacity_requirements_json"])} == {
        "acquisition",
        "liquidation",
    }


def test_sell_order_arbitrage_uses_labeled_history_when_current_sell_is_missing() -> None:
    constraints = _constraints(sale_method=SaleMethod.SELL_ORDER)
    route = generate_arbitrage_routes(constraints).routes[0]
    eligible = EligibleArbitrageRoute(
        Item("T5_METALBAR", "Titanium Steel Bar", 5, crafting_category="ore"),
        route,
    )
    history = tuple(
        MarketHistoryInterval(
            "T5_METALBAR",
            city,
            1,
            Region.AMERICAS,
            NOW - timedelta(days=day),
            20,
            price,
            HistoryTimeScale.DAILY,
            NOW,
        )
        for city, price in (("Bridgewatch", 100), ("Thetford", 200))
        for day in range(1, 8)
    )

    evaluation = ArbitrageCandidateEvaluator().evaluate(
        (eligible,),
        (),
        (),
        constraints,
        history=history,
        as_of=NOW,
    )

    assert evaluation.candidates
    prices = json.loads(dict(evaluation.candidates[0].evidence)["prices"])
    assert {value["source"] for value in prices} == {"HISTORICAL_ESTIMATE"}
    assert all(value["confidence"] == "HIGH" for value in prices)


@pytest.mark.parametrize(
    ("prices", "message"),
    (
        ((_market("T5_METALBAR", "Thetford", 200, 180),), "source price is missing"),
        ((_market("T5_METALBAR", "Bridgewatch", 100, 90),), "destination price is missing"),
        (
            (
                _market("T5_METALBAR", "Bridgewatch", 100, 90),
                _market_at(
                    "T5_METALBAR",
                    "Thetford",
                    200,
                    180,
                    datetime(2026, 8, 19, 12, 5, tzinfo=UTC),
                ),
            ),
            "destination price is materially future-dated",
        ),
        (
            (
                _market("T5_METALBAR", "Bridgewatch", 100, 90),
                _market("T5_METALBAR", "Thetford", 100, 90),
            ),
            "spread is nonpositive",
        ),
    ),
)
def test_invalid_arbitrage_evidence_and_fee_erased_spreads_are_explained(
    prices,
    message,
) -> None:
    constraints = _constraints()
    route = generate_arbitrage_routes(constraints).routes[0]
    eligible = EligibleArbitrageRoute(
        Item("T5_METALBAR", "Titanium Steel Bar", 5, crafting_category="ore"),
        route,
    )
    result = ArbitrageCandidateEvaluator().evaluate(
        (eligible,),
        prices,
        (),
        constraints,
        as_of=NOW,
    )
    assert result.near_misses
    assert message in " ".join(
        reason.message.casefold() for reason in result.near_misses[0].reasons
    )


@pytest.mark.parametrize("stale_city", ("Bridgewatch", "Thetford"))
def test_stale_arbitrage_price_is_advisory_and_route_remains_eligible(stale_city) -> None:
    constraints = _constraints()
    route = generate_arbitrage_routes(constraints).routes[0]
    eligible = EligibleArbitrageRoute(
        Item("T5_METALBAR", "Titanium Steel Bar", 5, crafting_category="ore"),
        route,
    )
    prices = tuple(
        _market_at(
            "T5_METALBAR",
            city,
            100 if city == "Bridgewatch" else 200,
            90 if city == "Bridgewatch" else 180,
            (datetime(2026, 8, 19, 6, tzinfo=UTC) if city == stale_city else NOW),
        )
        for city in ("Bridgewatch", "Thetford")
    )

    result = ArbitrageCandidateEvaluator().evaluate(
        (eligible,),
        prices,
        (),
        constraints,
        as_of=NOW,
    )

    assert result.candidates
    assert not result.near_misses
    assert any(
        reason.code is PlanReasonCode.STALE_MARKET_DATA
        and reason.severity is PlanReasonSeverity.WARNING
        for reason in result.candidates[0].reasons
    )
    candidate = result.candidates[0]
    ceilings = {
        requirement.key: _ceiling(requirement.key, 2)
        for requirement in candidate.capacity_requirements
    }
    optimized = PlanningOptimizer().optimize((candidate,), ceilings, constraints)

    validation = validate_plan(
        optimized,
        constraints,
        ceilings,
        as_of=NOW,
        freshness_hooks=(
            *default_freshness_hooks(constraints),
            action_evidence_hook(constraints, CURRENT_RULES),
        ),
    )

    assert validation.is_feasible
    assert validation.status is PlanStatus.ADVISORY
    assert PlanReasonCode.INVALID_ACTION_EVIDENCE not in {
        reason.code for reason in validation.reasons
    }
    assert any(
        reason.code is PlanReasonCode.STALE_MARKET_DATA
        and reason.severity is PlanReasonSeverity.WARNING
        for reason in validation.reasons
    )


def _candidate(
    candidate_id: str,
    *,
    action_kind: ActionKind,
    route: CandidateRoute,
    profit: int,
    requirements: tuple[CapacityRequirement, ...],
    output_quantity: int = 1,
    item_id: str = "T5_METALBAR",
) -> PlanCandidate:
    return PlanCandidate(
        candidate_id,
        item_id,
        "Titanium Steel Bar",
        route,
        CandidateEconomics(
            10 + route.transport_cost_per_action_unit,
            profit,
            expected_revenue_per_craft=100,
            nonfocused_effective_cost_per_craft=50,
            transport_cash_per_craft=route.transport_cost_per_action_unit,
        ),
        action_kind=action_kind,
        output_quantity_per_craft=output_quantity,
        liquidity=LiquidityLevel.HIGH,
        nonfocused_roi=profit / 50,
        capacity_requirements=requirements,
    )


def test_mixed_action_routes_share_liquidation_capacity_without_double_counting() -> None:
    destination = (Region.AMERICAS, "T5_METALBAR", "Thetford", 1)
    source = (Region.AMERICAS, "T5_METALBAR", "Bridgewatch", 1)
    production_route = CandidateRoute(
        Region.AMERICAS,
        "Thetford",
        "Thetford",
        "Thetford",
        TransportPolicy.LOCAL_ONLY,
    )
    arbitrage_route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Thetford",
        TransportPolicy.EXPLICIT_COST,
        5,
    )
    production = _candidate(
        "production",
        action_kind=ActionKind.REFINE,
        route=production_route,
        profit=150,
        requirements=(CapacityRequirement(destination, CapacityRole.LIQUIDATION, 2),),
        output_quantity=2,
    )
    arbitrage = _candidate(
        "arbitrage",
        action_kind=ActionKind.ARBITRAGE,
        route=arbitrage_route,
        profit=60,
        requirements=(
            CapacityRequirement(source, CapacityRole.ACQUISITION, 1),
            CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
        ),
    )
    constraints = _constraints(
        action_kinds=frozenset({ActionKind.REFINE, ActionKind.ARBITRAGE}),
        per_item_craft_cap=3,
    )
    ceilings = {destination: _ceiling(destination, 3), source: _ceiling(source, 3)}
    result = PlanningOptimizer().optimize((production, arbitrage), ceilings, constraints)
    assert result.diagnostics.method == "capacity_component_pareto_v1"
    assert result.diagnostics.status is OptimizationStatus.EXACT
    assert result.total_expected_profit == 210
    assert {action.candidate_id: action.quantity for action in result.actions} == {
        "production": 1,
        "arbitrage": 1,
    }
    assert (
        sum(
            units
            for action in result.actions
            for key, units in action.capacity_consumption
            if key == destination
        )
        == 3
    )


def test_arbitrage_requires_both_capacities_and_zero_capacity_is_not_unlimited() -> None:
    source = (Region.AMERICAS, "T5_METALBAR", "Bridgewatch", 1)
    destination = (Region.AMERICAS, "T5_METALBAR", "Thetford", 1)
    route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Thetford",
        TransportPolicy.ACKNOWLEDGED_UNCOSTED,
    )
    with pytest.raises(ValueError, match="acquisition and one liquidation"):
        _candidate(
            "invalid-single-capacity-arbitrage",
            action_kind=ActionKind.ARBITRAGE,
            route=route,
            profit=10,
            requirements=(CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),),
        )
    candidate = _candidate(
        "zero-source-capacity",
        action_kind=ActionKind.ARBITRAGE,
        route=route,
        profit=10,
        requirements=(
            CapacityRequirement(source, CapacityRole.ACQUISITION, 1),
            CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
        ),
    )
    ceilings = {
        source: QuantityCeiling(
            source,
            3,
            0,
            QuantityCeilingSource.HISTORICAL_VOLUME_SHARE,
            0,
            0.2,
            "No conservative acquisition capacity.",
        ),
        destination: _ceiling(destination, 3),
    }
    result = PlanningOptimizer().optimize((candidate,), ceilings, _constraints())
    assert result.actions == ()
    assert result.total_expected_profit == 0


def test_multicapacity_quantity_bound_is_feasible_and_never_labeled_exact() -> None:
    source = (Region.AMERICAS, "T5_METALBAR", "Bridgewatch", 1)
    destination = (Region.AMERICAS, "T5_METALBAR", "Thetford", 1)
    candidate = _candidate(
        "bounded-arbitrage",
        action_kind=ActionKind.ARBITRAGE,
        route=CandidateRoute(
            Region.AMERICAS,
            "Bridgewatch",
            "Bridgewatch",
            "Thetford",
            TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        ),
        profit=10,
        requirements=(
            CapacityRequirement(source, CapacityRole.ACQUISITION, 1),
            CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
        ),
    )
    ceilings = {source: _ceiling(source, 1_000), destination: _ceiling(destination, 1_000)}
    constraints = _constraints(
        available_silver=100_000,
        per_item_craft_cap=1_000,
    )
    result = PlanningOptimizer().optimize(
        (candidate,),
        ceilings,
        constraints,
        limits=OptimizerLimits(max_states=10),
    )
    assert result.diagnostics.status is OptimizationStatus.APPROXIMATE
    assert "candidate_quantity_state_limit" in result.diagnostics.approximation_reasons
    assert result.total_pre_revenue_cash <= constraints.silver_budget
    assert all(action.quantity <= 1_000 for action in result.actions)


def test_arbitrage_routes_share_source_and_destination_capacities() -> None:
    item_id = "T5_METALBAR"
    bridgewatch = (Region.AMERICAS, item_id, "Bridgewatch", 1)
    martlock = (Region.AMERICAS, item_id, "Martlock", 1)
    thetford = (Region.AMERICAS, item_id, "Thetford", 1)
    candidates = tuple(
        _candidate(
            f"source-{index}",
            action_kind=ActionKind.ARBITRAGE,
            route=CandidateRoute(
                Region.AMERICAS,
                "Bridgewatch",
                "Bridgewatch",
                destination_city,
                TransportPolicy.ACKNOWLEDGED_UNCOSTED,
            ),
            profit=profit,
            requirements=(
                CapacityRequirement(bridgewatch, CapacityRole.ACQUISITION, 1),
                CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
            ),
        )
        for index, (destination_city, destination, profit) in enumerate(
            (("Martlock", martlock, 10), ("Thetford", thetford, 20))
        )
    )
    constraints = _constraints(
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        transport_cost_per_craft=None,
        per_item_craft_cap=3,
    )
    ceilings = {
        bridgewatch: _ceiling(bridgewatch, 2),
        martlock: _ceiling(martlock, 3),
        thetford: _ceiling(thetford, 3),
    }
    result = PlanningOptimizer().optimize(candidates, ceilings, constraints)
    assert (
        sum(
            units
            for action in result.actions
            for key, units in action.capacity_consumption
            if key == bridgewatch
        )
        == 2
    )
    assert {action.candidate_id: action.quantity for action in result.actions} == {"source-1": 2}

    reverse_candidates = tuple(
        _candidate(
            f"destination-{index}",
            action_kind=ActionKind.ARBITRAGE,
            route=CandidateRoute(
                Region.AMERICAS,
                city,
                city,
                "Martlock",
                TransportPolicy.ACKNOWLEDGED_UNCOSTED,
            ),
            profit=10 + index,
            requirements=(
                CapacityRequirement(key, CapacityRole.ACQUISITION, 1),
                CapacityRequirement(martlock, CapacityRole.LIQUIDATION, 1),
            ),
        )
        for index, (city, key) in enumerate((("Bridgewatch", bridgewatch), ("Thetford", thetford)))
    )
    reverse_ceilings = {
        bridgewatch: _ceiling(bridgewatch, 3),
        thetford: _ceiling(thetford, 3),
        martlock: _ceiling(martlock, 2),
    }
    reverse = PlanningOptimizer().optimize(
        reverse_candidates,
        reverse_ceilings,
        constraints,
    )
    assert sum(action.quantity for action in reverse.actions) == 2


def test_multicapacity_pareto_preserves_deterministic_ties_across_capacity_usage() -> None:
    item_id = "T5_METALBAR"
    source = (Region.AMERICAS, item_id, "Martlock", 1)
    destination = (Region.AMERICAS, item_id, "Thetford", 1)
    arbitrage_route = CandidateRoute(
        Region.AMERICAS,
        "Martlock",
        "Martlock",
        "Thetford",
        TransportPolicy.ACKNOWLEDGED_UNCOSTED,
    )
    production_route = CandidateRoute(
        Region.AMERICAS,
        "Thetford",
        "Thetford",
        "Thetford",
        TransportPolicy.LOCAL_ONLY,
    )

    def candidate(candidate_id, action_kind, route, cash, profit, requirements):
        return PlanCandidate(
            candidate_id,
            item_id,
            item_id,
            route,
            CandidateEconomics(cash, profit),
            action_kind=action_kind,
            liquidity=LiquidityLevel.HIGH,
            capacity_requirements=requirements,
        )

    arbitrage_requirements = (
        CapacityRequirement(source, CapacityRole.ACQUISITION, 1),
        CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
    )
    candidates = (
        candidate(
            "candidate-0",
            ActionKind.ARBITRAGE,
            arbitrage_route,
            3,
            14,
            arbitrage_requirements,
        ),
        candidate(
            "candidate-1",
            ActionKind.ARBITRAGE,
            arbitrage_route,
            5,
            16,
            arbitrage_requirements,
        ),
        candidate(
            "candidate-2",
            ActionKind.CRAFT,
            production_route,
            4,
            15,
            (CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),),
        ),
    )
    constraints = _constraints(
        available_silver=13,
        available_focus=0,
        action_kinds=frozenset(ActionKind),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        transport_cost_per_craft=None,
        per_item_craft_cap=3,
    )
    result = PlanningOptimizer().optimize(
        candidates,
        {source: _ceiling(source, 3), destination: _ceiling(destination, 3)},
        constraints,
    )

    assert result.diagnostics.status is OptimizationStatus.EXACT
    assert result.total_expected_profit == 46
    assert result.total_pre_revenue_cash == 13
    assert tuple(
        (action.candidate_id, action.focused_quantity, action.nonfocused_quantity)
        for action in result.actions
    ) == (("candidate-0", 0, 1), ("candidate-1", 0, 2))


def test_disconnected_capacity_components_share_global_silver_and_focus() -> None:
    candidates: list[PlanCandidate] = []
    ceilings = {}
    for item_id, source_city, destination_city, focus_profit in (
        ("ITEM_A", "Bridgewatch", "Thetford", 30),
        ("ITEM_B", "Lymhurst", "Martlock", 40),
    ):
        source = (Region.AMERICAS, item_id, source_city, 1)
        destination = (Region.AMERICAS, item_id, destination_city, 1)
        ceilings[source] = _ceiling(source, 1)
        ceilings[destination] = _ceiling(destination, 1)
        candidates.append(
            _candidate(
                f"arb-{item_id}",
                action_kind=ActionKind.ARBITRAGE,
                route=CandidateRoute(
                    Region.AMERICAS,
                    source_city,
                    source_city,
                    destination_city,
                    TransportPolicy.ACKNOWLEDGED_UNCOSTED,
                ),
                profit=5,
                requirements=(
                    CapacityRequirement(source, CapacityRole.ACQUISITION, 1),
                    CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
                ),
                item_id=item_id,
            )
        )
        candidates.append(
            PlanCandidate(
                f"production-{item_id}",
                item_id,
                item_id,
                CandidateRoute(
                    Region.AMERICAS,
                    destination_city,
                    destination_city,
                    destination_city,
                    TransportPolicy.LOCAL_ONLY,
                ),
                CandidateEconomics(10, 10, focus_profit, 2),
                action_kind=ActionKind.CRAFT,
                liquidity=LiquidityLevel.HIGH,
                nonfocused_roi=1,
                focused_roi=focus_profit / 10,
                capacity_requirements=(
                    CapacityRequirement(destination, CapacityRole.LIQUIDATION, 1),
                ),
            )
        )
    constraints = _constraints(
        available_silver=20,
        available_focus=2,
        action_kinds=frozenset(ActionKind),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        transport_cost_per_craft=None,
        per_item_craft_cap=1,
    )
    result = PlanningOptimizer().optimize(tuple(candidates), ceilings, constraints)
    assert result.diagnostics.group_count == 2
    assert result.total_expected_profit == 50
    assert result.total_pre_revenue_cash == 20
    assert result.total_focus == 2
    assert {
        action.candidate_id: (action.focused_quantity, action.nonfocused_quantity)
        for action in result.actions
    } == {
        "production-ITEM_A": (0, 1),
        "production-ITEM_B": (1, 0),
    }
