from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime

from albion_crafter.core.arbitrage import calculate_arbitrage_economics
from albion_crafter.core.freshness import Freshness, FreshnessPolicy
from albion_crafter.core.mechanics import CURRENT_RULES, MechanicsRules, VerificationStatus
from albion_crafter.core.models import ActionKind
from albion_crafter.market.history import MarketHistoryInterval
from albion_crafter.market.liquidity import LiquidityAssessment, LiquidityLevel
from albion_crafter.market.models import MarketPrice, Region, UserPriceOverride
from albion_crafter.market.pricing import resolve_price

from .candidates import CandidateEvaluationResult, CandidateNearMiss
from .models import (
    CandidateEconomics,
    CapacityRequirement,
    CapacityRole,
    FindMoneyConstraints,
    PlanCandidate,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    quantize_profit_down,
    quantize_resource_up,
)
from .preflight import EligibleArbitrageRoute

LiquidityKey = tuple[Region, str, str, int]
ProgressCallback = Callable[[int, int], None]
CancellationCheck = Callable[[], bool]


class ArbitrageCandidateEvaluator:
    """Evaluate source-buy/destination-sale routes from preloaded evidence only."""

    def __init__(self, rules: MechanicsRules = CURRENT_RULES) -> None:
        self.rules = rules

    def evaluate(
        self,
        eligible: Iterable[EligibleArbitrageRoute],
        market_prices: Iterable[MarketPrice],
        overrides: Iterable[UserPriceOverride],
        constraints: FindMoneyConstraints,
        *,
        history: Iterable[MarketHistoryInterval] = (),
        liquidity_by_key: Mapping[LiquidityKey, LiquidityAssessment] | None = None,
        as_of: datetime | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> CandidateEvaluationResult:
        started = time.perf_counter()
        evaluation_time = as_of or datetime.now(UTC)
        if evaluation_time.tzinfo is None:
            raise ValueError("arbitrage evaluation as_of must be timezone-aware")
        routes = tuple(
            sorted(
                eligible,
                key=lambda value: (value.item.item_id, value.route.canonical_key),
            )
        )
        market_index = {
            (row.region, row.item_id, row.city, row.quality): row for row in market_prices
        }
        override_index = {
            (row.region, row.item_id, row.city, row.quality, row.side): row for row in overrides
        }
        history_index: dict[LiquidityKey, list[MarketHistoryInterval]] = {}
        for row in history:
            history_index.setdefault((row.region, row.item_id, row.city, row.quality), []).append(
                row
            )
        policy = FreshnessPolicy(constraints.max_market_age)
        liquidity_index = liquidity_by_key or {}
        candidates: list[PlanCandidate] = []
        misses: list[CandidateNearMiss] = []
        rejected: Counter[str] = Counter()

        for position, eligible_route in enumerate(routes, start=1):
            if cancelled is not None and cancelled():
                return CandidateEvaluationResult(
                    tuple(candidates),
                    tuple(misses),
                    tuple(sorted(rejected.items())),
                    position - 1,
                    max(time.perf_counter() - started, 0.0),
                    True,
                )
            item = eligible_route.item
            route = eligible_route.route
            source = self._resolve(
                item.item_id,
                route.buy_city,
                eligible_route.source_price_side,
                "arbitrage_source",
                constraints,
                policy,
                evaluation_time,
                market_index,
                override_index,
                history_index,
            )
            destination = self._resolve(
                item.item_id,
                route.sell_city,
                eligible_route.destination_price_side,
                "arbitrage_destination",
                constraints,
                policy,
                evaluation_time,
                market_index,
                override_index,
                history_index,
            )
            candidate_id = "|".join(
                (
                    ActionKind.ARBITRAGE.value,
                    item.item_id,
                    constraints.region.value,
                    route.buy_city,
                    route.sell_city,
                    constraints.sale_method.value,
                    "q1",
                )
            )
            reasons = [*route.reasons]
            reasons.extend(self._price_reasons(source, source=True, candidate_id=candidate_id))
            reasons.extend(
                self._price_reasons(destination, source=False, candidate_id=candidate_id)
            )
            if route.buy_city.casefold() == route.sell_city.casefold():
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        "Arbitrage source and destination cities must differ.",
                    )
                )
            if self.rules.component_status("marketplace_fees") is not VerificationStatus.VERIFIED:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNVERIFIED_MECHANICS,
                        "Marketplace fee mechanics are not verified for decision-grade arbitrage.",
                    )
                )
            blocking = any(reason.severity is PlanReasonSeverity.BLOCKING for reason in reasons)
            if source.price is None or destination.price is None or blocking:
                misses.append(
                    CandidateNearMiss(
                        ActionKind.ARBITRAGE,
                        item.item_id,
                        item.display_name,
                        candidate_id,
                        f"{route.buy_city} -> {route.sell_city}",
                        None,
                        _deduplicate_reasons(reasons),
                    )
                )
                for reason in reasons:
                    if reason.severity is PlanReasonSeverity.BLOCKING:
                        rejected[reason.code.value] += 1
                if progress is not None:
                    progress(position, len(routes))
                continue

            economics = calculate_arbitrage_economics(
                source.price,
                destination.price,
                premium=constraints.premium,
                sale_method=constraints.sale_method,
                transport_cash_per_unit=route.transport_cost_per_action_unit,
                rules=self.rules,
            )
            source_key = (constraints.region, item.item_id, route.buy_city, 1)
            destination_key = (constraints.region, item.item_id, route.sell_city, 1)
            source_liquidity = liquidity_index.get(source_key)
            destination_liquidity = liquidity_index.get(destination_key)
            liquidity = _minimum_liquidity(source_liquidity, destination_liquidity)
            if constraints.history_enabled and (
                source_liquidity is None
                or destination_liquidity is None
                or liquidity is LiquidityLevel.UNKNOWN
            ):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_LIQUIDITY,
                        "Complete source acquisition and destination liquidation history is "
                        "unavailable; history is an execution proxy, not live depth.",
                        PlanReasonSeverity.WARNING,
                    )
                )

            timestamps = tuple(
                value
                for value in (source.observation_timestamp, destination.observation_timestamp)
                if value is not None
            )
            pre_revenue = quantize_resource_up(economics.pre_revenue_cash)
            profit = quantize_profit_down(economics.expected_profit)
            if profit <= 0:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        "The apparent arbitrage spread is nonpositive after marketplace fees "
                        "and explicit transport.",
                    )
                )
            if constraints.minimum_profit is not None and profit < constraints.minimum_profit:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        "Expected arbitrage profit is below the selected minimum.",
                    )
                )
            if constraints.minimum_roi is not None and (
                economics.roi is None or economics.roi < constraints.minimum_roi
            ):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        "Expected arbitrage ROI is below the selected minimum.",
                    )
                )
            effective_cost = quantize_resource_up(economics.effective_economic_cost)
            purchase = quantize_resource_up(economics.purchase_cash)
            setup = quantize_resource_up(economics.setup_cash)
            transport = quantize_resource_up(economics.transport_cash)
            candidate = PlanCandidate(
                candidate_id,
                item.item_id,
                item.display_name,
                route,
                CandidateEconomics(
                    pre_revenue,
                    profit,
                    nonfocused_eligible=True,
                    expected_revenue_per_craft=quantize_resource_up(
                        economics.gross_destination_value
                    ),
                    nonfocused_effective_cost_per_craft=effective_cost,
                    gross_material_cash_per_craft=purchase,
                    station_cash_per_craft=0,
                    setup_cash_per_craft=setup,
                    transport_cash_per_craft=transport,
                ),
                action_kind=ActionKind.ARBITRAGE,
                output_quantity_per_craft=1,
                sale_method=constraints.sale_method,
                liquidity=liquidity,
                nonfocused_roi=economics.roi,
                capacity_requirements=(
                    CapacityRequirement(source_key, CapacityRole.ACQUISITION, 1),
                    CapacityRequirement(destination_key, CapacityRole.LIQUIDATION, 1),
                ),
                reasons=_deduplicate_reasons(reasons),
                evidence=_evidence(
                    eligible_route,
                    source,
                    destination,
                    source_liquidity,
                    destination_liquidity,
                    economics,
                    constraints,
                    self.rules,
                ),
                oldest_market_observed_at=min(timestamps) if len(timestamps) == 2 else None,
            )
            candidates.append(candidate)
            economic_blockers = tuple(
                reason
                for reason in candidate.reasons
                if reason.severity is PlanReasonSeverity.BLOCKING
            )
            if economic_blockers:
                misses.append(
                    CandidateNearMiss(
                        ActionKind.ARBITRAGE,
                        item.item_id,
                        item.display_name,
                        candidate_id,
                        f"{route.buy_city} -> {route.sell_city}",
                        profit,
                        economic_blockers,
                    )
                )
                for reason in economic_blockers:
                    rejected[reason.code.value] += 1
            if progress is not None:
                progress(position, len(routes))

        return CandidateEvaluationResult(
            tuple(sorted(candidates, key=lambda value: value.canonical_key)),
            tuple(sorted(misses, key=lambda value: value.candidate_id)),
            tuple(sorted(rejected.items())),
            len(routes),
            max(time.perf_counter() - started, 0.0),
        )

    @staticmethod
    def _resolve(
        item_id,
        city,
        side,
        role,
        constraints,
        policy,
        as_of,
        market_index,
        override_index,
        history_index,
    ):
        return resolve_price(
            item_id=item_id,
            city=city,
            quality=1,
            side=side,
            role=role,
            freshness_policy=policy,
            as_of=as_of,
            market_price=market_index.get((constraints.region, item_id, city, 1)),
            override=override_index.get((constraints.region, item_id, city, 1, side)),
            history=history_index.get((constraints.region, item_id, city, 1), ()),
        )

    @staticmethod
    def _price_reasons(line, *, source: bool, candidate_id: str) -> tuple[PlanReason, ...]:
        label = "source" if source else "destination"
        code = (
            PlanReasonCode.MISSING_MATERIAL_PRICE if source else PlanReasonCode.MISSING_OUTPUT_PRICE
        )
        reasons: list[PlanReason] = []
        if line.price is None or line.price <= 0:
            reasons.append(PlanReason(code, f"Arbitrage {label} price is missing or nonpositive."))
        if line.is_historical_estimate:
            reasons.append(
                PlanReason(
                    PlanReasonCode.OTHER,
                    f"Arbitrage {label} uses a {line.confidence.value} confidence AODP "
                    f"historical SELL estimate from {line.historical_days_used} day(s).",
                    PlanReasonSeverity.WARNING,
                )
            )
        if line.price is not None and not line.provenance.is_actionable_price_source:
            reasons.append(
                PlanReason(
                    PlanReasonCode.UNTRUSTED_PROVENANCE,
                    f"Action {candidate_id} uses untrusted {label} price provenance.",
                )
            )
        if line.freshness is Freshness.FUTURE:
            reasons.append(
                PlanReason(
                    PlanReasonCode.FUTURE_MARKET_DATA,
                    f"Arbitrage {label} price is materially future-dated.",
                )
            )
        elif line.freshness not in {Freshness.FRESH, Freshness.AGING}:
            reasons.append(
                PlanReason(
                    PlanReasonCode.STALE_MARKET_DATA,
                    f"Arbitrage {label} uses the latest available price; its timestamp is old "
                    "or unavailable.",
                    PlanReasonSeverity.WARNING,
                )
            )
        return tuple(reasons)


def _minimum_liquidity(
    source: LiquidityAssessment | None,
    destination: LiquidityAssessment | None,
) -> LiquidityLevel:
    rank = {
        LiquidityLevel.UNKNOWN: 0,
        LiquidityLevel.LOW: 1,
        LiquidityLevel.MODERATE: 2,
        LiquidityLevel.HIGH: 3,
    }
    if source is None or destination is None:
        return LiquidityLevel.UNKNOWN
    return min((source.level, destination.level), key=rank.__getitem__)


def _liquidity_payload(value: LiquidityAssessment | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "level": value.level.value,
        "reported_volume": value.reported_volume,
        "active_intervals": value.active_intervals,
        "weighted_mean_price": value.weighted_mean_price,
        "current_price_deviation": value.current_price_deviation,
        "last_activity_at": (
            value.last_activity_at.astimezone(UTC).isoformat()
            if value.last_activity_at is not None
            else None
        ),
        "reasons": list(value.reasons),
    }


def _evidence(
    eligible,
    source,
    destination,
    source_liquidity,
    destination_liquidity,
    economics,
    constraints,
    rules,
) -> tuple[tuple[str, str], ...]:
    prices = [
        {
            "item_id": line.item_id,
            "city": line.city,
            "side": line.side.value,
            "price": line.price,
            "observed_at": (
                line.observation_timestamp.astimezone(UTC).isoformat()
                if line.observation_timestamp is not None
                else None
            ),
            "provenance": line.provenance.value,
            "freshness": line.freshness.value,
            "role": line.role,
            "source": line.source.value,
            "confidence": line.confidence.value,
            "current_price": line.current_price,
            "current_observed_at": (
                line.current_timestamp.astimezone(UTC).isoformat()
                if line.current_timestamp is not None
                else None
            ),
            "historical_reference_price": line.historical_reference_price,
            "historical_days_used": line.historical_days_used,
            "historical_total_volume": line.historical_total_volume,
            "historical_avg_daily_volume_7d": line.historical_avg_daily_volume_7d,
        }
        for line in (source, destination)
    ]
    payloads = {
        "arbitrage": {
            "action_kind": ActionKind.ARBITRAGE.value,
            "item_id": eligible.item.item_id,
            "display_name": eligible.item.display_name,
            "tier": eligible.item.tier,
            "enchantment": eligible.item.enchantment,
            "category": eligible.item.crafting_category,
            "source_city": eligible.route.buy_city,
            "destination_city": eligible.route.sell_city,
            "quality": 1,
        },
        "prices": prices,
        "marketplace": {
            "ruleset_id": rules.ruleset_id,
            "premium": constraints.premium,
            "sale_method": constraints.sale_method.value,
            "setup_rate": rules.sell_order_setup_fee,
            "transaction_tax_rate": rules.transaction_tax(premium=constraints.premium),
            "marketplace_fee_status": rules.component_status("marketplace_fees").value,
        },
        "arbitrage_accounting": {
            "purchase_cash": economics.purchase_cash,
            "gross_destination_value": economics.gross_destination_value,
            "setup_cash": economics.setup_cash,
            "transaction_tax": economics.transaction_tax,
            "transport_cash": economics.transport_cash,
            "pre_revenue_cash": economics.pre_revenue_cash,
            "net_sale_proceeds": economics.net_sale_proceeds,
            "effective_economic_cost": economics.effective_economic_cost,
            "expected_profit": economics.expected_profit,
            "roi": economics.roi,
            "margin": economics.margin,
            "focus": None,
            "station": None,
            "rrr": None,
            "fce": None,
        },
        "transport": {
            "policy": eligible.route.transport_policy.value,
            "cost_per_action_unit": eligible.route.transport_cost_per_action_unit,
        },
        "capacity_evidence": {
            "source": _liquidity_payload(source_liquidity),
            "destination": _liquidity_payload(destination_liquidity),
            "warning": "Historical volume is a conservative execution proxy, not live depth.",
        },
    }
    return tuple(
        sorted(
            (
                key,
                json.dumps(value, sort_keys=True, separators=(",", ":")),
            )
            for key, value in payloads.items()
        )
    )


def _deduplicate_reasons(reasons) -> tuple[PlanReason, ...]:
    unique = {(reason.code, reason.message, reason.severity): reason for reason in reasons}
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda item: (item[2].value, item[0].value, item[1]))
    )
