import itertools
import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from albion_crafter.core.crafting_profile import (
    CraftingSkillProfile,
    crafting_skill_mapping_for_recipe,
)
from albion_crafter.core.freshness import Freshness
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.liquidity import LiquidityAssessment, LiquidityLevel
from albion_crafter.market.models import MarketPrice, Region
from albion_crafter.planning.candidates import (
    CandidateMode,
    CandidatePruningCancelled,
    PlanCandidateEvaluator,
    candidate_mode_frontier,
    prune_dominated_candidates,
    shortlist_candidates,
)
from albion_crafter.planning.models import (
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationStatus,
    PlanCandidate,
    PlanReasonCode,
    TransportPolicy,
    quantize_profit_down,
)
from albion_crafter.planning.optimizer import OptimizerLimits, optimize_plan
from albion_crafter.planning.preflight import EligibleRecipeRoute
from albion_crafter.planning.quantity import QuantityCeiling, QuantityCeilingSource

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _recipe(item_id: str = "T4_MAIN_SWORD") -> Recipe:
    return Recipe(
        Item(item_id, "Broadsword", 4, crafting_category="sword"),
        1,
        (MaterialRequirement("T4_METALBAR", 16, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="candidate-fixture",
    )


def _price(
    item_id: str,
    city: str,
    sell: int,
    *,
    buy: int | None = None,
    observed_at: datetime = NOW,
) -> MarketPrice:
    return MarketPrice(
        item_id,
        city,
        1,
        Region.AMERICAS,
        sell,
        observed_at,
        buy if buy is not None else sell - 1,
        observed_at,
        NOW,
        Provenance.AODP_CACHED,
    )


def _eligible(
    *,
    item_id: str = "T4_MAIN_SWORD",
    material_city: str = "Bridgewatch",
    craft_city: str = "Bridgewatch",
    sell_city: str = "Bridgewatch",
    transport_policy: TransportPolicy = TransportPolicy.LOCAL_ONLY,
    transport_cost: int = 0,
) -> tuple[EligibleRecipeRoute, CraftingSkillProfile]:
    recipe = _recipe(item_id)
    profile = CraftingSkillProfile(available_focus=10_000, assume_zero_for_unspecified=True)
    route = CandidateRoute(
        Region.AMERICAS,
        material_city,
        craft_city,
        sell_city,
        transport_policy,
        transport_cost,
    )
    return (
        EligibleRecipeRoute(
            recipe,
            route,
            StationFeeObservation(
                Region.AMERICAS.value,
                craft_city,
                StationType.WARRIORS_FORGE,
                500,
                NOW,
            ),
            Freshness.FRESH,
            profile.resolve(crafting_skill_mapping_for_recipe(recipe)),
            True,
        ),
        profile,
    )


def _constraints(**changes) -> FindMoneyConstraints:
    return replace(
        FindMoneyConstraints(
            available_silver=1_000_000,
            available_focus=10_000,
            material_cities=("Bridgewatch",),
            craft_cities=("Bridgewatch",),
            sell_cities=("Bridgewatch",),
            history_enabled=False,
        ),
        **changes,
    )


def test_candidate_economics_keeps_focus_modes_and_conservative_cash() -> None:
    eligible, profile = _eligible()
    result = PlanCandidateEvaluator().evaluate(
        (eligible,),
        (
            _price("T4_METALBAR", "Bridgewatch", 100),
            _price("T4_MAIN_SWORD", "Bridgewatch", 2_500),
        ),
        (),
        profile,
        _constraints(),
        as_of=NOW,
    )

    assert not result.cancelled
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.economics.nonfocused_eligible
    assert candidate.economics.has_focused_variant
    assert candidate.economics.focus_per_focused_craft is not None
    assert candidate.economics.pre_revenue_cash_per_craft == sum(
        (
            candidate.economics.gross_material_cash_per_craft or 0,
            candidate.economics.station_cash_per_craft or 0,
            candidate.economics.setup_cash_per_craft or 0,
            candidate.economics.transport_cash_per_craft or 0,
        )
    )
    assert dict(candidate.evidence).keys() >= {
        "recipe",
        "prices",
        "station_fee",
        "focus",
        "mechanics",
        "accounting",
        "transport",
    }
    accounting = json.loads(dict(candidate.evidence)["accounting"])
    assert accounting["nonfocused_per_craft"]["returned_material_cost_basis_value"] > 0
    assert (
        accounting["focused_per_craft"]["return_rate"]
        > accounting["nonfocused_per_craft"]["return_rate"]
    )
    mechanics = json.loads(dict(candidate.evidence)["mechanics"])
    assert mechanics["nonfocused_city_bonus"]["classification"].startswith("verified_")


def test_explicit_transport_is_cash_and_economic_cost_once() -> None:
    eligible, profile = _eligible(
        sell_city="Thetford",
        transport_policy=TransportPolicy.EXPLICIT_COST,
        transport_cost=250,
    )
    prices = (
        _price("T4_METALBAR", "Bridgewatch", 100),
        _price("T4_MAIN_SWORD", "Thetford", 2_500),
    )
    with_transport = (
        PlanCandidateEvaluator()
        .evaluate((eligible,), prices, (), profile, _constraints(), as_of=NOW)
        .candidates[0]
    )
    local, local_profile = _eligible()
    without_transport = (
        PlanCandidateEvaluator()
        .evaluate(
            (local,),
            (
                _price("T4_METALBAR", "Bridgewatch", 100),
                _price("T4_MAIN_SWORD", "Bridgewatch", 2_500),
            ),
            (),
            local_profile,
            _constraints(),
            as_of=NOW,
        )
        .candidates[0]
    )

    assert (
        with_transport.economics.pre_revenue_cash_per_craft
        - without_transport.economics.pre_revenue_cash_per_craft
        == 250
    )
    assert (
        without_transport.economics.nonfocused_profit_per_craft
        - with_transport.economics.nonfocused_profit_per_craft
        == 250
    )


def test_stale_price_is_latest_available_and_does_not_reject_candidate() -> None:
    eligible, profile = _eligible()
    result = PlanCandidateEvaluator().evaluate(
        (eligible,),
        (
            _price("T4_METALBAR", "Bridgewatch", 100),
            _price(
                "T4_MAIN_SWORD",
                "Bridgewatch",
                2_500,
                observed_at=NOW - timedelta(hours=5),
            ),
        ),
        (),
        profile,
        _constraints(),
        as_of=NOW,
    )

    assert result.candidates[0].economics.nonfocused_eligible
    assert result.candidates[0].economics.has_focused_variant
    assert not result.near_misses
    assert dict(result.rejection_counts).get("stale_market_data", 0) == 0


@pytest.mark.parametrize(
    ("weighted_mean", "deviation", "output_price"),
    (
        (2_500.0, 9.0, 25_000),
        (None, None, 50_000),
    ),
)
def test_extreme_optimistic_output_quote_requires_in_game_verification(
    weighted_mean: float | None,
    deviation: float | None,
    output_price: int,
) -> None:
    eligible, profile = _eligible()
    key = (Region.AMERICAS, "T4_MAIN_SWORD", "Bridgewatch", 1)
    liquidity = LiquidityAssessment(
        LiquidityLevel.LOW if weighted_mean is not None else LiquidityLevel.UNKNOWN,
        100 if weighted_mean is not None else 0,
        4 if weighted_mean is not None else 0,
        weighted_mean,
        deviation,
        float(output_price),
        NOW if weighted_mean is not None else None,
        weighted_mean,
        weighted_mean,
        (),
    )
    result = PlanCandidateEvaluator().evaluate(
        (eligible,),
        (
            _price("T4_METALBAR", "Bridgewatch", 100),
            _price("T4_MAIN_SWORD", "Bridgewatch", output_price),
        ),
        (),
        profile,
        _constraints(history_enabled=True),
        liquidity_by_key={key: liquidity},
        as_of=NOW,
    )

    candidate = result.candidates[0]
    assert candidate.has_blocker
    assert not candidate.economics.nonfocused_eligible
    assert not candidate.economics.has_focused_variant
    assert PlanReasonCode.EXTREME_MARKET_OUTLIER in {reason.code for reason in candidate.reasons}
    assert result.near_misses[0].item_id == "T4_MAIN_SWORD"
    assert dict(result.rejection_counts)["extreme_market_outlier"] == 1


def test_missing_current_sell_uses_cached_daily_history_in_planning() -> None:
    eligible, profile = _eligible()
    history = tuple(
        MarketHistoryInterval(
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
            Region.AMERICAS,
            NOW - timedelta(days=day),
            20,
            2_500,
            HistoryTimeScale.DAILY,
            NOW,
        )
        for day in range(1, 8)
    )

    result = PlanCandidateEvaluator().evaluate(
        (eligible,),
        (_price("T4_METALBAR", "Bridgewatch", 100),),
        (),
        profile,
        _constraints(),
        history=history,
        as_of=NOW,
    )

    assert result.candidates
    prices = json.loads(dict(result.candidates[0].evidence)["prices"])
    output = next(value for value in prices if value["role"] == "output")
    assert output["price"] == 2_500
    assert output["source"] == "HISTORICAL_ESTIMATE"


def test_shortlist_selects_groups_but_keeps_competing_routes() -> None:
    first, profile = _eligible()
    competing, _ = _eligible(
        material_city="Martlock",
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
    )
    other, _ = _eligible(item_id="T4_MAIN_AXE")
    prices = (
        _price("T4_METALBAR", "Bridgewatch", 100),
        _price("T4_METALBAR", "Martlock", 90),
        _price("T4_MAIN_SWORD", "Bridgewatch", 2_500),
        _price("T4_MAIN_AXE", "Bridgewatch", 2_000),
    )
    evaluated = PlanCandidateEvaluator().evaluate(
        (first, competing, other),
        prices,
        (),
        profile,
        _constraints(),
        as_of=NOW,
    )
    shortlist = shortlist_candidates(evaluated.candidates, maximum_capacity_groups=1)

    assert shortlist.capacity_groups_considered == 2
    assert shortlist.capacity_groups_selected == 1
    assert len(shortlist.candidates) == 2
    assert {value.item_id for value in shortlist.candidates} == {"T4_MAIN_SWORD"}


def _ranking_candidate(
    item_id: str,
    *,
    cash: int,
    nonfocus_profit: int,
    focus_profit: int,
    focus_cost: int = 10,
    nonfocus_roi: float = 0.1,
    focus_roi: float = 0.1,
    material_city: str = "Bridgewatch",
) -> PlanCandidate:
    route = CandidateRoute(
        Region.AMERICAS,
        material_city,
        "Bridgewatch",
        "Bridgewatch",
        (
            TransportPolicy.LOCAL_ONLY
            if material_city == "Bridgewatch"
            else TransportPolicy.EXPLICIT_COST
        ),
        0,
    )
    return PlanCandidate(
        item_id,
        item_id,
        item_id,
        route,
        CandidateEconomics(cash, nonfocus_profit, focus_profit, focus_cost),
        nonfocused_roi=nonfocus_roi,
        focused_roi=focus_roi,
    )


def test_shortlist_ranks_only_modes_legal_under_focus_reserve_and_filters() -> None:
    focus_only_star = _ranking_candidate(
        "A",
        cash=100,
        nonfocus_profit=1,
        focus_profit=1_000,
        focus_cost=10,
        focus_roi=0.01,
    )
    nonfocus_winner = _ranking_candidate(
        "B",
        cash=100,
        nonfocus_profit=100,
        focus_profit=101,
        focus_cost=10,
        focus_roi=0.50,
    )
    no_focus = _constraints(use_focus=False)
    reserved = _constraints(available_focus=10, focus_reserve=10)
    roi_filtered = _constraints(minimum_roi=0.10)

    for constraints in (no_focus, reserved, roi_filtered):
        shortlist = shortlist_candidates(
            (focus_only_star, nonfocus_winner),
            maximum_capacity_groups=1,
            constraints=constraints,
        )
        assert [candidate.item_id for candidate in shortlist.candidates] == ["B"]


def test_route_dominance_pruning_preserves_real_cash_focus_tradeoffs() -> None:
    dominant = _ranking_candidate(
        "ITEM",
        cash=90,
        nonfocus_profit=110,
        focus_profit=160,
        focus_cost=8,
    )
    dominated = _ranking_candidate(
        "ITEM",
        cash=100,
        nonfocus_profit=100,
        focus_profit=150,
        focus_cost=10,
        material_city="Martlock",
    )
    focus_tradeoff = _ranking_candidate(
        "ITEM",
        cash=80,
        nonfocus_profit=90,
        focus_profit=170,
        focus_cost=20,
        material_city="Thetford",
    )
    result = prune_dominated_candidates(
        (dominated, focus_tradeoff, dominant),
        _constraints(),
    )

    assert result.dominated_count == 1
    assert {candidate.route.material_city for candidate in result.candidates} == {
        "Bridgewatch",
        "Thetford",
    }


def _pruning_candidate(
    candidate_id: str,
    *,
    item_id: str = "SHARED_ITEM",
    material_city: str,
    cash: int,
    nonfocus_profit: int,
    focus_profit: int,
    focus_cost: int,
    liquidity: LiquidityLevel = LiquidityLevel.MODERATE,
    output_quantity: int = 1,
) -> PlanCandidate:
    return PlanCandidate(
        candidate_id,
        item_id,
        candidate_id,
        CandidateRoute(
            Region.AMERICAS,
            material_city,
            "Bridgewatch",
            "Bridgewatch",
            TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        ),
        CandidateEconomics(cash, nonfocus_profit, focus_profit, focus_cost),
        output_quantity_per_craft=output_quantity,
        liquidity=liquidity,
    )


def _explicit_ceilings(
    candidates: tuple[PlanCandidate, ...],
    crafts: int,
    *,
    output_units: int | None = None,
) -> dict:
    return {
        candidate.execution_capacity_key: QuantityCeiling(
            candidate.execution_capacity_key,
            crafts,
            output_units,
            QuantityCeilingSource.EXPLICIT_CAP,
            explanation="Pruning regression fixture shared execution capacity.",
        )
        for candidate in candidates
    }


def _brute_force_profit(
    candidates: tuple[PlanCandidate, ...],
    ceilings: dict,
    constraints: FindMoneyConstraints,
) -> int:
    """Independent tiny-fixture reference; it does not call preprocessing."""

    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.canonical_key))
    choices: list[tuple[tuple[int, int], ...]] = []
    for candidate in ordered:
        ceiling = ceilings[candidate.execution_capacity_key]
        values = [(0, 0)]
        maximum = min(ceiling.maximum_crafts, constraints.per_item_craft_cap)
        for total in range(1, maximum + 1):
            for focused in range(total + 1):
                nonfocused = total - focused
                if focused and (
                    not constraints.use_focus or not candidate.economics.has_focused_variant
                ):
                    continue
                if nonfocused and not candidate.economics.nonfocused_eligible:
                    continue
                values.append((focused, nonfocused))
        choices.append(tuple(values))

    best = 0
    for allocations in itertools.product(*choices):
        cash = 0
        focus = 0
        profit = 0
        crafts_by_key: dict = {}
        output_by_key: dict = {}
        for candidate, (focused, nonfocused) in zip(ordered, allocations, strict=True):
            total = focused + nonfocused
            cash += total * candidate.economics.pre_revenue_cash_per_craft
            focus += focused * (candidate.economics.focus_per_focused_craft or 0)
            profit += nonfocused * candidate.economics.nonfocused_profit_per_craft
            profit += focused * (candidate.economics.focused_profit_per_craft or 0)
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
        best = max(best, profit)
    return best


def test_mutual_dominance_regression_retains_one_route_and_profit() -> None:
    routes = (
        _pruning_candidate(
            "route-a",
            material_city="Martlock",
            cash=5,
            nonfocus_profit=9,
            focus_profit=9,
            focus_cost=1,
        ),
        _pruning_candidate(
            "route-b",
            material_city="Thetford",
            cash=5,
            nonfocus_profit=9,
            focus_profit=9,
            focus_cost=2,
        ),
    )
    constraints = _constraints(
        available_silver=5,
        available_focus=2,
        per_item_craft_cap=1,
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        material_cities=("Martlock", "Thetford"),
    )
    ceilings = _explicit_ceilings(routes, 1)

    pruned = prune_dominated_candidates(routes, constraints)

    assert _brute_force_profit(routes, ceilings, constraints) == 9
    assert _brute_force_profit(pruned.candidates, ceilings, constraints) == 9
    assert len(pruned.candidates) == 1
    assert pruned.candidates[0].candidate_id == "route-a"
    assert pruned.dominated_count == 1
    assert pruned.routes_before == 2
    assert pruned.routes_after == 1
    assert pruned.local_modes_removed == 2
    assert pruned.equivalent_routes_collapsed == 1
    optimized = optimize_plan(
        pruned.candidates,
        ceilings,
        constraints,
        limits=OptimizerLimits(max_states=100),
    )
    assert optimized.diagnostics.status is OptimizationStatus.EXACT
    assert optimized.total_expected_profit == 9


def test_candidate_local_frontier_removes_rounded_away_focus_uplift_only() -> None:
    tied = _pruning_candidate(
        "rounded-tie",
        material_city="Martlock",
        cash=5,
        nonfocus_profit=quantize_profit_down(Decimal("9.01")),
        focus_profit=quantize_profit_down(Decimal("9.99")),
        focus_cost=2,
    )
    tradeoff = _pruning_candidate(
        "real-uplift",
        material_city="Thetford",
        cash=5,
        nonfocus_profit=9,
        focus_profit=10,
        focus_cost=2,
    )

    assert candidate_mode_frontier(tied, _constraints()) == (CandidateMode(5, 0, 9),)
    assert candidate_mode_frontier(tradeoff, _constraints()) == (
        CandidateMode(5, 0, 9),
        CandidateMode(5, 2, 10),
    )


def test_equivalent_route_representative_is_deterministic_and_prefers_evidence() -> None:
    canonical = _pruning_candidate(
        "a-canonical-low-liquidity",
        material_city="Martlock",
        cash=5,
        nonfocus_profit=9,
        focus_profit=9,
        focus_cost=1,
        liquidity=LiquidityLevel.LOW,
    )
    better_evidence = _pruning_candidate(
        "z-better-liquidity",
        material_city="Thetford",
        cash=5,
        nonfocus_profit=9,
        focus_profit=9,
        focus_cost=3,
        liquidity=LiquidityLevel.HIGH,
    )
    constraints = _constraints(
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        material_cities=("Martlock", "Thetford"),
    )

    first = prune_dominated_candidates((canonical, better_evidence), constraints)
    second = prune_dominated_candidates((better_evidence, canonical), constraints)

    assert [value.candidate_id for value in first.candidates] == ["z-better-liquidity"]
    assert second.candidates == first.candidates
    assert first.equivalent_routes_collapsed == 1


def test_pruning_preserves_shared_craft_and_output_capacity() -> None:
    routes = (
        _pruning_candidate(
            "best",
            material_city="Bridgewatch",
            cash=4,
            nonfocus_profit=8,
            focus_profit=12,
            focus_cost=1,
            output_quantity=2,
        ),
        _pruning_candidate(
            "dominated",
            material_city="Martlock",
            cash=5,
            nonfocus_profit=7,
            focus_profit=11,
            focus_cost=2,
            output_quantity=2,
        ),
    )
    constraints = _constraints(
        available_silver=20,
        available_focus=2,
        per_item_craft_cap=3,
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        material_cities=("Bridgewatch", "Martlock"),
    )
    ceilings = _explicit_ceilings(routes, 3, output_units=4)
    pruned = prune_dominated_candidates(routes, constraints)
    result = optimize_plan(pruned.candidates, ceilings, constraints)

    assert result.diagnostics.status is OptimizationStatus.EXACT
    assert result.total_expected_profit == _brute_force_profit(routes, ceilings, constraints)
    assert sum(action.quantity for action in result.actions) <= 3
    assert sum(action.output_units for action in result.actions) <= 4


def test_pruning_cancellation_is_observed_during_normalization_and_comparison() -> None:
    routes = tuple(
        _pruning_candidate(
            f"route-{index:02}",
            material_city=f"City-{index:02}",
            cash=5,
            nonfocus_profit=9,
            focus_profit=10,
            focus_cost=1 + index % 3,
        )
        for index in range(36)
    )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 180

    with pytest.raises(CandidatePruningCancelled):
        prune_dominated_candidates(routes, _constraints(), cancelled=cancelled)
    assert checks > 180


def test_seeded_preprocessing_preserves_brute_force_optimum() -> None:
    """Exercise ties, route alternatives, and multiple shared-capacity groups."""

    randomizer = random.Random(20260401)
    for fixture in range(250):
        route_count = randomizer.randint(2, 5)
        routes = tuple(
            _pruning_candidate(
                f"fixture-{fixture:03}-route-{index}",
                item_id=f"ITEM_{randomizer.randint(0, 1)}",
                material_city=f"City-{index}",
                cash=randomizer.randint(1, 6),
                nonfocus_profit=(nonfocus_profit := randomizer.randint(1, 8)),
                focus_profit=nonfocus_profit + randomizer.choice((0, 0, 0, 1, 2)),
                focus_cost=randomizer.randint(1, 3),
                output_quantity=randomizer.randint(1, 2),
            )
            for index in range(route_count)
        )
        craft_cap = randomizer.randint(1, 2)
        constraints = _constraints(
            available_silver=randomizer.randint(2, 15),
            available_focus=randomizer.randint(0, 6),
            per_item_craft_cap=craft_cap,
            transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
            material_cities=tuple(f"City-{index}" for index in range(route_count)),
        )
        ceilings = _explicit_ceilings(routes, craft_cap)
        pruned = prune_dominated_candidates(routes, constraints)
        raw_profit = _brute_force_profit(routes, ceilings, constraints)
        pruned_profit = _brute_force_profit(pruned.candidates, ceilings, constraints)

        assert pruned_profit == raw_profit, f"preprocessing mismatch in fixture {fixture}"
        optimized = optimize_plan(
            pruned.candidates,
            ceilings,
            constraints,
            limits=OptimizerLimits(max_states=10_000),
        )
        assert optimized.diagnostics.status is OptimizationStatus.EXACT
        assert optimized.total_expected_profit == raw_profit
