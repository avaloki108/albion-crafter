from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning import (
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationResult,
    PlanCandidate,
    PlanReasonCode,
    PlanStatus,
    QuantityCeiling,
    QuantityCeilingSource,
    TransportPolicy,
    default_freshness_hooks,
    optimize_plan,
    station_fee_freshness_hook,
    summarize_rejections,
    validate_plan,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _fixture(
    *, market_age: timedelta = timedelta(minutes=5), station_age: timedelta = timedelta(hours=1)
):
    constraints = FindMoneyConstraints(
        1_000,
        100,
        per_item_craft_cap=2,
        history_enabled=False,
    )
    route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Bridgewatch",
        TransportPolicy.LOCAL_ONLY,
    )
    candidate = PlanCandidate(
        "candidate",
        "T4_SWORD",
        "Sword",
        route,
        CandidateEconomics(100, 20, 30, 10),
        liquidity=LiquidityLevel.MODERATE,
        oldest_market_observed_at=NOW - market_age,
        station_fee_observed_at=NOW - station_age,
    )
    key = candidate.execution_capacity_key
    ceiling = QuantityCeiling(
        key,
        2,
        None,
        QuantityCeilingSource.EXPLICIT_CAP,
        explanation="Test fixture ceiling.",
    )
    ceilings = {key: ceiling}
    result = optimize_plan((candidate,), ceilings, constraints)
    return constraints, ceilings, result


def test_independent_validation_recomputes_resources_capacity_and_final_freshness() -> None:
    constraints, ceilings, result = _fixture()
    validation = validate_plan(
        result,
        constraints,
        ceilings,
        as_of=NOW,
        freshness_hooks=default_freshness_hooks(constraints),
    )
    assert validation.is_feasible
    assert validation.is_decision_grade
    assert validation.total_pre_revenue_cash == sum(
        action.pre_revenue_cash_required for action in result.actions
    )
    assert validation.total_focus == sum(action.focus_required for action in result.actions)


def test_validation_rejects_tampered_optimizer_totals() -> None:
    constraints, ceilings, result = _fixture()
    tampered = replace(result, total_pre_revenue_cash=result.total_pre_revenue_cash - 1)
    validation = validate_plan(tampered, constraints, ceilings, as_of=NOW)
    assert not validation.is_feasible
    assert PlanReasonCode.INVALID_RESOURCE_TOTAL in {reason.code for reason in validation.reasons}


def test_validation_rejects_shared_capacity_double_spend() -> None:
    constraints, ceilings, result = _fixture()
    action = result.actions[0]
    duplicate = replace(action, candidate_id="duplicate")
    actions = (action, duplicate)
    cash = sum(value.pre_revenue_cash_required for value in actions)
    focus = sum(value.focus_required for value in actions)
    profit = sum(value.expected_profit for value in actions)
    tampered = OptimizationResult(
        actions,
        cash,
        focus,
        profit,
        constraints.available_silver - cash,
        constraints.available_focus - focus,
        PlanStatus.DECISION_GRADE,
        (),
        result.diagnostics,
    )
    validation = validate_plan(tampered, constraints, ceilings, as_of=NOW)
    assert not validation.is_feasible
    assert PlanReasonCode.QUANTITY_CEILING_EXCEEDED in {
        reason.code for reason in validation.reasons
    }


def test_final_market_freshness_is_advisory_not_a_restriction() -> None:
    constraints, ceilings, result = _fixture(market_age=timedelta(hours=5))
    validation = validate_plan(
        result,
        constraints,
        ceilings,
        as_of=NOW,
        freshness_hooks=default_freshness_hooks(constraints),
    )
    assert validation.status is PlanStatus.ADVISORY
    assert validation.is_feasible
    assert PlanReasonCode.STALE_MARKET_DATA in {reason.code for reason in validation.reasons}


def test_final_market_freshness_rejects_materially_future_timestamp() -> None:
    constraints, ceilings, result = _fixture(market_age=timedelta(minutes=-5))
    validation = validate_plan(
        result,
        constraints,
        ceilings,
        as_of=NOW,
        freshness_hooks=default_freshness_hooks(constraints),
    )
    assert validation.status is PlanStatus.NON_ACTIONABLE
    assert PlanReasonCode.FUTURE_MARKET_DATA in {reason.code for reason in validation.reasons}


def test_explicit_stale_station_mode_is_advisory_not_decision_grade() -> None:
    constraints, ceilings, result = _fixture(station_age=timedelta(days=2))
    allowed = replace(constraints, allow_stale_station_fees=True)
    validation = validate_plan(
        result,
        allowed,
        ceilings,
        as_of=NOW,
        freshness_hooks=(
            station_fee_freshness_hook(
                allowed.max_station_fee_age,
                allow_stale=allowed.allow_stale_station_fees,
            ),
        ),
    )
    assert validation.is_feasible
    assert validation.status is PlanStatus.ADVISORY
    assert not validation.is_decision_grade


def test_stale_station_allowance_never_accepts_future_dated_evidence() -> None:
    constraints, ceilings, result = _fixture(station_age=timedelta(minutes=-5))
    allowed = replace(constraints, allow_stale_station_fees=True)
    validation = validate_plan(
        result,
        allowed,
        ceilings,
        as_of=NOW,
        freshness_hooks=(
            station_fee_freshness_hook(
                allowed.max_station_fee_age,
                allow_stale=allowed.allow_stale_station_fees,
            ),
        ),
    )
    assert validation.status is PlanStatus.NON_ACTIONABLE
    assert PlanReasonCode.FUTURE_STATION_FEE in {reason.code for reason in validation.reasons}


def test_rejection_summary_is_count_then_code_deterministic() -> None:
    summary = summarize_rejections(
        {
            PlanReasonCode.UNKNOWN_FCE: 2,
            PlanReasonCode.STALE_STATION_FEE: 5,
            PlanReasonCode.STALE_MARKET_DATA: 5,
            PlanReasonCode.OTHER: 0,
        }
    )
    assert summary == (
        "5 rejected: stale market data.",
        "5 rejected: stale station fee.",
        "2 rejected: unknown fce.",
    )
