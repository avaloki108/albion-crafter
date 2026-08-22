from __future__ import annotations

import json
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.freshness import (
    Freshness,
    FreshnessPolicy,
    future_offset_beyond_tolerance,
)
from albion_crafter.core.mechanics import CURRENT_RULES, MechanicsRules
from albion_crafter.core.models import ActionKind
from albion_crafter.database.database import MarketPriceRepository, PriceOverrideRepository
from albion_crafter.database.v3 import CraftingProfileRepository, MarketHistoryRepository
from albion_crafter.market.estimation import DEFAULT_HISTORICAL_ESTIMATION_POLICY
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.history_cache import (
    CachedHistoryRefreshResult,
    CachedOutputHistoryService,
)
from albion_crafter.market.liquidity import LiquidityAssessment, assess_liquidity
from albion_crafter.market.models import MarketPrice, MarketSide, Region, UserPriceOverride

from .arbitrage import ArbitrageCandidateEvaluator
from .candidates import (
    CandidateEvaluationResult,
    CandidatePruningCancelled,
    CandidateShortlist,
    PlanCandidateEvaluator,
    prune_dominated_candidates,
    shortlist_candidates,
)
from .current_refresh import CurrentMarketRefreshExecutor, CurrentRefreshResult
from .explanations import default_plan_assumptions
from .models import (
    ExecutionCapacityKey,
    FindMoneyConstraints,
    MinimumLiquidity,
    OptimizationResult,
    OptimizationStatus,
    PlanDataHealth,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanSnapshot,
    PriceRole,
    RefreshStatistics,
)
from .optimizer import (
    DEFAULT_OPTIMIZER_LIMITS,
    OptimizerLimits,
    PlanningCancelled,
    PlanningOptimizer,
)
from .preflight import FindMoneyPreflight, FindMoneyPreflightPlanner
from .quantity import QuantityCeiling, calculate_quantity_ceiling
from .routes import RouteGenerationCancelled
from .validation import (
    PlanValidationResult,
    action_evidence_hook,
    default_freshness_hooks,
    validate_plan,
)

CancellationCheck = Callable[[], bool]
Clock = Callable[[], datetime]
IdentifierFactory = Callable[[datetime], str]
LiquidityKey = tuple[Region, str, str, int]


class PlanSnapshotWriter(Protocol):
    def save(self, snapshot: PlanSnapshot) -> PlanSnapshot: ...


class PlanningStage(StrEnum):
    PREFLIGHT = "preflight"
    CURRENT_REFRESH = "current_refresh"
    INITIAL_EVALUATION = "initial_evaluation"
    SHORTLIST = "shortlist"
    HISTORY_REFRESH = "history_refresh"
    FINAL_EVALUATION = "final_evaluation"
    OPTIMIZATION = "optimization"
    VALIDATION = "validation"
    PERSISTENCE = "persistence"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PlanningProgress:
    stage: PlanningStage
    message: str
    completed: int = 0
    total: int | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(max(self.completed / self.total, 0.0), 1.0)


ProgressCallback = Callable[[PlanningProgress], None]


@dataclass(frozen=True, slots=True)
class HistoryRefreshAggregate:
    requested_keys: tuple[LiquidityKey, ...]
    outcomes: tuple[CachedHistoryRefreshResult, ...]
    batches_planned: int
    batches_completed: int
    batches_failed: int
    records_loaded: int
    request_attempts: int
    retry_count: int
    elapsed_seconds: float
    cancelled: bool

    @property
    def statistics(self) -> RefreshStatistics:
        return RefreshStatistics(
            keys_required=len(self.requested_keys),
            batches_planned=self.batches_planned,
            batches_completed=self.batches_completed,
            batches_failed=self.batches_failed,
            records_loaded=self.records_loaded,
            elapsed_seconds=self.elapsed_seconds,
        )


@dataclass(frozen=True, slots=True)
class FindMoneyRunResult:
    started_at: datetime
    completed_at: datetime
    preflight: FindMoneyPreflight
    initial_evaluation: CandidateEvaluationResult | None
    shortlist: CandidateShortlist | None
    final_evaluation: CandidateEvaluationResult | None
    liquidity: tuple[tuple[LiquidityKey, LiquidityAssessment], ...]
    ceilings: tuple[tuple[ExecutionCapacityKey, QuantityCeiling], ...]
    optimization: OptimizationResult | None
    validation: PlanValidationResult | None
    snapshot: PlanSnapshot | None
    current_refresh: CurrentRefreshResult | None
    history_refresh: HistoryRefreshAggregate | None
    rejection_counts: tuple[tuple[str, int], ...]
    cancelled: bool = False


class FindMoneyService:
    """Staged Find Me Money orchestration with explicit network boundaries.

    Constructing the service and calling :meth:`preflight` never performs HTTP.
    A caller must explicitly pass the returned preflight to :meth:`execute`,
    which makes it suitable for the two-stage Qt workflow.
    """

    def __init__(
        self,
        preflight_planner: FindMoneyPreflightPlanner,
        market_prices: MarketPriceRepository,
        overrides: PriceOverrideRepository,
        crafting_profiles: CraftingProfileRepository,
        history: MarketHistoryRepository,
        *,
        snapshots: PlanSnapshotWriter | None = None,
        current_refresh: CurrentMarketRefreshExecutor | None = None,
        history_refresh: CachedOutputHistoryService | None = None,
        evaluator: PlanCandidateEvaluator | None = None,
        arbitrage_evaluator: ArbitrageCandidateEvaluator | None = None,
        optimizer: PlanningOptimizer | None = None,
        rules: MechanicsRules = CURRENT_RULES,
        clock: Clock | None = None,
        identifier_factory: IdentifierFactory | None = None,
    ) -> None:
        self.preflight_planner = preflight_planner
        self.market_prices = market_prices
        self.overrides = overrides
        self.crafting_profiles = crafting_profiles
        self.history = history
        self.snapshots = snapshots
        self.current_refresh = current_refresh
        self.history_refresh = history_refresh
        self.evaluator = evaluator or PlanCandidateEvaluator(rules)
        self.arbitrage_evaluator = arbitrage_evaluator or ArbitrageCandidateEvaluator(rules)
        self.optimizer = optimizer or PlanningOptimizer()
        self.rules = rules
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or _snapshot_identifier

    def preflight(
        self,
        constraints: FindMoneyConstraints,
        *,
        as_of: datetime | None = None,
    ) -> FindMoneyPreflight:
        return self.preflight_planner.build(constraints, as_of=as_of or self._now())

    def execute(
        self,
        preflight: FindMoneyPreflight,
        *,
        optimizer_limits: OptimizerLimits = DEFAULT_OPTIMIZER_LIMITS,
        refresh_current: bool = True,
        refresh_history: bool = True,
        cancelled: CancellationCheck | None = None,
        progress: ProgressCallback | None = None,
    ) -> FindMoneyRunResult:
        started_wall = time.perf_counter()
        started_at = self._now()
        constraints = preflight.constraints
        rejections = Counter(dict(preflight.rejection_counts))
        self._report(
            progress,
            PlanningStage.PREFLIGHT,
            f"Preflight retained {len(preflight.eligible) + len(preflight.arbitrage_routes):,} "
            "production and arbitrage routes.",
        )
        if self._cancelled(cancelled):
            return self._cancelled_result(started_at, preflight, rejections)

        current_result: CurrentRefreshResult | None = None
        if (
            refresh_current
            and self.current_refresh is not None
            and preflight.market_refresh.refresh_keys
        ):
            self._report(
                progress,
                PlanningStage.CURRENT_REFRESH,
                f"Refreshing {len(preflight.market_refresh.refresh_keys):,} sparse price keys...",
            )
            current_result = self.current_refresh.execute(
                preflight.market_refresh,
                is_cancelled=cancelled,
                on_progress=(
                    None
                    if progress is None
                    else lambda value: self._report(
                        progress,
                        PlanningStage.CURRENT_REFRESH,
                        f"Current-price batches: {value.batches_completed:,} of "
                        f"{value.batches_planned:,} complete"
                        + (
                            f" · {value.batches_failed:,} failed; saved prices retained."
                            if value.batches_failed
                            else "."
                        ),
                        value.batches_completed,
                        value.batches_planned,
                    )
                ),
                on_history_progress=(
                    None
                    if progress is None
                    else lambda value: self._report(
                        progress,
                        PlanningStage.CURRENT_REFRESH,
                        "Missing-SELL history: "
                        f"{value.city} · city {value.city_number:,}/{value.city_count:,} · "
                        f"batch {value.batch_number:,}/{value.batch_count:,}.",
                        round(
                            1_000
                            * (
                                value.city_number
                                - 1
                                + value.batch_number / max(value.batch_count, 1)
                            )
                            / max(value.city_count, 1)
                        ),
                        1_000,
                    )
                ),
            )
            if current_result.circuit_breaker_open:
                self._report(
                    progress,
                    PlanningStage.CURRENT_REFRESH,
                    "AODP stopped responding repeatedly; skipped "
                    f"{current_result.groups_skipped:,} remaining batches and continuing with "
                    "saved prices.",
                    current_result.batches_completed,
                    current_result.batches_planned,
                )
            elif current_result.history_circuit_breaker_open:
                self._report(
                    progress,
                    PlanningStage.CURRENT_REFRESH,
                    "AODP history stopped responding repeatedly; continuing with saved current "
                    "prices and retained history.",
                    current_result.batches_completed,
                    current_result.batches_planned,
                )
            if current_result.cancelled or self._cancelled(cancelled):
                return self._cancelled_result(
                    started_at,
                    preflight,
                    rejections,
                    current_refresh=current_result,
                )

        # Rebuild from persisted cache even after partial failure. Successful
        # batches survive; failed keys remain stale/missing and affect only their routes.
        try:
            active_preflight = self.preflight_planner.build(
                constraints,
                as_of=self._now(),
                cancelled=cancelled,
            )
        except RouteGenerationCancelled:
            return self._cancelled_result(
                started_at,
                preflight,
                rejections,
                current_refresh=current_result,
            )
        rejections = Counter(dict(active_preflight.rejection_counts))
        price_rows, override_rows = self._load_current_rows(active_preflight)
        evaluation_at = self._now()
        price_history_rows = self._load_price_history(active_preflight, evaluation_at)
        profile = self.crafting_profiles.load() or CraftingSkillProfile(
            available_focus=constraints.available_focus
        )

        self._report(
            progress,
            PlanningStage.INITIAL_EVALUATION,
            "Evaluating "
            f"{len(active_preflight.eligible) + len(active_preflight.arbitrage_routes):,} "
            "routes from current prices...",
        )
        initial = self._evaluate_candidates(
            active_preflight.eligible,
            active_preflight.arbitrage_routes,
            price_rows,
            override_rows,
            price_history_rows,
            profile,
            constraints,
            as_of=evaluation_at,
            cancelled=cancelled,
            progress=progress,
            stage=PlanningStage.INITIAL_EVALUATION,
        )
        rejections.update(dict(initial.rejection_counts))
        if initial.cancelled or self._cancelled(cancelled):
            return self._cancelled_result(
                started_at,
                active_preflight,
                rejections,
                initial=initial,
                current_refresh=current_result,
            )

        shortlist_source = tuple(
            candidate
            for candidate in initial.candidates
            if _candidate_is_preoptimizer_eligible(candidate, constraints)
        )
        rejections.update(
            _candidate_filter_rejections(
                initial.candidates,
                constraints,
                include_liquidity=False,
            )
        )
        try:
            prehistory_pruning = prune_dominated_candidates(
                shortlist_source,
                constraints,
                cancelled=cancelled,
            )
        except CandidatePruningCancelled:
            return self._cancelled_result(
                started_at,
                active_preflight,
                rejections,
                initial=initial,
                current_refresh=current_result,
            )
        if prehistory_pruning.dominated_count:
            rejections["dominated_route"] += prehistory_pruning.dominated_count
        capacity_group_count = len(
            {candidate.capacity_signature for candidate in prehistory_pruning.candidates}
        )
        shortlist = shortlist_candidates(
            prehistory_pruning.candidates,
            maximum_capacity_groups=(
                constraints.history_shortlist_limit
                if constraints.history_enabled
                else max(capacity_group_count, 1)
            ),
            constraints=constraints,
        )
        not_shortlisted = max(
            shortlist.capacity_groups_considered - shortlist.capacity_groups_selected,
            0,
        )
        self._report(
            progress,
            PlanningStage.SHORTLIST,
            f"Selected {shortlist.capacity_groups_selected:,} of "
            f"{shortlist.capacity_groups_considered:,} profitable capacity groups.",
        )

        history_result: HistoryRefreshAggregate | None = None
        history_keys = (
            tuple(sorted(shortlist.selected_capacity_keys, key=_capacity_order))
            if constraints.history_enabled
            else ()
        )
        if (
            constraints.history_enabled
            and refresh_history
            and self.history_refresh is not None
            and history_keys
        ):
            self._report(
                progress,
                PlanningStage.HISTORY_REFRESH,
                f"Refreshing reported history for {len(history_keys):,} market-capacity keys...",
            )
            history_result = self._refresh_output_history(
                history_keys,
                as_of=self._now(),
                cancelled=cancelled,
                progress=progress,
            )
            if history_result.cancelled or self._cancelled(cancelled):
                return self._cancelled_result(
                    started_at,
                    active_preflight,
                    rejections,
                    initial=initial,
                    shortlist=shortlist,
                    current_refresh=current_result,
                    history_refresh=history_result,
                )

        completed_history_at = self._now()
        intervals_by_key, status_by_key = self._load_history(history_keys, completed_history_at)
        output_prices = _output_prices(active_preflight)
        liquidity = {
            key: assess_liquidity(
                intervals_by_key.get(key, ()),
                current_price=output_prices.get(key),
                now=completed_history_at,
                history_available=status_by_key.get(key) in {"success", "empty", "partial"},
                history_complete=status_by_key.get(key) in {"success", "empty"},
            )
            for key in history_keys
        }

        # Early pruning is useful for deterministic history-group ranking, but it
        # must not become a destructive trust boundary. Evidence can age while
        # history refresh runs, so re-evaluate every route that was eligible at
        # the initial timestamp and prune only the final evidence snapshot below.
        retained_route_keys = {
            (candidate.action_kind, candidate.item_id, candidate.route.canonical_key)
            for candidate in shortlist_source
        }
        selected_routes = tuple(
            value
            for value in active_preflight.eligible
            if (
                value.action_kind,
                value.recipe.output.item_id,
                value.route.canonical_key,
            )
            in retained_route_keys
        )
        selected_arbitrage_routes = tuple(
            value
            for value in active_preflight.arbitrage_routes
            if (ActionKind.ARBITRAGE, value.item.item_id, value.route.canonical_key)
            in retained_route_keys
        )
        self._report(
            progress,
            PlanningStage.FINAL_EVALUATION,
            f"Revalidating {len(selected_routes) + len(selected_arbitrage_routes):,} "
            "shortlisted routes with liquidity evidence...",
        )
        final_evaluation = self._evaluate_candidates(
            selected_routes,
            selected_arbitrage_routes,
            price_rows,
            override_rows,
            price_history_rows,
            profile,
            constraints,
            liquidity_by_key=liquidity,
            as_of=self._now(),
            cancelled=cancelled,
            progress=progress,
            stage=PlanningStage.FINAL_EVALUATION,
        )
        rejections.update(dict(final_evaluation.rejection_counts))
        rejections.update(_candidate_filter_rejections(final_evaluation.candidates, constraints))
        if final_evaluation.cancelled or self._cancelled(cancelled):
            return self._cancelled_result(
                started_at,
                active_preflight,
                rejections,
                initial=initial,
                shortlist=shortlist,
                final=final_evaluation,
                liquidity=liquidity,
                current_refresh=current_result,
                history_refresh=history_result,
            )

        unpruned_optimizer_candidates = tuple(
            candidate
            for candidate in final_evaluation.candidates
            if _candidate_is_preoptimizer_eligible(candidate, constraints)
            and candidate.liquidity_rank >= constraints.minimum_liquidity.minimum_rank
        )
        try:
            final_pruning = prune_dominated_candidates(
                unpruned_optimizer_candidates,
                constraints,
                cancelled=cancelled,
            )
        except CandidatePruningCancelled:
            return self._cancelled_result(
                started_at,
                active_preflight,
                rejections,
                initial=initial,
                shortlist=shortlist,
                final=final_evaluation,
                liquidity=liquidity,
                current_refresh=current_result,
                history_refresh=history_result,
            )
        optimizer_candidates = final_pruning.candidates
        ceilings = self._build_ceilings(
            optimizer_candidates,
            intervals_by_key,
            status_by_key,
            constraints,
            as_of=self._now(),
        )
        self._report(
            progress,
            PlanningStage.OPTIMIZATION,
            f"Allocating shared silver and Focus across {len(optimizer_candidates):,} "
            "route candidates...",
        )
        try:
            optimization = self.optimizer.optimize(
                optimizer_candidates,
                ceilings,
                constraints,
                limits=optimizer_limits,
                cancelled=cancelled,
            )
        except PlanningCancelled:
            return self._cancelled_result(
                started_at,
                active_preflight,
                rejections,
                initial=initial,
                shortlist=shortlist,
                final=final_evaluation,
                liquidity=liquidity,
                ceilings=ceilings,
                current_refresh=current_result,
                history_refresh=history_result,
            )
        optimization = replace(
            optimization,
            diagnostics=replace(
                optimization.diagnostics,
                candidate_routes_before_pruning=final_pruning.routes_before,
                candidate_routes_after_pruning=final_pruning.routes_after,
                candidate_local_modes_removed=final_pruning.local_modes_removed,
                equivalent_routes_collapsed=final_pruning.equivalent_routes_collapsed,
            ),
        )

        liquidity_shortlist_truncated = bool(
            constraints.history_enabled
            and constraints.minimum_liquidity is not MinimumLiquidity.ANY
            and not_shortlisted
        )
        if liquidity_shortlist_truncated:
            shortlist_reason = PlanReason(
                PlanReasonCode.APPROXIMATE_OPTIMIZATION,
                f"Liquidity policy required history, but the explicit "
                f"{constraints.history_shortlist_limit:,}-market enrichment limit omitted "
                f"{not_shortlisted:,} otherwise eligible capacity groups. Exactness is scoped "
                "to the history-enriched candidate universe.",
                PlanReasonSeverity.WARNING,
            )
            optimization = replace(
                optimization,
                reasons=tuple((*optimization.reasons, shortlist_reason)),
                diagnostics=replace(
                    optimization.diagnostics,
                    status=OptimizationStatus.APPROXIMATE,
                    method=optimization.diagnostics.method + "+bounded_history_shortlist",
                    approximation_reasons=tuple(
                        sorted(
                            {
                                *optimization.diagnostics.approximation_reasons,
                                "bounded_history_shortlist",
                            }
                        )
                    ),
                ),
            )

        completed_at = self._now()
        self._report(
            progress,
            PlanningStage.VALIDATION,
            "Independently recomputing resource totals and final evidence freshness...",
        )
        validation = validate_plan(
            optimization,
            constraints,
            ceilings,
            as_of=completed_at,
            freshness_hooks=(
                *default_freshness_hooks(constraints),
                action_evidence_hook(constraints, self.rules),
            ),
        )
        validated = replace(
            optimization,
            total_pre_revenue_cash=validation.total_pre_revenue_cash,
            total_focus=validation.total_focus,
            total_expected_profit=validation.total_expected_profit,
            silver_remaining=validation.silver_remaining,
            focus_remaining=validation.focus_remaining,
            plan_status=validation.status,
            reasons=validation.reasons,
        )
        current_stats = _current_statistics(preflight, current_result)
        history_stats = (
            history_result.statistics
            if history_result is not None
            else RefreshStatistics(keys_required=len(history_keys))
        )
        health = _data_health(validated, constraints, self.rules, completed_at)
        catalog_version = (
            active_preflight.catalog.source_version
            if active_preflight.catalog is not None
            else "missing-catalog"
        )
        metadata = tuple(
            sorted(
                (
                    ("candidate_recipes", str(active_preflight.summary.candidate_recipes)),
                    ("arbitrage_items", str(active_preflight.summary.arbitrage_items)),
                    ("eligible_recipe_routes", str(len(active_preflight.eligible))),
                    ("eligible_arbitrage_routes", str(len(active_preflight.arbitrage_routes))),
                    ("initial_scenarios", str(initial.scenarios_evaluated)),
                    ("shortlist_capacity_groups", str(shortlist.capacity_groups_selected)),
                    ("history_groups_not_enriched", str(not_shortlisted)),
                    (
                        "prehistory_routes_pruned_for_ranking",
                        str(prehistory_pruning.dominated_count),
                    ),
                    ("candidate_routes_before_pruning", str(final_pruning.routes_before)),
                    ("candidate_routes_after_pruning", str(final_pruning.routes_after)),
                    (
                        "candidate_local_modes_removed",
                        str(final_pruning.local_modes_removed),
                    ),
                    (
                        "equivalent_routes_collapsed",
                        str(final_pruning.equivalent_routes_collapsed),
                    ),
                    ("final_candidates", str(len(final_evaluation.candidates))),
                    ("optimizer_candidates", str(len(optimizer_candidates))),
                    (
                        "rejection_counts",
                        json.dumps(dict(sorted(rejections.items())), sort_keys=True),
                    ),
                    (
                        "automatic_price_history_keys_requested",
                        str(
                            current_result.history_keys_requested
                            if current_result is not None
                            else 0
                        ),
                    ),
                    (
                        "automatic_price_history_estimates_available",
                        str(
                            current_result.historical_estimates_available
                            if current_result is not None
                            else 0
                        ),
                    ),
                    (
                        "automatic_price_history_keys_unresolved",
                        str(
                            current_result.history_keys_unresolved
                            if current_result is not None
                            else 0
                        ),
                    ),
                    (
                        "current_refresh_circuit_breaker_open",
                        str(
                            current_result.circuit_breaker_open
                            if current_result is not None
                            else False
                        ).lower(),
                    ),
                    (
                        "current_refresh_groups_skipped",
                        str(current_result.groups_skipped if current_result is not None else 0),
                    ),
                    (
                        "history_backfill_circuit_breaker_open",
                        str(
                            current_result.history_circuit_breaker_open
                            if current_result is not None
                            else False
                        ).lower(),
                    ),
                    (
                        "pipeline_elapsed_seconds",
                        f"{time.perf_counter() - started_wall:.6f}",
                    ),
                )
            )
        )
        snapshot = PlanSnapshot.from_optimization(
            snapshot_id=self._identifier_factory(started_at),
            created_at=started_at,
            completed_at=completed_at,
            constraints=constraints,
            result=validated,
            catalog_source_version=catalog_version,
            mechanics_ruleset_id=self.rules.ruleset_id,
            assumptions=default_plan_assumptions(constraints),
            data_health=health,
            current_refresh=current_stats,
            history_refresh=history_stats,
            metadata=metadata,
        )
        if self.snapshots is not None:
            self._report(
                progress,
                PlanningStage.PERSISTENCE,
                "Persisting an immutable recent-plan snapshot...",
            )
            self.snapshots.save(snapshot)
        self._report(
            progress,
            PlanningStage.COMPLETE,
            f"Plan complete with {len(snapshot.actions):,} actions and "
            f"{snapshot.total_expected_profit:,} expected silver profit.",
            len(snapshot.actions),
            len(snapshot.actions),
        )
        return FindMoneyRunResult(
            started_at,
            completed_at,
            active_preflight,
            initial,
            shortlist,
            final_evaluation,
            tuple(sorted(liquidity.items(), key=lambda value: _capacity_order(value[0]))),
            tuple(sorted(ceilings.items(), key=lambda value: _capacity_order(value[0]))),
            validated,
            validation,
            snapshot,
            current_result,
            history_result,
            tuple(sorted(rejections.items())),
        )

    def _load_current_rows(
        self,
        preflight: FindMoneyPreflight,
    ) -> tuple[list[MarketPrice], list[UserPriceOverride]]:
        keys = {assessment.requirement.key for assessment in preflight.market_refresh.assessments}
        if not keys:
            return [], []
        item_ids = tuple(sorted({value.item_id for value in keys}))
        cities = tuple(sorted({value.city for value in keys}, key=str.casefold))
        qualities = tuple(sorted({value.quality for value in keys}))
        constraints = preflight.constraints
        return (
            self.market_prices.list_for_scan(
                constraints.region,
                cities=cities,
                qualities=qualities,
                item_ids=item_ids,
            ),
            self.overrides.list_for_scan(
                constraints.region,
                cities=cities,
                qualities=qualities,
                item_ids=item_ids,
            ),
        )

    def _load_price_history(
        self,
        preflight: FindMoneyPreflight,
        as_of: datetime,
    ) -> list[MarketHistoryInterval]:
        sell_keys = {
            assessment.requirement.key
            for assessment in preflight.market_refresh.assessments
            if assessment.requirement.required_for_actionability
            and assessment.requirement.side is MarketSide.SELL_ORDER
        }
        if not sell_keys:
            return []
        rows: list[MarketHistoryInterval] = []
        for quality in sorted({key.quality for key in sell_keys}):
            quality_keys = tuple(key for key in sell_keys if key.quality == quality)
            rows.extend(
                self.history.list_for_items(
                    preflight.constraints.region,
                    tuple(sorted({key.item_id for key in quality_keys})),
                    tuple(sorted({key.city for key in quality_keys}, key=str.casefold)),
                    quality,
                    as_of - DEFAULT_HISTORICAL_ESTIMATION_POLICY.volume_lookback,
                    time_scale=HistoryTimeScale.DAILY,
                )
            )
        return rows

    def _evaluate_candidates(
        self,
        production_routes,
        arbitrage_routes,
        price_rows,
        override_rows,
        price_history_rows,
        profile: CraftingSkillProfile,
        constraints: FindMoneyConstraints,
        *,
        as_of: datetime,
        liquidity_by_key: Mapping[LiquidityKey, LiquidityAssessment] | None = None,
        cancelled: CancellationCheck | None = None,
        progress: ProgressCallback | None = None,
        stage: PlanningStage,
    ) -> CandidateEvaluationResult:
        """Evaluate both action families against one immutable evidence timestamp."""

        production_routes = tuple(production_routes)
        arbitrage_routes = tuple(arbitrage_routes)
        total = len(production_routes) + len(arbitrage_routes)

        def report(completed: int, offset: int) -> None:
            self._report(
                progress,
                stage,
                f"Evaluated {completed + offset:,} of {total:,} routes.",
                completed + offset,
                total,
            )

        production = self.evaluator.evaluate(
            production_routes,
            price_rows,
            override_rows,
            profile,
            constraints,
            history=price_history_rows,
            liquidity_by_key=liquidity_by_key,
            as_of=as_of,
            cancelled=cancelled,
            progress=(None if progress is None else lambda done, _: report(done, 0)),
        )
        if production.cancelled:
            return production
        arbitrage = self.arbitrage_evaluator.evaluate(
            arbitrage_routes,
            price_rows,
            override_rows,
            constraints,
            history=price_history_rows,
            liquidity_by_key=liquidity_by_key,
            as_of=as_of,
            cancelled=cancelled,
            progress=(
                None if progress is None else lambda done, _: report(done, len(production_routes))
            ),
        )
        return _merge_evaluations(production, arbitrage)

    def _refresh_output_history(
        self,
        keys: tuple[LiquidityKey, ...],
        *,
        as_of: datetime,
        cancelled: CancellationCheck | None,
        progress: ProgressCallback | None,
    ) -> HistoryRefreshAggregate:
        assert self.history_refresh is not None
        if self.history_refresh.client.region is not keys[0][0]:
            raise ValueError("history refresh client region does not match the plan")
        grouped: dict[str, list[str]] = defaultdict(list)
        for _, item_id, city, _ in keys:
            grouped[city].append(item_id)
        outcomes: list[CachedHistoryRefreshResult] = []
        started = time.perf_counter()
        groups = sorted(grouped.items(), key=lambda value: value[0].casefold())
        for position, (city, item_ids) in enumerate(groups, start=1):
            if self._cancelled(cancelled):
                break
            outcome = self.history_refresh.refresh_outputs(
                tuple(sorted(set(item_ids))),
                start_date=(as_of - timedelta(days=7)).date(),
                end_date=as_of.date(),
                sell_cities=(city,),
                qualities=(1,),
                time_scale=HistoryTimeScale.SIX_HOURLY,
                is_cancelled=cancelled,
            )
            outcomes.append(outcome)
            self._report(
                progress,
                PlanningStage.HISTORY_REFRESH,
                f"History groups: {position:,} of {len(groups):,} complete.",
                position,
                len(groups),
            )
            if outcome.cancelled:
                break
        fetches = [value.fetch for value in outcomes]
        return HistoryRefreshAggregate(
            keys,
            tuple(outcomes),
            sum(value.batch_count for value in fetches),
            sum(value.completed_batches for value in fetches),
            sum(value.failed_batches for value in fetches),
            sum(value.records_returned for value in fetches),
            sum(value.request_attempts for value in fetches),
            sum(value.retry_count for value in fetches),
            max(time.perf_counter() - started, 0.0),
            self._cancelled(cancelled) or any(value.cancelled for value in outcomes),
        )

    def _load_history(
        self,
        keys: Sequence[LiquidityKey],
        as_of: datetime,
    ) -> tuple[
        dict[LiquidityKey, tuple[MarketHistoryInterval, ...]],
        dict[LiquidityKey, str],
    ]:
        if not keys:
            return {}, {}
        region = keys[0][0]
        item_ids = tuple(sorted({value[1] for value in keys}))
        cities = tuple(sorted({value[2] for value in keys}, key=str.casefold))
        intervals = self.history.list_for_outputs(
            region,
            item_ids,
            cities,
            1,
            as_of - timedelta(days=7),
            time_scale=HistoryTimeScale.SIX_HOURLY,
        )
        coverage = self.history.list_coverage(
            region,
            item_ids,
            cities,
            1,
            HistoryTimeScale.SIX_HOURLY,
        )
        grouped: dict[LiquidityKey, list[MarketHistoryInterval]] = defaultdict(list)
        for value in intervals:
            grouped[(value.region, value.item_id, value.city, value.quality)].append(value)
        statuses = {
            (value.region, value.item_id, value.city, value.quality): value.status
            for value in coverage
        }
        return (
            {
                key: tuple(sorted(values, key=lambda value: value.observed_at))
                for key, values in grouped.items()
            },
            statuses,
        )

    @staticmethod
    def _build_ceilings(
        candidates,
        intervals_by_key: Mapping[LiquidityKey, tuple[MarketHistoryInterval, ...]],
        status_by_key: Mapping[LiquidityKey, str],
        constraints: FindMoneyConstraints,
        *,
        as_of: datetime,
    ) -> dict[ExecutionCapacityKey, QuantityCeiling]:
        keys = {
            requirement.key
            for candidate in candidates
            for requirement in candidate.capacity_requirements
        }
        result: dict[ExecutionCapacityKey, QuantityCeiling] = {}
        window_start = as_of - timedelta(hours=24)
        for key in keys:
            reliable = status_by_key.get(key) == "success"
            intervals = intervals_by_key.get(key, ())
            reported_volume = (
                sum(
                    value.item_count
                    for value in intervals
                    if window_start <= value.observed_at
                    and future_offset_beyond_tolerance(value.observed_at, now=as_of) is None
                )
                if reliable
                else None
            )
            result[key] = calculate_quantity_ceiling(
                key,
                explicit_craft_cap=constraints.per_item_craft_cap,
                history_enabled=constraints.history_enabled,
                reported_24h_volume=reported_volume,
                historical_volume_share=constraints.historical_volume_share,
            )
        return result

    def _cancelled_result(
        self,
        started_at: datetime,
        preflight: FindMoneyPreflight,
        rejections: Counter[str],
        *,
        initial: CandidateEvaluationResult | None = None,
        shortlist: CandidateShortlist | None = None,
        final: CandidateEvaluationResult | None = None,
        liquidity: Mapping[LiquidityKey, LiquidityAssessment] | None = None,
        ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling] | None = None,
        current_refresh: CurrentRefreshResult | None = None,
        history_refresh: HistoryRefreshAggregate | None = None,
    ) -> FindMoneyRunResult:
        completed_at = self._now()
        rejections[PlanReasonCode.CANCELLED.value] += 1
        return FindMoneyRunResult(
            started_at,
            completed_at,
            preflight,
            initial,
            shortlist,
            final,
            tuple(sorted((liquidity or {}).items(), key=lambda value: _capacity_order(value[0]))),
            tuple(sorted((ceilings or {}).items(), key=lambda value: _capacity_order(value[0]))),
            None,
            None,
            None,
            current_refresh,
            history_refresh,
            tuple(sorted(rejections.items())),
            True,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("planning clock must return timezone-aware datetimes")
        return value.astimezone(UTC)

    @staticmethod
    def _cancelled(cancelled: CancellationCheck | None) -> bool:
        return cancelled is not None and cancelled()

    @staticmethod
    def _report(
        callback: ProgressCallback | None,
        stage: PlanningStage,
        message: str,
        completed: int = 0,
        total: int | None = None,
    ) -> None:
        if callback is not None:
            callback(PlanningProgress(stage, message, completed, total))


def _output_prices(preflight: FindMoneyPreflight) -> dict[LiquidityKey, float]:
    result: dict[LiquidityKey, float] = {}
    for value in preflight.market_refresh.assessments:
        requirement = value.requirement
        if (
            requirement.role
            in {
                PriceRole.OUTPUT,
                PriceRole.ARBITRAGE_SOURCE,
                PriceRole.ARBITRAGE_DESTINATION,
            }
            and value.price is not None
        ):
            key = requirement.key
            result[(key.region, key.item_id, key.city, key.quality)] = value.price
    return result


def _merge_evaluations(
    left: CandidateEvaluationResult,
    right: CandidateEvaluationResult,
) -> CandidateEvaluationResult:
    rejections = Counter(dict(left.rejection_counts))
    rejections.update(dict(right.rejection_counts))
    return CandidateEvaluationResult(
        tuple(sorted((*left.candidates, *right.candidates), key=lambda value: value.canonical_key)),
        tuple(
            sorted(
                (*left.near_misses, *right.near_misses),
                key=lambda value: (value.action_kind.value, value.candidate_id),
            )
        ),
        tuple(sorted(rejections.items())),
        left.scenarios_evaluated + right.scenarios_evaluated,
        left.elapsed_seconds + right.elapsed_seconds,
        left.cancelled or right.cancelled,
    )


def _candidate_filter_rejections(
    candidates,
    constraints: FindMoneyConstraints,
    *,
    include_liquidity: bool = True,
) -> Counter[str]:
    result: Counter[str] = Counter()
    for candidate in candidates:
        if candidate.economics.pre_revenue_cash_per_craft > constraints.silver_budget:
            result["single_craft_exceeds_silver_budget"] += 1
        if not _candidate_has_acceptable_mode(candidate, constraints):
            result["below_profit_or_roi"] += 1
        if (
            include_liquidity
            and candidate.liquidity_rank < constraints.minimum_liquidity.minimum_rank
        ):
            result["below_minimum_liquidity"] += 1
    return result


def _candidate_is_preoptimizer_eligible(candidate, constraints: FindMoneyConstraints) -> bool:
    return (
        not candidate.has_blocker
        and candidate.economics.pre_revenue_cash_per_craft <= constraints.silver_budget
        and _candidate_has_acceptable_mode(candidate, constraints)
    )


def _candidate_has_acceptable_mode(candidate, constraints: FindMoneyConstraints) -> bool:
    economics = candidate.economics

    def passes(profit: int | None, roi: float | None) -> bool:
        if profit is None or profit <= 0:
            return False
        if constraints.minimum_profit is not None and profit < constraints.minimum_profit:
            return False
        return constraints.minimum_roi is None or (
            roi is not None and roi >= constraints.minimum_roi
        )

    nonfocused = economics.nonfocused_eligible and passes(
        economics.nonfocused_profit_per_craft,
        candidate.nonfocused_roi,
    )
    focused = (
        constraints.use_focus
        and economics.has_focused_variant
        and (economics.focus_per_focused_craft or 0) <= constraints.focus_budget
        and passes(economics.focused_profit_per_craft, candidate.focused_roi)
    )
    return nonfocused or focused


def _current_statistics(
    preflight: FindMoneyPreflight,
    result: CurrentRefreshResult | None,
) -> RefreshStatistics:
    if result is not None:
        return result.statistics
    return RefreshStatistics(
        keys_required=len(preflight.market_refresh.refresh_keys),
        batches_planned=preflight.market_refresh.estimated_batches,
    )


def _data_health(
    result: OptimizationResult,
    constraints: FindMoneyConstraints,
    rules: MechanicsRules,
    as_of: datetime,
) -> PlanDataHealth:
    market_used = 0
    market_fresh = 0
    market_stale = 0
    overrides = 0
    market_policy = FreshnessPolicy(constraints.max_market_age)
    for action in result.actions:
        try:
            lines = json.loads(dict(action.evidence).get("prices", "[]"))
        except (json.JSONDecodeError, TypeError):
            lines = []
        for line in lines:
            if not isinstance(line, dict) or line.get("role") not in {
                "material",
                "output",
                "arbitrage_source",
                "arbitrage_destination",
            }:
                continue
            market_used += 1
            if line.get("provenance") == "user_override":
                overrides += 1
            raw_observed_at = line.get("observed_at")
            try:
                observed_at = (
                    datetime.fromisoformat(raw_observed_at)
                    if isinstance(raw_observed_at, str)
                    else None
                )
            except ValueError:
                observed_at = None
            freshness = market_policy.classify(observed_at, now=as_of)
            if freshness in {Freshness.FRESH, Freshness.AGING}:
                market_fresh += 1
            else:
                market_stale += 1
    production_actions = tuple(
        action for action in result.actions if action.action_kind is not ActionKind.ARBITRAGE
    )
    station_used = len(production_actions)
    station_policy = FreshnessPolicy(constraints.max_station_fee_age)
    station_stale = sum(
        station_policy.classify(action.station_fee_observed_at, now=as_of)
        not in {Freshness.FRESH, Freshness.AGING}
        for action in production_actions
    )
    station_fresh = station_used - station_stale
    mechanics = rules.verification_health(as_of=as_of)
    mechanics_status = rules.verification_status.value
    if mechanics.is_aging:
        mechanics_status += "_aging"
    return PlanDataHealth(
        market_observations_used=market_used,
        market_fresh=market_fresh,
        market_stale=market_stale,
        user_overrides_used=overrides,
        station_fees_used=station_used,
        station_fees_fresh=station_fresh,
        station_fees_stale=station_stale,
        mechanics_status=mechanics_status,
    )


def _snapshot_identifier(created_at: datetime) -> str:
    return f"plan-{created_at.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:12]}"


def _capacity_order(key: ExecutionCapacityKey) -> tuple[str, str, str, int]:
    return (key[0].value, key[1], key[2].casefold(), key[3])
