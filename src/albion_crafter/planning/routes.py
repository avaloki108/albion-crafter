from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import product

from .models import (
    CandidateRoute,
    FindMoneyConstraints,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    TransportPolicy,
)


@dataclass(frozen=True, slots=True)
class RouteGenerationResult:
    routes: tuple[CandidateRoute, ...]
    combinations_considered: int
    combinations_pruned: int
    rejection_counts: tuple[tuple[PlanReasonCode, int], ...] = ()


class RouteGenerationCancelled(RuntimeError):
    """Raised when route Cartesian-product generation is cancelled."""


def generate_candidate_routes(
    constraints: FindMoneyConstraints,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> RouteGenerationResult:
    """Generate deterministic material -> production -> sell routes under transport policy."""

    material_cities = _canonical_cities(constraints.material_cities)
    craft_cities = _canonical_cities(constraints.craft_cities)
    sell_cities = _canonical_cities(constraints.sell_cities)
    considered = len(material_cities) * len(craft_cities) * len(sell_cities)
    routes: list[CandidateRoute] = []

    for index, (material_city, craft_city, sell_city) in enumerate(
        product(material_cities, craft_cities, sell_cities)
    ):
        if index % 64 == 0 and cancelled is not None and cancelled():
            raise RouteGenerationCancelled("planning route generation was cancelled")
        cross_city = (
            len({material_city.casefold(), craft_city.casefold(), sell_city.casefold()}) > 1
        )
        if constraints.transport_policy is TransportPolicy.LOCAL_ONLY and cross_city:
            continue

        reasons: tuple[PlanReason, ...] = ()
        transport_cost = 0
        if cross_city and constraints.transport_policy is TransportPolicy.ACKNOWLEDGED_UNCOSTED:
            reasons = (
                PlanReason(
                    PlanReasonCode.UNMODELED_TRANSPORT,
                    f"Transport on {material_city} -> {craft_city} -> {sell_city} has no "
                    "modeled silver cost; travel time, capacity, and risk are also unmodeled.",
                    PlanReasonSeverity.WARNING,
                ),
            )
        elif cross_city and constraints.transport_policy is TransportPolicy.EXPLICIT_COST:
            assert constraints.transport_cost_per_craft is not None
            transport_cost = constraints.transport_cost_per_craft
            reasons = (
                PlanReason(
                    PlanReasonCode.UNMODELED_TRANSPORT,
                    f"A user-supplied transport cost of {transport_cost:,} silver per batch is "
                    "applied; travel time, capacity, and risk remain unmodeled.",
                    PlanReasonSeverity.INFO,
                ),
            )

        routes.append(
            CandidateRoute(
                region=constraints.region,
                material_city=material_city,
                craft_city=craft_city,
                sell_city=sell_city,
                transport_policy=constraints.transport_policy,
                transport_cost_per_craft=transport_cost,
                reasons=reasons,
            )
        )

    if cancelled is not None and cancelled():
        raise RouteGenerationCancelled("planning route generation was cancelled")

    routes.sort(key=lambda route: route.canonical_key)
    pruned = considered - len(routes)
    rejection_counts: tuple[tuple[PlanReasonCode, int], ...] = ()
    if pruned:
        rejection_counts = ((PlanReasonCode.TRANSPORT_FORBIDDEN, pruned),)
    return RouteGenerationResult(tuple(routes), considered, pruned, rejection_counts)


def generate_arbitrage_routes(
    constraints: FindMoneyConstraints,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> RouteGenerationResult:
    """Generate bounded outer-Royal source -> destination market routes."""

    sources = _canonical_cities(constraints.arbitrage_source_cities)
    destinations = _canonical_cities(constraints.arbitrage_destination_cities)
    considered = len(sources) * len(destinations)
    routes: list[CandidateRoute] = []
    for index, (source, destination) in enumerate(product(sources, destinations)):
        if index % 64 == 0 and cancelled is not None and cancelled():
            raise RouteGenerationCancelled("arbitrage route generation was cancelled")
        if source.casefold() == destination.casefold():
            continue
        if constraints.transport_policy is TransportPolicy.LOCAL_ONLY:
            continue
        transport_cost = 0
        if constraints.transport_policy is TransportPolicy.ACKNOWLEDGED_UNCOSTED:
            reasons = (
                PlanReason(
                    PlanReasonCode.UNMODELED_TRANSPORT,
                    "Transport cost omitted by explicit user choice; travel time, capacity, "
                    "and risk are also unmodeled.",
                    PlanReasonSeverity.WARNING,
                ),
            )
        else:
            assert constraints.transport_cost_per_action_unit is not None
            transport_cost = constraints.transport_cost_per_action_unit
            reasons = (
                PlanReason(
                    PlanReasonCode.UNMODELED_TRANSPORT,
                    f"A user-supplied transport cost of {transport_cost:,} silver per item is "
                    "applied; travel time, capacity, and risk remain unmodeled.",
                    PlanReasonSeverity.INFO,
                ),
            )
        routes.append(
            CandidateRoute(
                constraints.region,
                source,
                source,
                destination,
                constraints.transport_policy,
                transport_cost,
                reasons,
            )
        )
    if cancelled is not None and cancelled():
        raise RouteGenerationCancelled("arbitrage route generation was cancelled")
    routes.sort(key=lambda route: route.canonical_key)
    pruned = considered - len(routes)
    rejection_counts = ((PlanReasonCode.TRANSPORT_FORBIDDEN, pruned),) if pruned else ()
    return RouteGenerationResult(tuple(routes), considered, pruned, rejection_counts)


def _canonical_cities(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(values, key=lambda value: (value.casefold(), value)))
