from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from .aodp import (
    CancellationCheck,
    WallClock,
    _deduplicate_item_ids,
    _normalize_city,
    _utcnow,
)
from .history import (
    AODPHistoryClient,
    HistoryFetchResult,
    HistoryProgressCallback,
    HistoryTimeScale,
    MarketHistoryInterval,
)
from .models import Region

DEFAULT_OUTPUT_HISTORY_RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class HistoryCoverageUpdate:
    """Structural coverage record accepted by the SQLite history repository."""

    region: Region
    item_id: str
    city: str
    quality: int
    time_scale: HistoryTimeScale
    window_start: datetime
    window_end: datetime
    fetched_at: datetime
    status: str
    record_count: int
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.item_id or not self.city or not self.status:
            raise ValueError("history coverage identity and status are required")
        if not 1 <= self.quality <= 5 or self.record_count < 0:
            raise ValueError("history coverage quality/count is invalid")
        if any(
            value.tzinfo is None for value in (self.window_start, self.window_end, self.fetched_at)
        ):
            raise ValueError("history coverage timestamps must be timezone-aware")
        if self.window_end < self.window_start:
            raise ValueError("history coverage window end precedes its start")


class HistoryCacheRepository(Protocol):
    retention: timedelta

    def upsert_many(self, records: Iterable[MarketHistoryInterval]) -> None: ...

    def set_coverage(self, coverage: HistoryCoverageUpdate) -> None: ...

    def prune_before(self, cutoff: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class CachedHistoryRefreshResult:
    fetch: HistoryFetchResult
    coverage_total: int
    success_coverage: int
    empty_coverage: int
    partial_coverage: int
    failed_coverage: int
    cancelled_coverage: int
    pruned_intervals: int

    @property
    def cancelled(self) -> bool:
        return self.fetch.cancelled


class CachedOutputHistoryService:
    """Fetch and cache a caller-bounded set of AODP sell-history series."""

    def __init__(
        self,
        client: AODPHistoryClient,
        repository: HistoryCacheRepository,
        *,
        retention: timedelta | None = None,
        wall_clock: WallClock = _utcnow,
    ) -> None:
        selected_retention = (
            getattr(repository, "retention", DEFAULT_OUTPUT_HISTORY_RETENTION)
            if retention is None
            else retention
        )
        if selected_retention <= timedelta(0):
            raise ValueError("history retention must be positive")
        self.client = client
        self.repository = repository
        self.retention = selected_retention
        self._wall_clock = wall_clock

    def refresh_outputs(
        self,
        output_item_ids: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        sell_cities: Sequence[str],
        qualities: Sequence[int] = (1,),
        time_scale: HistoryTimeScale = HistoryTimeScale.SIX_HOURLY,
        is_cancelled: CancellationCheck | None = None,
        on_progress: HistoryProgressCallback | None = None,
    ) -> CachedHistoryRefreshResult:
        """Backward-compatible name for refreshing bounded output history."""

        return self.refresh_market_items(
            output_item_ids,
            start_date=start_date,
            end_date=end_date,
            cities=sell_cities,
            qualities=qualities,
            time_scale=time_scale,
            is_cancelled=is_cancelled,
            on_progress=on_progress,
        )

    def refresh_market_items(
        self,
        item_ids: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        cities: Sequence[str],
        qualities: Sequence[int] = (1,),
        time_scale: HistoryTimeScale = HistoryTimeScale.DAILY,
        is_cancelled: CancellationCheck | None = None,
        on_progress: HistoryProgressCallback | None = None,
    ) -> CachedHistoryRefreshResult:
        """Persist each successful batch, then record complete/partial coverage."""

        item_ids = _deduplicate_item_ids(item_ids)
        if any(not isinstance(city, str) or not city.strip() for city in cities):
            raise ValueError("history cities must contain non-empty strings")
        city_map: dict[str, str] = {}
        for city in cities:
            clean = city.strip()
            city_map.setdefault(_normalize_city(clean), clean)
        cities = tuple(city_map.values())
        quality_values = tuple(dict.fromkeys(qualities))
        result = self.client.fetch_history(
            item_ids,
            start_date=start_date,
            end_date=end_date,
            cities=cities,
            qualities=quality_values,
            time_scale=time_scale,
            is_cancelled=is_cancelled,
            on_batch_success=self.repository.upsert_many,
            on_progress=on_progress,
        )
        completed_at = self._now()
        statuses = self._store_coverage(
            result,
            item_ids=item_ids,
            cities=cities,
            qualities=quality_values,
            completed_at=completed_at,
        )
        pruned = self.repository.prune_before(completed_at - self.retention)
        return CachedHistoryRefreshResult(
            fetch=result,
            coverage_total=sum(statuses.values()),
            success_coverage=statuses["success"],
            empty_coverage=statuses["empty"],
            partial_coverage=statuses["partial"],
            failed_coverage=statuses["failed"],
            cancelled_coverage=statuses["cancelled"],
            pruned_intervals=pruned,
        )

    def _store_coverage(
        self,
        result: HistoryFetchResult,
        *,
        item_ids: tuple[str, ...],
        cities: tuple[str, ...],
        qualities: tuple[int, ...],
        completed_at: datetime,
    ) -> Counter[str]:
        retention_start = completed_at - self.retention
        record_counts: Counter[tuple[str, str, int]] = Counter(
            (
                interval.item_id.casefold(),
                _normalize_city(interval.city),
                interval.quality,
            )
            for interval in result.intervals
            if interval.observed_at >= retention_start
        )
        failed_messages: dict[str, list[str]] = {}
        for failure in result.failures:
            for item_id in failure.item_ids:
                failed_messages.setdefault(item_id.casefold(), []).append(failure.message)
        completed_ids = {item_id.casefold() for item_id in result.completed_item_ids}
        malformed_message = (
            f"{len(result.record_failures)} malformed history rows/series were skipped"
            if result.record_failures
            else None
        )

        requested_start = datetime.combine(result.start_date, time.min, tzinfo=UTC)
        requested_end = min(
            datetime.combine(result.end_date, time.max, tzinfo=UTC),
            completed_at,
        )
        window_start = min(max(requested_start, retention_start), requested_end)
        statuses: Counter[str] = Counter()
        for item_id in item_ids:
            item_key = item_id.casefold()
            for city in cities:
                city_key = _normalize_city(city)
                for quality in qualities:
                    count = record_counts[(item_key, city_key, quality)]
                    failures = failed_messages.get(item_key)
                    if failures:
                        status = "failed"
                        error_message = "; ".join(dict.fromkeys(failures))
                    elif item_key not in completed_ids:
                        status = "cancelled"
                        error_message = "History refresh was cancelled before this item completed"
                    elif malformed_message is not None:
                        status = "partial"
                        error_message = malformed_message
                    elif count:
                        status = "success"
                        error_message = None
                    else:
                        status = "empty"
                        error_message = None
                    self.repository.set_coverage(
                        HistoryCoverageUpdate(
                            region=self.client.region,
                            item_id=item_id,
                            city=city,
                            quality=quality,
                            time_scale=result.time_scale,
                            window_start=window_start,
                            window_end=requested_end,
                            fetched_at=completed_at,
                            status=status,
                            record_count=count,
                            error_message=error_message,
                        )
                    )
                    statuses[status] += 1
        return statuses

    def _now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            raise ValueError("wall_clock must return a timezone-aware datetime")
        return value.astimezone(UTC)
