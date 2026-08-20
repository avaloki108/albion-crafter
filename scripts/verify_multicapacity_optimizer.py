#!/usr/bin/env python3
"""Verify the V0.6 multi-capacity optimizer against an independent brute force oracle."""

from __future__ import annotations

import argparse
import random
import time
from itertools import product

from albion_crafter.core.models import ActionKind
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning.models import (
    CandidateEconomics,
    CandidateRoute,
    CapacityRequirement,
    CapacityRole,
    FindMoneyConstraints,
    PlanCandidate,
    TransportPolicy,
)
from albion_crafter.planning.optimizer import PlanningOptimizer
from albion_crafter.planning.quantity import QuantityCeiling, QuantityCeilingSource

DEFAULT_FIXTURES = 5_000
DEFAULT_SEED = 606_041


def _fixture(rng: random.Random, number: int):
    item_id = f"ITEM_{number}"
    cities = ("Bridgewatch", "Martlock", "Thetford")
    capacity_keys = {city: (Region.AMERICAS, item_id, city, 1) for city in cities}
    ceilings = {
        key: QuantityCeiling(
            key,
            3,
            rng.randint(1, 3),
            QuantityCeilingSource.HISTORICAL_VOLUME_SHARE,
            reported_24h_volume=20,
            historical_volume_share=0.2,
            explanation="Independent V0.6 verifier fixture.",
        )
        for key in capacity_keys.values()
    }
    candidates: list[PlanCandidate] = []
    scenarios = (
        # Production-only compatibility paths.
        ((ActionKind.CRAFT, "Thetford", "Thetford"),) * 2,
        ((ActionKind.REFINE, "Martlock", "Martlock"),) * 2,
        # Shared source, shared destination, and cross-action liquidation.
        (
            (ActionKind.ARBITRAGE, "Bridgewatch", "Martlock"),
            (ActionKind.ARBITRAGE, "Bridgewatch", "Thetford"),
        ),
        (
            (ActionKind.CRAFT, "Martlock", "Martlock"),
            (ActionKind.ARBITRAGE, "Bridgewatch", "Martlock"),
        ),
        (
            (ActionKind.REFINE, "Martlock", "Martlock"),
            (ActionKind.ARBITRAGE, "Bridgewatch", "Martlock"),
        ),
        # All three actions plus a chain through Martlock.
        (
            (ActionKind.CRAFT, "Martlock", "Martlock"),
            (ActionKind.REFINE, "Thetford", "Thetford"),
            (ActionKind.ARBITRAGE, "Bridgewatch", "Martlock"),
            (ActionKind.ARBITRAGE, "Martlock", "Thetford"),
        ),
        (
            (ActionKind.ARBITRAGE, "Bridgewatch", "Martlock"),
            (ActionKind.ARBITRAGE, "Thetford", "Martlock"),
        ),
        (
            (ActionKind.ARBITRAGE, "Bridgewatch", "Martlock"),
            (ActionKind.ARBITRAGE, "Martlock", "Thetford"),
        ),
    )
    for index, (action_kind, source_city, destination_city) in enumerate(
        scenarios[number % len(scenarios)]
    ):
        is_production = action_kind is not ActionKind.ARBITRAGE
        if is_production:
            units = rng.randint(1, 2)
            route = CandidateRoute(
                Region.AMERICAS,
                destination_city,
                destination_city,
                destination_city,
                TransportPolicy.LOCAL_ONLY,
            )
            requirements = (
                CapacityRequirement(
                    capacity_keys[destination_city],
                    CapacityRole.LIQUIDATION,
                    units,
                ),
            )
            output_quantity = units
        else:
            route = CandidateRoute(
                Region.AMERICAS,
                source_city,
                source_city,
                destination_city,
                TransportPolicy.ACKNOWLEDGED_UNCOSTED,
            )
            requirements = (
                CapacityRequirement(
                    capacity_keys[source_city],
                    CapacityRole.ACQUISITION,
                    1,
                ),
                CapacityRequirement(
                    capacity_keys[destination_city],
                    CapacityRole.LIQUIDATION,
                    1,
                ),
            )
            output_quantity = 1
        cash = rng.randint(1, 10)
        profit = rng.randint(1, 20)
        has_focus = action_kind is ActionKind.CRAFT or (
            action_kind is ActionKind.REFINE and rng.random() < 0.7
        )
        focus_cost = rng.randint(1, 4) if has_focus else None
        focused_profit = profit + rng.randint(0, 8) if has_focus else None
        candidates.append(
            PlanCandidate(
                f"{number:05d}-{index}",
                item_id,
                item_id,
                route,
                CandidateEconomics(
                    cash,
                    profit,
                    focused_profit,
                    focus_cost,
                    expected_revenue_per_craft=cash + profit,
                    nonfocused_effective_cost_per_craft=cash,
                ),
                action_kind=action_kind,
                output_quantity_per_craft=output_quantity,
                liquidity=LiquidityLevel.HIGH,
                nonfocused_roi=profit / cash,
                capacity_requirements=requirements,
            )
        )
    constraints = FindMoneyConstraints(
        available_silver=rng.randint(5, 30),
        available_focus=rng.randint(0, 8),
        action_kinds=frozenset(ActionKind),
        material_cities=cities,
        craft_cities=cities,
        sell_cities=cities,
        use_focus=True,
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        per_item_craft_cap=3,
        history_enabled=True,
        historical_volume_share=0.2,
        arbitrage_source_cities=cities,
        arbitrage_destination_cities=cities,
    )
    return tuple(sorted(candidates, key=lambda value: value.canonical_key)), ceilings, constraints


def _oracle(candidates, ceilings, constraints):
    best = None
    candidate_choices = []
    for candidate in candidates:
        choices = [(0, 0)]
        for quantity in range(1, constraints.per_item_craft_cap + 1):
            choices.append((0, quantity))
            if candidate.economics.has_focused_variant:
                choices.extend((focused, quantity - focused) for focused in range(1, quantity + 1))
        candidate_choices.append(tuple(choices))
    for modes in product(*candidate_choices):
        cash = sum(
            (focused + nonfocused) * candidate.economics.pre_revenue_cash_per_craft
            for candidate, (focused, nonfocused) in zip(candidates, modes, strict=True)
        )
        if cash > constraints.silver_budget:
            continue
        focus = sum(
            focused * (candidate.economics.focus_per_focused_craft or 0)
            for candidate, (focused, _) in zip(candidates, modes, strict=True)
        )
        if focus > constraints.focus_budget:
            continue
        usage = {key: 0 for key in ceilings}
        for candidate, (focused, nonfocused) in zip(candidates, modes, strict=True):
            quantity = focused + nonfocused
            for requirement in candidate.capacity_requirements:
                usage[requirement.key] += quantity * requirement.units_per_action_unit
        if any(
            used
            > (
                ceilings[key].maximum_output_units
                if ceilings[key].maximum_output_units is not None
                else ceilings[key].maximum_crafts
            )
            for key, used in usage.items()
        ):
            continue
        profit = sum(
            nonfocused * candidate.economics.nonfocused_profit_per_craft
            + focused * (candidate.economics.focused_profit_per_craft or 0)
            for candidate, (focused, nonfocused) in zip(candidates, modes, strict=True)
        )
        signature = tuple(
            (candidate.candidate_id, focused, nonfocused)
            for candidate, (focused, nonfocused) in zip(candidates, modes, strict=True)
            if focused + nonfocused
        )
        minimum_liquidity = 3 if signature else 4
        objective = (
            -profit,
            cash,
            focus,
            -minimum_liquidity,
            len(signature),
            signature,
        )
        if best is None or objective < best:
            best = objective
    assert best is not None
    return best


def _optimized_objective(result):
    signature = tuple(
        (action.candidate_id, action.focused_quantity, action.nonfocused_quantity)
        for action in result.actions
    )
    minimum_liquidity = min(
        (action.liquidity_rank for action in result.actions),
        default=4,
    )
    return (
        -result.total_expected_profit,
        result.total_pre_revenue_cash,
        result.total_focus,
        -minimum_liquidity,
        len(result.actions),
        signature,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=int, default=DEFAULT_FIXTURES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    arguments = parser.parse_args()
    if arguments.fixtures < 1:
        parser.error("--fixtures must be positive")
    rng = random.Random(arguments.seed)
    started = time.perf_counter()
    optimizer = PlanningOptimizer()
    for number in range(arguments.fixtures):
        candidates, ceilings, constraints = _fixture(rng, number)
        expected = _oracle(candidates, ceilings, constraints)
        result = optimizer.optimize(candidates, ceilings, constraints)
        actual = _optimized_objective(result)
        if actual != expected:
            raise AssertionError(
                f"fixture {number} mismatch: expected {expected!r}, actual {actual!r}"
            )
        if result.diagnostics.status.value != "exact":
            raise AssertionError(f"fixture {number} unexpectedly became Approximate")
    elapsed = time.perf_counter() - started
    print(
        f"Verified {arguments.fixtures:,} multi-capacity fixtures with seed "
        f"{arguments.seed:,}: 0 mismatches, all Exact, {elapsed:.3f}s."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
