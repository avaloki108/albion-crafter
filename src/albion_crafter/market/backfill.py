from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from albion_crafter.database.database import MarketPriceRepository
from albion_crafter.database.v3 import MarketHistoryRepository

from .aodp import CancellationCheck
from .estimation import (
    DEFAULT_HISTORICAL_ESTIMATION_POLICY,
    HistoricalEstimationPolicy,
    estimate_historical_sell_price,
)
from .history import AODPHistoryClient, HistoryBatchProgress, HistoryTimeScale
from .history_cache import CachedHistoryRefreshResult, CachedOutputHistoryService
from .models import Region

HistoryClientFactory = Callable[[Region], AODPHistoryClient]
HistoryCacheServiceFactory = Callable[
    [AODPHistoryClient, MarketHistoryRepository], CachedOutputHistoryService
]
MissingSellKey = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class MissingSellHistoryProgress:
    city: str
    city_number: int
    city_count: int
    batch_number: int
    batch_count: int
    item_ids: tuple[str, ...]
    records_returned: int
    successful: bool


@dataclass(frozen=True, slots=True)
class MissingSellHistoryBackfillResult:
    requested_keys: tuple[MissingSellKey, ...]
    resolved_keys: tuple[MissingSellKey, ...]
    unresolved_keys: tuple[MissingSellKey, ...]
    refreshes: tuple[CachedHistoryRefreshResult, ...]
    cancelled: bool = False
    circuit_breaker_open: bool = False

    @property
    def requested_count(self) -> int:
        return len(self.requested_keys)

    @property
    def resolved_count(self) -> int:
        return len(self.resolved_keys)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved_keys)

    @property
    def batches_planned(self) -> int:
        return sum(value.fetch.batch_count for value in self.refreshes)

    @property
    def batches_completed(self) -> int:
        return sum(value.fetch.completed_batches for value in self.refreshes)

    @property
    def batches_succeeded(self) -> int:
        return sum(value.fetch.successful_batches for value in self.refreshes)

    @property
    def batches_failed(self) -> int:
        return sum(value.fetch.failed_batches for value in self.refreshes)

    @property
    def record_failures(self) -> int:
        return sum(len(value.fetch.record_failures) for value in self.refreshes)

    @property
    def has_errors(self) -> bool:
        return bool(self.batches_failed or self.record_failures)


BackfillProgressCallback = Callable[[MissingSellHistoryProgress], None]


class MissingSellHistoryBackfillService:
    """Fetch daily SELL history only where no current SELL order is available.

    AODP history is never copied into the raw current-price table. This service
    only fills the separate history cache so the shared resolver can produce a
    labeled ``HISTORICAL_ESTIMATE``.
    """

    def __init__(
        self,
        market: MarketPriceRepository,
        history: MarketHistoryRepository,
        *,
        client_factory: HistoryClientFactory = AODPHistoryClient,
        cache_service_factory: HistoryCacheServiceFactory = CachedOutputHistoryService,
        policy: HistoricalEstimationPolicy = DEFAULT_HISTORICAL_ESTIMATION_POLICY,
    ) -> None:
        self.market = market
        self.history = history
        self.client_factory = client_factory
        self.cache_service_factory = cache_service_factory
        self.policy = policy

    def refresh_missing(
        self,
        region: Region,
        item_ids: Sequence[str],
        cities: Sequence[str],
        *,
        quality: int = 1,
        as_of: datetime | None = None,
        is_cancelled: CancellationCheck | None = None,
        on_progress: BackfillProgressCallback | None = None,
    ) -> MissingSellHistoryBackfillResult:
        resolution_time = (as_of or datetime.now(UTC)).astimezone(UTC)
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("history backfill as_of must be timezone-aware")
        if not 1 <= quality <= 5:
            raise ValueError("history backfill quality must be between 1 and 5")
        selected_ids = _unique_values(item_ids)
        selected_cities = _unique_values(cities)
        if not selected_ids or not selected_cities:
            return MissingSellHistoryBackfillResult((), (), ())

        missing_by_city = self._missing_current_sell_ids(
            region, selected_ids, selected_cities, quality
        )
        requested = tuple(
            (item_id, city, quality)
            for city in selected_cities
            for item_id in missing_by_city[city]
        )
        if not requested:
            return MissingSellHistoryBackfillResult((), (), ())

        refreshes: list[CachedHistoryRefreshResult] = []
        cancelled = False
        circuit_breaker_open = False
        active_cities = tuple(city for city in selected_cities if missing_by_city[city])
        for city_number, city in enumerate(active_cities, start=1):
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            client = self.client_factory(region)
            if client.region is not region:
                raise ValueError("history backfill client factory returned the wrong region")
            service = self.cache_service_factory(client, self.history)
            start_date = (resolution_time - self.policy.volume_lookback).date()
            end_date = resolution_time.date()
            planned_batches = client.plan_history_batches(
                missing_by_city[city],
                start_date=start_date,
                end_date=end_date,
                cities=(city,),
                qualities=(quality,),
                time_scale=HistoryTimeScale.DAILY,
            )

            # ``max_batches`` is a cap for one client operation, not a reason to
            # reject an explicitly selected larger backfill. Execute consecutive
            # capped operations while preserving the exact safe URL boundaries.
            for batch_offset in range(0, len(planned_batches), client.max_batches):
                if is_cancelled is not None and is_cancelled():
                    cancelled = True
                    break
                operation_batches = planned_batches[
                    batch_offset : batch_offset + client.max_batches
                ]
                operation_item_ids = tuple(
                    item_id for batch in operation_batches for item_id in batch
                )

                def report(
                    progress: HistoryBatchProgress,
                    *,
                    selected_city: str = city,
                    selected_city_number: int = city_number,
                    completed_before: int = batch_offset,
                    city_batch_count: int = len(planned_batches),
                ) -> None:
                    if on_progress is not None:
                        on_progress(
                            MissingSellHistoryProgress(
                                city=selected_city,
                                city_number=selected_city_number,
                                city_count=len(active_cities),
                                batch_number=completed_before + progress.batch_number,
                                batch_count=city_batch_count,
                                item_ids=progress.item_ids,
                                records_returned=progress.records_returned,
                                successful=progress.successful,
                            )
                        )

                refresh = service.refresh_market_items(
                    operation_item_ids,
                    start_date=start_date,
                    end_date=end_date,
                    cities=(city,),
                    qualities=(quality,),
                    time_scale=HistoryTimeScale.DAILY,
                    is_cancelled=is_cancelled,
                    on_progress=report,
                )
                refreshes.append(refresh)
                if refresh.cancelled:
                    cancelled = True
                    break
                if refresh.fetch.circuit_breaker_open:
                    circuit_breaker_open = True
                    break
            if cancelled or circuit_breaker_open:
                break

        resolved = self._resolved_history_keys(region, requested, resolution_time)
        resolved_set = {
            (item.casefold(), _city_key(city), value_quality)
            for item, city, value_quality in resolved
        }
        unresolved = tuple(
            key
            for key in requested
            if (key[0].casefold(), _city_key(key[1]), key[2]) not in resolved_set
        )
        return MissingSellHistoryBackfillResult(
            requested,
            resolved,
            unresolved,
            tuple(refreshes),
            cancelled,
            circuit_breaker_open,
        )

    def _missing_current_sell_ids(
        self,
        region: Region,
        item_ids: tuple[str, ...],
        cities: tuple[str, ...],
        quality: int,
    ) -> dict[str, tuple[str, ...]]:
        rows = self.market.list_for_scan(
            region,
            cities=cities,
            qualities=(quality,),
            item_ids=item_ids,
        )
        available = {
            (row.item_id.casefold(), _city_key(row.city))
            for row in rows
            if row.sell_price is not None and row.sell_price > 0
        }
        return {
            city: tuple(
                item_id
                for item_id in item_ids
                if (item_id.casefold(), _city_key(city)) not in available
            )
            for city in cities
        }

    def _resolved_history_keys(
        self,
        region: Region,
        requested: tuple[MissingSellKey, ...],
        as_of: datetime,
    ) -> tuple[MissingSellKey, ...]:
        by_city: dict[tuple[str, int], list[str]] = defaultdict(list)
        canonical: dict[tuple[str, str, int], MissingSellKey] = {}
        for item_id, city, quality in requested:
            by_city[(city, quality)].append(item_id)
            canonical[(item_id.casefold(), _city_key(city), quality)] = (item_id, city, quality)

        resolved: list[MissingSellKey] = []
        for (city, quality), item_ids in by_city.items():
            intervals = self.history.list_for_items(
                region,
                tuple(item_ids),
                (city,),
                quality,
                as_of - self.policy.volume_lookback,
                time_scale=HistoryTimeScale.DAILY,
            )
            series: dict[tuple[str, str, int], list] = defaultdict(list)
            for interval in intervals:
                series[
                    (interval.item_id.casefold(), _city_key(interval.city), interval.quality)
                ].append(interval)
            for key, values in series.items():
                if (
                    estimate_historical_sell_price(values, as_of=as_of, policy=self.policy)
                    is not None
                ):
                    resolved.append(canonical[key])
        resolved.sort(key=lambda value: (_city_key(value[1]), value[0].casefold(), value[2]))
        return tuple(resolved)


def _unique_values(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("history backfill values must be non-empty strings")
        value = raw.strip()
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return tuple(result)


def _city_key(value: str) -> str:
    return value.replace(" ", "").replace("'", "").casefold()
