#!/usr/bin/env python3
"""Deterministically verify planning preprocessing against exhaustive search.

This is intentionally separate from the normal unit-test budget.  It generates
small integer planning universes, computes the raw exhaustive optimum, applies
the production route preprocessor, and compares both the exhaustive and
production-optimizer results. The default V0.6 release gate is 5,000 crafting/refining cases;
the independent multi-capacity verifier covers arbitrage portfolios.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass
from itertools import product

from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning import (
    ActionKind,
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationStatus,
    OptimizerLimits,
    PlanCandidate,
    QuantityCeiling,
    QuantityCeilingSource,
    TransportPolicy,
    optimize_plan,
    prune_dominated_candidates,
)

DEFAULT_FIXTURES = 5_000
DEFAULT_SEED = 410_041


@dataclass(frozen=True, slots=True)
class Fixture:
    candidates: tuple[PlanCandidate, ...]
    ceilings: dict
    constraints: FindMoneyConstraints


@dataclass(frozen=True, slots=True)
class Allocation:
    cash: int = 0
    focus: int = 0
    profit: int = 0
    minimum_liquidity: int = 4
    action_count: int = 0
    signature: tuple[tuple[str, int, int], ...] = ()

    @property
    def objective(self) -> tuple:
        return (
            -self.profit,
            self.cash,
            self.focus,
            -self.minimum_liquidity,
            self.action_count,
            self.signature,
        )

    @property
    def economic_objective(self) -> tuple[int, int, int, int, int]:
        return self.objective[:5]


def generate_fixture(rng: random.Random, fixture_number: int) -> Fixture:
    group_count = rng.randint(1, 2)
    candidates: list[PlanCandidate] = []
    ceilings: dict = {}
    material_cities: list[str] = []
    use_focus = rng.random() < 0.85

    for group_number in range(group_count):
        item_id = f"ITEM_{fixture_number}_{group_number}"
        sell_city = f"Sell {group_number}"
        route_count = rng.randint(1, 3)
        cap = rng.randint(1, 3)
        key = (Region.AMERICAS, item_id, sell_city, 1)
        maximum_output_units = rng.choice((None, cap, cap + 1, cap * 2))
        ceilings[key] = QuantityCeiling(
            key,
            cap,
            maximum_output_units,
            QuantityCeilingSource.EXPLICIT_CAP,
            explanation="Deterministic preprocessing verification fixture.",
        )
        prior_economics: CandidateEconomics | None = None
        for route_number in range(route_count):
            material_city = f"Material {group_number}-{route_number}"
            material_cities.append(material_city)
            action_kind = rng.choice((ActionKind.CRAFT, ActionKind.REFINE))
            candidate_id = f"{action_kind.value}|C{fixture_number}-{group_number}-{route_number}"
            if prior_economics is not None and rng.random() < 0.35:
                # Equivalent routes and rounded-away Focus uplift are the
                # regression-rich cases retained from the V0.4.1 hardening pass.
                economics = prior_economics
            else:
                cash = rng.randint(1, 14)
                nonfocus_profit = rng.randint(1, 18)
                has_focus = rng.random() < 0.85
                focused_profit = None
                focus_cost = None
                if has_focus:
                    focused_profit = nonfocus_profit + rng.choice((0, 0, 1, 2, 5))
                    focus_cost = rng.randint(1, 5)
                economics = CandidateEconomics(
                    cash,
                    nonfocus_profit,
                    focused_profit,
                    focus_cost,
                    nonfocused_eligible=rng.random() >= 0.08,
                )
            prior_economics = economics
            candidates.append(
                PlanCandidate(
                    candidate_id,
                    item_id,
                    candidate_id,
                    CandidateRoute(
                        Region.AMERICAS,
                        material_city,
                        f"Craft {group_number}",
                        sell_city,
                        TransportPolicy.ACKNOWLEDGED_UNCOSTED,
                    ),
                    economics,
                    action_kind=action_kind,
                    output_quantity_per_craft=rng.choice((1, 1, 2)),
                    liquidity=rng.choice(tuple(LiquidityLevel)),
                )
            )

    constraints = FindMoneyConstraints(
        available_silver=rng.randint(5, 45),
        available_focus=rng.randint(0, 12),
        material_cities=tuple(material_cities),
        craft_cities=tuple(f"Craft {index}" for index in range(group_count)),
        sell_cities=tuple(f"Sell {index}" for index in range(group_count)),
        use_focus=use_focus,
        history_enabled=False,
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        per_item_craft_cap=3,
    )
    return Fixture(tuple(candidates), ceilings, constraints)


def exhaustive_optimum(fixture: Fixture, candidates: tuple[PlanCandidate, ...]) -> Allocation:
    grouped: dict[tuple, list[PlanCandidate]] = {}
    for candidate in sorted(candidates, key=lambda value: value.canonical_key):
        grouped.setdefault(candidate.execution_capacity_key, []).append(candidate)
    group_options = [
        _exhaustive_group_options(
            tuple(grouped[key]),
            fixture.ceilings[key],
            fixture.constraints,
        )
        for key in sorted(grouped, key=_capacity_order)
    ]
    best = Allocation()
    for selected in product(*group_options):
        combined = _combine_allocations(selected)
        if combined.cash > fixture.constraints.silver_budget:
            continue
        if combined.focus > fixture.constraints.focus_budget:
            continue
        if combined.objective < best.objective:
            best = combined
    return best


def _exhaustive_group_options(
    candidates: tuple[PlanCandidate, ...],
    ceiling: QuantityCeiling,
    constraints: FindMoneyConstraints,
) -> tuple[Allocation, ...]:
    options: list[Allocation] = []

    def visit(
        index: int,
        crafts_used: int,
        output_units_used: int,
        allocation: Allocation,
    ) -> None:
        if index == len(candidates):
            options.append(allocation)
            return
        candidate = candidates[index]
        maximum = min(
            ceiling.maximum_crafts - crafts_used,
            constraints.per_item_craft_cap - crafts_used,
        )
        for total in range(maximum + 1):
            output_units = total * candidate.output_quantity_per_craft
            if (
                ceiling.maximum_output_units is not None
                and output_units_used + output_units > ceiling.maximum_output_units
            ):
                break
            for focused in range(total + 1):
                nonfocused = total - focused
                if focused and (
                    not constraints.use_focus or not candidate.economics.has_focused_variant
                ):
                    continue
                if nonfocused and not candidate.economics.nonfocused_eligible:
                    continue
                cash = total * candidate.economics.pre_revenue_cash_per_craft
                focus = focused * (candidate.economics.focus_per_focused_craft or 0)
                profit = nonfocused * candidate.economics.nonfocused_profit_per_craft + focused * (
                    candidate.economics.focused_profit_per_craft or 0
                )
                if allocation.cash + cash > constraints.silver_budget:
                    continue
                if allocation.focus + focus > constraints.focus_budget:
                    continue
                signature = allocation.signature
                action_count = allocation.action_count
                minimum_liquidity = allocation.minimum_liquidity
                if total:
                    signature = (*signature, (candidate.candidate_id, focused, nonfocused))
                    action_count += 1
                    minimum_liquidity = min(minimum_liquidity, candidate.liquidity_rank)
                visit(
                    index + 1,
                    crafts_used + total,
                    output_units_used + output_units,
                    Allocation(
                        allocation.cash + cash,
                        allocation.focus + focus,
                        allocation.profit + profit,
                        minimum_liquidity,
                        action_count,
                        signature,
                    ),
                )

    visit(0, 0, 0, Allocation())
    return tuple(options)


def _combine_allocations(values: tuple[Allocation, ...]) -> Allocation:
    nonempty = tuple(value for value in values if value.action_count)
    return Allocation(
        sum(value.cash for value in values),
        sum(value.focus for value in values),
        sum(value.profit for value in values),
        min((value.minimum_liquidity for value in nonempty), default=4),
        sum(value.action_count for value in values),
        tuple(item for value in values for item in value.signature),
    )


def optimizer_objective(result) -> tuple[int, int, int, int, int]:
    minimum_liquidity = min((action.liquidity_rank for action in result.actions), default=4)
    return (
        -result.total_expected_profit,
        result.total_pre_revenue_cash,
        result.total_focus,
        -minimum_liquidity,
        len(result.actions),
    )


def verify(fixtures: int, seed: int) -> tuple[int, int, float]:
    rng = random.Random(seed)
    mismatches = 0
    exact_results = 0
    started = time.perf_counter()
    for fixture_number in range(fixtures):
        fixture = generate_fixture(rng, fixture_number)
        raw = exhaustive_optimum(fixture, fixture.candidates)
        pruning = prune_dominated_candidates(fixture.candidates, fixture.constraints)
        pruned = exhaustive_optimum(fixture, pruning.candidates)
        optimized = optimize_plan(
            pruning.candidates,
            fixture.ceilings,
            fixture.constraints,
            limits=OptimizerLimits(max_states=50_000),
        )
        exact_results += optimized.diagnostics.status is OptimizationStatus.EXACT
        actual = (pruned.economic_objective, optimizer_objective(optimized))
        expected = raw.economic_objective
        if actual != (expected, expected):
            mismatches += 1
            print(
                f"mismatch fixture={fixture_number} raw={expected} "
                f"pruned={actual[0]} optimizer={actual[1]}",
                file=sys.stderr,
            )
            if mismatches >= 10:
                break
    return mismatches, exact_results, time.perf_counter() - started


def _capacity_order(key: tuple) -> tuple[str, str, str, int]:
    return (key[0].value, key[1], key[2].casefold(), key[3])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=int, default=DEFAULT_FIXTURES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    if args.fixtures < 1:
        parser.error("--fixtures must be positive")
    mismatches, exact_results, elapsed = verify(args.fixtures, args.seed)
    print(
        f"fixtures={args.fixtures} seed={args.seed} mismatches={mismatches} "
        f"exact_results={exact_results} elapsed_seconds={elapsed:.3f}"
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
