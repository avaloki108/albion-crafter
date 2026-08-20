from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import (
    FindMoneyConstraints,
    OptimizationResult,
    PlanDataHealth,
    PlanReasonCode,
    PlanSnapshot,
    TransportPolicy,
)


@dataclass(frozen=True, slots=True)
class PlanExplanation:
    assumptions: tuple[str, ...]
    data_health: tuple[str, ...]
    optimizer: tuple[str, ...]
    unused_resources: tuple[str, ...]
    rejection_summary: tuple[str, ...]


def default_plan_assumptions(constraints: FindMoneyConstraints) -> tuple[str, ...]:
    assumptions = [
        f"Premium {'enabled' if constraints.premium else 'disabled'}.",
        "Normal quality only; equipment-quality expected value is not modeled.",
        f"Sale method: {constraints.sale_method.value.replace('_', ' ')}.",
        "Production materials and arbitrage sources use current minimum sell-order acquisition.",
        f"Required market observations may be at most {_hours(constraints.max_market_age)} old.",
        f"Station-fee observations may be at most {_hours(constraints.max_station_fee_age)} old.",
        "Pre-revenue capital uses gross input purchase, station, sell-order setup, and "
        "explicit transport cash; transaction tax is deducted after sale.",
        "Expected material returns reduce economic cost but never fund another plan action.",
        "Current top-of-book prices do not model live order-book depth.",
    ]
    if constraints.history_enabled and constraints.historical_volume_share is not None:
        assumptions.append(
            "Historical-volume execution ceiling is "
            f"{constraints.historical_volume_share:.1%} of positive reported 24h market "
            "activity, bounded by the explicit action-unit/batch cap; history is not live depth."
        )
    else:
        assumptions.append(
            f"Quantity uses the explicit shared cap of "
            f"{constraints.per_item_craft_cap:,} action units/batches."
        )
    assumptions.append(
        "Craft/refine input-material acquisition does not yet have shared historical capacity; "
        "arbitrage source acquisition and every supported sale output do."
    )
    assumptions.append(_transport_assumption(constraints))
    if constraints.allow_stale_station_fees:
        assumptions.append(
            "Stale station fees are explicitly allowed only as an advisory assumption."
        )
    return tuple(assumptions)


def explain_unused_resources(
    result: OptimizationResult,
    constraints: FindMoneyConstraints,
    *,
    next_cash_required: int | None = None,
    next_focus_required: int | None = None,
) -> tuple[str, ...]:
    """Explain leftovers without claiming that every remaining silver unit is deployable."""

    deployable_silver_remaining = constraints.silver_budget - result.total_pre_revenue_cash
    deployable_focus_remaining = constraints.focus_budget - result.total_focus
    explanations: list[str] = []
    if deployable_silver_remaining > 0:
        if next_cash_required is not None and next_cash_required > deployable_silver_remaining:
            explanations.append(
                f"{deployable_silver_remaining:,} deployable silver remains because the "
                f"smallest otherwise-eligible next batch requires {next_cash_required:,}."
            )
        else:
            explanations.append(
                f"{deployable_silver_remaining:,} deployable silver remains because no "
                "additional positive-profit action fits every current trust, quantity, and "
                "resource constraint."
            )
    if constraints.silver_reserve:
        explanations.append(
            f"{constraints.silver_reserve:,} silver remains untouched by explicit reserve."
        )
    if deployable_focus_remaining > 0 and constraints.use_focus:
        if next_focus_required is not None and next_focus_required > deployable_focus_remaining:
            explanations.append(
                f"{deployable_focus_remaining:,} deployable Focus remains because the smallest "
                f"otherwise-eligible focused batch requires {next_focus_required:,}."
            )
        else:
            explanations.append(
                f"{deployable_focus_remaining:,} deployable Focus remains because allocating it "
                "does not improve expected profit within the other constraints."
            )
    if constraints.focus_reserve:
        explanations.append(
            f"{constraints.focus_reserve:,} Focus remains untouched by explicit reserve."
        )
    return tuple(explanations)


def summarize_rejections(
    rejection_counts: Mapping[PlanReasonCode | str, int],
) -> tuple[str, ...]:
    rows: list[tuple[str, int]] = []
    for code, count in rejection_counts.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("rejection counts must be non-negative integers")
        if count:
            value = code.value if isinstance(code, PlanReasonCode) else str(code)
            rows.append((value, count))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return tuple(f"{count:,} rejected: {code.replace('_', ' ')}." for code, count in rows)


def summarize_data_health(health: PlanDataHealth) -> tuple[str, ...]:
    return (
        f"Market observations: {health.market_observations_used:,} used; "
        f"{health.market_fresh:,} fresh; {health.market_stale:,} stale; "
        f"{health.user_overrides_used:,} user overrides.",
        f"Station fees: {health.station_fees_used:,} used; "
        f"{health.station_fees_fresh:,} fresh; {health.station_fees_stale:,} stale.",
        f"Mechanics status: {health.mechanics_status}.",
    )


def build_plan_explanation(
    snapshot: PlanSnapshot,
    *,
    rejection_counts: Mapping[PlanReasonCode | str, int] | None = None,
) -> PlanExplanation:
    optimizer = snapshot.optimizer
    routes_before = optimizer.candidate_routes_before_pruning or optimizer.candidate_count
    routes_after = optimizer.candidate_routes_after_pruning or optimizer.candidate_count
    optimizer_lines = (
        f"Optimization: {optimizer.status.value.upper()} using {optimizer.method}.",
        f"Routes: {routes_before:,} before safe preprocessing, {routes_after:,} after; "
        f"{optimizer.candidate_local_modes_removed:,} candidate-local modes removed and "
        f"{optimizer.equivalent_routes_collapsed:,} equivalent routes collapsed.",
        f"Quantity states: {optimizer.quantity_states_generated:,} generated, "
        f"{optimizer.quantity_states_after_pruning:,} retained; portfolio states: "
        f"{optimizer.portfolio_states_considered:,} considered, "
        f"{optimizer.portfolio_states_pruned:,} pruned; peak frontier "
        f"{optimizer.peak_frontier_size:,}.",
        f"{optimizer.states_considered:,} total transitions; "
        f"{optimizer.states_pruned:,} states pruned; elapsed "
        f"{optimizer.elapsed_seconds:.3f}s.",
        f"Quantization: {optimizer.quantization_policy}.",
        (
            f"Safety limit reached ({', '.join(optimizer.approximation_reasons)}); all resource "
            "constraints remain exact, while optimality is approximate."
            if optimizer.state_limit_reached
            else f"The {optimizer.state_limit:,}-state safety limit was not reached."
        ),
    )
    result = OptimizationResult(
        snapshot.actions,
        snapshot.total_pre_revenue_cash,
        snapshot.total_focus,
        snapshot.total_expected_profit,
        snapshot.silver_remaining,
        snapshot.focus_remaining,
        snapshot.plan_status,
        snapshot.reasons,
        snapshot.optimizer,
    )
    return PlanExplanation(
        snapshot.assumptions or default_plan_assumptions(snapshot.constraints),
        summarize_data_health(snapshot.data_health),
        optimizer_lines,
        explain_unused_resources(result, snapshot.constraints),
        summarize_rejections(rejection_counts or {}),
    )


def _transport_assumption(constraints: FindMoneyConstraints) -> str:
    if constraints.transport_policy is TransportPolicy.LOCAL_ONLY:
        return " ".join(
            (
                "Transport is disabled;",
                "only same-city material, production, and sale routes are used.",
            )
        )
    if constraints.transport_policy is TransportPolicy.ACKNOWLEDGED_UNCOSTED:
        return (
            "Cross-city transport is acknowledged without a silver cost; time, capacity, and "
            "risk are unmodeled, so affected actions are advisory."
        )
    return (
        f"Cross-city routes apply {constraints.transport_cost_per_craft or 0:,} silver per batch; "
        "time, capacity, and risk remain unmodeled."
    )


def _hours(value) -> str:
    hours = value.total_seconds() / 3600
    return f"{hours:g} hours"
