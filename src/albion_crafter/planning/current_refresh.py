from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from albion_crafter.database.database import MarketPriceRepository
from albion_crafter.market.aodp import AODPClient, BatchFetchResult, CancellationCheck, Clock
from albion_crafter.market.backfill import (
    MissingSellHistoryBackfillResult,
    MissingSellHistoryBackfillService,
    MissingSellHistoryProgress,
)
from albion_crafter.market.cache import CachedMarketService
from albion_crafter.market.models import MarketSide, Region

from .models import MarketKey, RefreshStatistics
from .preflight import MarketRefreshPlan, PlannedAODPBatch

ClientFactory = Callable[[Region], AODPClient]
ServiceFactory = Callable[[AODPClient, MarketPriceRepository], CachedMarketService]
DEFAULT_MAX_CONSECUTIVE_GROUP_FAILURES = 2


@dataclass(frozen=True, slots=True)
class CurrentRefreshBatchOutcome:
    planned_batch_number: int
    planned_batch: PlannedAODPBatch
    result: BatchFetchResult


@dataclass(frozen=True, slots=True)
class CurrentRefreshFailure:
    planned_batch_number: int
    request_batch_number: int
    region: Region
    city: str
    quality: int
    item_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class CurrentRefreshProgress:
    groups_planned: int
    groups_completed: int
    batches_planned: int
    batches_completed: int
    batches_succeeded: int
    batches_failed: int
    request_attempts: int
    retry_count: int
    records_loaded: int
    record_failures: int
    current_batch: PlannedAODPBatch
    cancelled: bool = False


CurrentRefreshProgressCallback = Callable[[CurrentRefreshProgress], None]
HistoryBackfillProgressCallback = Callable[[MissingSellHistoryProgress], None]


@dataclass(frozen=True, slots=True)
class CurrentRefreshResult:
    requested_keys: tuple[MarketKey, ...]
    groups_planned: int
    groups_completed: int
    batches_planned: int
    batches_completed: int
    batches_succeeded: int
    batches_failed: int
    request_attempts: int
    retry_count: int
    records_loaded: int
    record_failures: int
    elapsed_seconds: float
    max_url_bytes: int
    cancelled: bool
    outcomes: tuple[CurrentRefreshBatchOutcome, ...]
    history_keys_requested: int = 0
    historical_estimates_available: int = 0
    history_keys_unresolved: int = 0
    history_batches_planned: int = 0
    history_batches_failed: int = 0
    history_record_failures: int = 0
    circuit_breaker_open: bool = False
    groups_skipped: int = 0
    history_circuit_breaker_open: bool = False

    @property
    def keys_requested(self) -> int:
        return len(self.requested_keys)

    @property
    def failures(self) -> tuple[CurrentRefreshFailure, ...]:
        failures: list[CurrentRefreshFailure] = []
        for outcome in self.outcomes:
            group = outcome.planned_batch
            failures.extend(
                CurrentRefreshFailure(
                    planned_batch_number=outcome.planned_batch_number,
                    request_batch_number=failure.batch_number,
                    region=group.region,
                    city=group.city,
                    quality=group.quality,
                    item_ids=failure.item_ids,
                    message=failure.message,
                )
                for failure in outcome.result.failures
            )
        return tuple(failures)

    @property
    def has_errors(self) -> bool:
        return bool(
            self.batches_failed
            or self.record_failures
            or self.history_batches_failed
            or self.history_record_failures
        )

    @property
    def is_partial(self) -> bool:
        return self.batches_succeeded > 0 and (self.has_errors or self.cancelled)

    @property
    def statistics(self) -> RefreshStatistics:
        return RefreshStatistics(
            keys_required=self.keys_requested,
            batches_planned=self.batches_planned,
            batches_completed=self.batches_completed,
            batches_failed=self.batches_failed,
            records_loaded=self.records_loaded,
            elapsed_seconds=self.elapsed_seconds,
        )


class CurrentMarketRefreshExecutor:
    """Execute the preflight refresh plan without widening its sparse key set."""

    def __init__(
        self,
        repository: MarketPriceRepository,
        *,
        client_factory: ClientFactory = AODPClient,
        service_factory: ServiceFactory = CachedMarketService,
        history_backfill: MissingSellHistoryBackfillService | None = None,
        max_consecutive_group_failures: int = DEFAULT_MAX_CONSECUTIVE_GROUP_FAILURES,
        clock: Clock = monotonic,
    ) -> None:
        if (
            isinstance(max_consecutive_group_failures, bool)
            or not isinstance(max_consecutive_group_failures, int)
            or max_consecutive_group_failures < 1
        ):
            raise ValueError("max_consecutive_group_failures must be a positive integer")
        self.repository = repository
        self.client_factory = client_factory
        self.service_factory = service_factory
        self.history_backfill = history_backfill
        self.max_consecutive_group_failures = max_consecutive_group_failures
        self._clock = clock

    def execute(
        self,
        plan: MarketRefreshPlan,
        *,
        is_cancelled: CancellationCheck | None = None,
        on_progress: CurrentRefreshProgressCallback | None = None,
        on_history_progress: HistoryBackfillProgressCallback | None = None,
    ) -> CurrentRefreshResult:
        requested_keys = plan.refresh_keys
        self._validate_sparse_batches(plan, requested_keys)
        clients: dict[Region, AODPClient] = {}
        services: dict[Region, CachedMarketService] = {}
        group_batch_counts: list[int] = []
        max_url_bytes = 0
        for group in plan.batches:
            client = clients.get(group.region)
            if client is None:
                client = self.client_factory(group.region)
                clients[group.region] = client
                services[group.region] = self.service_factory(client, self.repository)
            if client.region != group.region:
                raise ValueError("current refresh client factory returned the wrong region")
            request_plan = client.plan_price_requests(
                group.item_ids,
                cities=group.request_cities,
                qualities=(group.quality,),
            )
            group_batch_counts.append(request_plan.batch_count)
            max_url_bytes = max(max_url_bytes, request_plan.max_url_bytes)

        batches_planned = sum(group_batch_counts)
        started_at = self._clock()
        outcomes: list[CurrentRefreshBatchOutcome] = []
        groups_completed = 0
        batches_completed = 0
        batches_succeeded = 0
        batches_failed = 0
        request_attempts = 0
        retry_count = 0
        records_loaded = 0
        record_failures = 0
        cancelled = False
        circuit_breaker_open = False
        consecutive_group_failures = 0
        history_results: list[MissingSellHistoryBackfillResult] = []

        for planned_batch_number, group in enumerate(plan.batches, start=1):
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            result = services[group.region].refresh(
                group.item_ids,
                cities=group.request_cities,
                qualities=(group.quality,),
                is_cancelled=is_cancelled,
            )
            outcomes.append(CurrentRefreshBatchOutcome(planned_batch_number, group, result))
            if not result.cancelled:
                groups_completed += 1
            batches_completed += result.completed_batches
            batches_succeeded += result.successful_batches
            batches_failed += result.failed_batches
            request_attempts += result.request_attempts
            retry_count += result.retry_count
            records_loaded += result.records_returned
            record_failures += len(result.record_failures)
            cancelled = result.cancelled
            if result.successful_batches:
                consecutive_group_failures = 0
            elif result.failed_batches:
                consecutive_group_failures += 1
            if on_progress is not None:
                on_progress(
                    CurrentRefreshProgress(
                        groups_planned=len(plan.batches),
                        groups_completed=groups_completed,
                        batches_planned=batches_planned,
                        batches_completed=batches_completed,
                        batches_succeeded=batches_succeeded,
                        batches_failed=batches_failed,
                        request_attempts=request_attempts,
                        retry_count=retry_count,
                        records_loaded=records_loaded,
                        record_failures=record_failures,
                        current_batch=group,
                        cancelled=cancelled,
                    )
                )
            if cancelled:
                break
            if consecutive_group_failures >= self.max_consecutive_group_failures:
                circuit_breaker_open = True
                break

        if not cancelled and not circuit_breaker_open and self.history_backfill is not None:
            sell_groups: dict[tuple[Region, str, int], list[str]] = {}
            for assessment in plan.assessments:
                requirement = assessment.requirement
                if (
                    not requirement.required_for_actionability
                    or requirement.side is not MarketSide.SELL_ORDER
                ):
                    continue
                key = requirement.key
                sell_groups.setdefault((key.region, key.city, key.quality), []).append(key.item_id)
            for (region, city, quality), item_ids in sorted(
                sell_groups.items(),
                key=lambda value: (
                    value[0][0].value,
                    value[0][1].casefold(),
                    value[0][2],
                ),
            ):
                if is_cancelled is not None and is_cancelled():
                    cancelled = True
                    break
                history_result = self.history_backfill.refresh_missing(
                    region,
                    tuple(dict.fromkeys(item_ids)),
                    (city,),
                    quality=quality,
                    is_cancelled=is_cancelled,
                    on_progress=on_history_progress,
                )
                history_results.append(history_result)
                if history_result.cancelled:
                    cancelled = True
                    break
                if history_result.circuit_breaker_open:
                    break

        return CurrentRefreshResult(
            requested_keys=requested_keys,
            groups_planned=len(plan.batches),
            groups_completed=groups_completed,
            batches_planned=batches_planned,
            batches_completed=batches_completed,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            request_attempts=request_attempts,
            retry_count=retry_count,
            records_loaded=records_loaded,
            record_failures=record_failures,
            elapsed_seconds=max(self._clock() - started_at, 0.0),
            max_url_bytes=max_url_bytes,
            cancelled=cancelled,
            outcomes=tuple(outcomes),
            history_keys_requested=sum(value.requested_count for value in history_results),
            historical_estimates_available=sum(value.resolved_count for value in history_results),
            history_keys_unresolved=sum(value.unresolved_count for value in history_results),
            history_batches_planned=sum(value.batches_planned for value in history_results),
            history_batches_failed=sum(value.batches_failed for value in history_results),
            history_record_failures=sum(value.record_failures for value in history_results),
            circuit_breaker_open=circuit_breaker_open,
            groups_skipped=max(len(plan.batches) - groups_completed, 0),
            history_circuit_breaker_open=any(
                value.circuit_breaker_open for value in history_results
            ),
        )

    @staticmethod
    def _validate_sparse_batches(
        plan: MarketRefreshPlan,
        requested_keys: tuple[MarketKey, ...],
    ) -> None:
        expected = Counter(requested_keys)
        actual = Counter(
            MarketKey(group.region, item_id, city, group.quality)
            for group in plan.batches
            for city in group.request_cities
            for item_id in group.item_ids
        )
        if actual != expected:
            missing = sum((expected - actual).values())
            extra = sum((actual - expected).values())
            raise ValueError(
                "market refresh batches must cover each refresh key exactly once "
                f"(missing={missing}, extra_or_duplicate={extra})"
            )


def execute_current_market_refresh(
    plan: MarketRefreshPlan,
    repository: MarketPriceRepository,
    *,
    client_factory: ClientFactory = AODPClient,
    service_factory: ServiceFactory = CachedMarketService,
    clock: Clock = monotonic,
    is_cancelled: CancellationCheck | None = None,
    on_progress: CurrentRefreshProgressCallback | None = None,
) -> CurrentRefreshResult:
    return CurrentMarketRefreshExecutor(
        repository,
        client_factory=client_factory,
        service_factory=service_factory,
        clock=clock,
    ).execute(plan, is_cancelled=is_cancelled, on_progress=on_progress)
