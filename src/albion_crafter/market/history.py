from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import IntEnum
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from albion_crafter.core.freshness import future_offset_beyond_tolerance
from albion_crafter.core.provenance import Provenance

from .aodp import (
    DEFAULT_MAX_BATCHES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    SAFE_AODP_URL_LENGTH,
    BatchFailure,
    CancellationCheck,
    Clock,
    MarketDataError,
    MarketTransportError,
    Sleeper,
    Transport,
    WallClock,
    _deduplicate_item_ids,
    _default_transport,
    _normalize_city,
    _parse_timestamp,
    _transport_failure,
    _url_length,
    _utcnow,
    _validate_qualities,
)
from .models import Region


class HistoryDataError(MarketDataError):
    """A recoverable failure while requesting or parsing AODP history data."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class HistoryTimeScale(IntEnum):
    HOURLY = 1
    SIX_HOURLY = 6
    DAILY = 24


@dataclass(frozen=True, slots=True)
class MarketHistoryInterval:
    item_id: str
    city: str
    quality: int
    region: Region
    observed_at: datetime
    item_count: int
    average_price: float
    time_scale: HistoryTimeScale
    fetched_at: datetime
    provenance: Provenance = Provenance.AODP_LIVE

    def __post_init__(self) -> None:
        if not self.item_id or not self.city:
            raise ValueError("item_id and city are required")
        if (
            isinstance(self.quality, bool)
            or not isinstance(self.quality, int)
            or not 1 <= self.quality <= 5
        ):
            raise ValueError("quality must be between 1 and 5")
        if (
            isinstance(self.item_count, bool)
            or not isinstance(self.item_count, int)
            or self.item_count <= 0
        ):
            raise ValueError("item_count must be positive")
        if (
            isinstance(self.average_price, bool)
            or not isinstance(self.average_price, (int, float))
            or not math.isfinite(self.average_price)
            or self.average_price <= 0
        ):
            raise ValueError("average_price must be positive")
        if not isinstance(self.time_scale, HistoryTimeScale):
            raise ValueError("time_scale must be a HistoryTimeScale")
        if self.observed_at.tzinfo is None or self.fetched_at.tzinfo is None:
            raise ValueError("history timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HistoryRecordFailure:
    batch_number: int
    series_number: int
    point_number: int | None
    message: str


@dataclass(frozen=True, slots=True)
class HistoryFetchResult:
    intervals: tuple[MarketHistoryInterval, ...]
    failures: tuple[BatchFailure, ...]
    record_failures: tuple[HistoryRecordFailure, ...]
    batch_count: int
    items_requested: int
    successful_batches: int
    elapsed_seconds: float
    start_date: date
    end_date: date
    time_scale: HistoryTimeScale
    request_attempts: int = 0
    retry_count: int = 0
    completed_batches: int = 0
    cancelled: bool = False
    max_url_bytes: int = 0
    completed_item_ids: tuple[str, ...] = ()

    @property
    def is_partial(self) -> bool:
        return self.successful_batches > 0 and (self.has_errors or self.cancelled)

    @property
    def has_errors(self) -> bool:
        return bool(self.failures or self.record_failures)

    @property
    def failed_batches(self) -> int:
        return len(self.failures)

    @property
    def http_batches(self) -> int:
        return self.batch_count

    @property
    def records_returned(self) -> int:
        return len(self.intervals)

    @property
    def http_attempts(self) -> int:
        return self.request_attempts


@dataclass(frozen=True, slots=True)
class HistoryBatchProgress:
    batch_number: int
    batch_count: int
    item_ids: tuple[str, ...]
    successful: bool
    records_returned: int
    request_attempts: int
    retry_count: int
    failure: BatchFailure | None = None
    record_failures: tuple[HistoryRecordFailure, ...] = ()

    @property
    def completed_batches(self) -> int:
        return self.batch_number


HistoryBatchSuccessCallback = Callable[[tuple[MarketHistoryInterval, ...]], None]
HistoryProgressCallback = Callable[[HistoryBatchProgress], None]


class AODPHistoryClient:
    """Bounded client for AODP's sell-history endpoint.

    AODP history is reported activity, not order-book depth. Empty responses are preserved as
    missing history rather than converted to zero volume.
    """

    def __init__(
        self,
        region: Region = Region.AMERICAS,
        *,
        timeout: float = 10.0,
        batch_size: int = 100,
        max_url_length: int = SAFE_AODP_URL_LENGTH,
        max_batches: int = DEFAULT_MAX_BATCHES,
        max_date_span_days: int = 31,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        transport: Transport | None = None,
        clock: Clock = monotonic,
        sleeper: Sleeper = sleep,
        wall_clock: WallClock = _utcnow,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not 512 <= max_url_length <= SAFE_AODP_URL_LENGTH:
            raise ValueError(f"max_url_length must be between 512 and {SAFE_AODP_URL_LENGTH}")
        if max_batches < 1:
            raise ValueError("max_batches must be positive")
        if max_date_span_days < 1:
            raise ValueError("max_date_span_days must be positive")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be finite and non-negative")
        self.region = region
        self.timeout = timeout
        self.batch_size = batch_size
        self.max_url_length = max_url_length
        self.max_batches = max_batches
        self.max_date_span_days = max_date_span_days
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._transport = transport or _default_transport
        self._clock = clock
        self._sleeper = sleeper
        self._wall_clock = wall_clock

    def build_history_url(
        self,
        item_ids: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        cities: Sequence[str],
        qualities: Sequence[int] = (1,),
        time_scale: HistoryTimeScale = HistoryTimeScale.DAILY,
    ) -> str:
        ids = _deduplicate_item_ids(item_ids)
        city_values = self._validate_cities(cities)
        quality_values = _validate_qualities(qualities)
        scale = self._validate_window(start_date, end_date, time_scale)
        item_path = ",".join(quote(item_id, safe="@_") for item_id in ids)
        params = {
            "date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "locations": ",".join(city_values),
            "qualities": ",".join(str(q) for q in quality_values),
            "time-scale": str(int(scale)),
        }
        return (
            f"{self.region.api_base_url}/api/v2/stats/history/{item_path}.json?{urlencode(params)}"
        )

    def fetch_history(
        self,
        item_ids: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        cities: Sequence[str],
        qualities: Sequence[int] = (1,),
        time_scale: HistoryTimeScale = HistoryTimeScale.DAILY,
        is_cancelled: CancellationCheck | None = None,
        on_batch_success: HistoryBatchSuccessCallback | None = None,
        on_progress: HistoryProgressCallback | None = None,
    ) -> HistoryFetchResult:
        unique_ids = _deduplicate_item_ids(item_ids)
        city_values = self._validate_cities(cities)
        quality_values = _validate_qualities(qualities)
        scale = self._validate_window(start_date, end_date, time_scale)
        batches = list(
            self._bounded_batches(
                unique_ids,
                start_date=start_date,
                end_date=end_date,
                cities=city_values,
                qualities=quality_values,
                time_scale=scale,
            )
        )
        if len(batches) > self.max_batches:
            raise ValueError(
                f"AODP history request needs {len(batches)} batches; configured maximum is "
                f"{self.max_batches}"
            )
        requests = tuple(
            (
                batch_number,
                batch,
                self.build_history_url(
                    batch,
                    start_date=start_date,
                    end_date=end_date,
                    cities=city_values,
                    qualities=quality_values,
                    time_scale=scale,
                ),
            )
            for batch_number, batch in enumerate(batches, start=1)
        )

        started_at = self._clock()
        intervals: list[MarketHistoryInterval] = []
        failures: list[BatchFailure] = []
        record_failures: list[HistoryRecordFailure] = []
        successful_batches = 0
        request_attempts = 0
        retry_count = 0
        completed_batches = 0
        completed_item_ids: list[str] = []
        cancelled = False
        max_url_bytes = max((_url_length(url) for _, _, url in requests), default=0)
        for batch_number, batch, url in requests:
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            attempts_for_batch = 0
            retries_for_batch = 0
            while True:
                if attempts_for_batch and is_cancelled is not None and is_cancelled():
                    cancelled = True
                    break
                attempts_for_batch += 1
                request_attempts += 1
                try:
                    parsed, invalid = self._fetch_url(
                        url,
                        batch_number=batch_number,
                        expected_item_ids=batch,
                        expected_cities=city_values,
                        expected_qualities=quality_values,
                        time_scale=scale,
                    )
                except (MarketTransportError, HistoryDataError) as exc:
                    if exc.retryable and retries_for_batch < self.max_retries:
                        if is_cancelled is not None and is_cancelled():
                            cancelled = True
                            break
                        delay = self.retry_backoff_seconds * (2**retries_for_batch)
                        if exc.retry_after_seconds is not None:
                            delay = max(delay, exc.retry_after_seconds)
                        if delay:
                            self._sleeper(delay)
                        retries_for_batch += 1
                        retry_count += 1
                        continue
                    failure = BatchFailure(batch_number, batch, str(exc))
                    failures.append(failure)
                    break
                except MarketDataError as exc:
                    failure = BatchFailure(batch_number, batch, str(exc))
                    failures.append(failure)
                    break
                else:
                    successful_batches += 1
                    intervals.extend(parsed)
                    record_failures.extend(invalid)
                    if on_batch_success is not None:
                        on_batch_success(tuple(parsed))
                    failure = None
                    break

            if cancelled:
                break
            completed_batches += 1
            completed_item_ids.extend(batch)
            if on_progress is not None:
                on_progress(
                    HistoryBatchProgress(
                        batch_number=batch_number,
                        batch_count=len(batches),
                        item_ids=batch,
                        successful=failure is None,
                        records_returned=len(parsed) if failure is None else 0,
                        request_attempts=attempts_for_batch,
                        retry_count=retries_for_batch,
                        failure=failure,
                        record_failures=tuple(invalid) if failure is None else (),
                    )
                )

        return HistoryFetchResult(
            intervals=tuple(intervals),
            failures=tuple(failures),
            record_failures=tuple(record_failures),
            batch_count=len(batches),
            items_requested=len(unique_ids),
            successful_batches=successful_batches,
            elapsed_seconds=max(self._clock() - started_at, 0.0),
            start_date=start_date,
            end_date=end_date,
            time_scale=scale,
            request_attempts=request_attempts,
            retry_count=retry_count,
            completed_batches=completed_batches,
            cancelled=cancelled,
            max_url_bytes=max_url_bytes,
            completed_item_ids=tuple(completed_item_ids),
        )

    def _fetch_url(
        self,
        url: str,
        *,
        batch_number: int,
        expected_item_ids: Sequence[str],
        expected_cities: Sequence[str],
        expected_qualities: Sequence[int],
        time_scale: HistoryTimeScale,
    ) -> tuple[list[MarketHistoryInterval], list[HistoryRecordFailure]]:
        try:
            payload = self._transport(url, self.timeout)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            failure = _transport_failure(exc, label="history data", now=self._now())
            raise HistoryDataError(
                str(failure),
                retryable=failure.retryable,
                status_code=failure.status_code,
                retry_after_seconds=failure.retry_after_seconds,
            ) from exc
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HistoryDataError("AODP returned malformed history JSON") from exc
        if not isinstance(decoded, list):
            raise HistoryDataError("AODP history response was not a list")

        expected_ids = {item_id.casefold() for item_id in expected_item_ids}
        expected_city_keys = {_normalize_city(city) for city in expected_cities}
        expected_quality_values = set(expected_qualities)
        fetched_at = self._now()
        intervals: list[MarketHistoryInterval] = []
        failures: list[HistoryRecordFailure] = []
        for series_number, series in enumerate(decoded, start=1):
            try:
                identity, points = self._parse_series_identity(
                    series,
                    expected_ids=expected_ids,
                    expected_cities=expected_city_keys,
                    expected_qualities=expected_quality_values,
                )
            except (MarketDataError, TypeError, ValueError) as exc:
                failures.append(HistoryRecordFailure(batch_number, series_number, None, str(exc)))
                continue

            item_id, city, quality = identity
            for point_number, point in enumerate(points, start=1):
                try:
                    intervals.append(
                        self._parse_point(
                            point,
                            item_id=item_id,
                            city=city,
                            quality=quality,
                            time_scale=time_scale,
                            fetched_at=fetched_at,
                        )
                    )
                except (MarketDataError, TypeError, ValueError) as exc:
                    failures.append(
                        HistoryRecordFailure(batch_number, series_number, point_number, str(exc))
                    )
        return intervals, failures

    @staticmethod
    def _parse_series_identity(
        series: Any,
        *,
        expected_ids: set[str],
        expected_cities: set[str],
        expected_qualities: set[int],
    ) -> tuple[tuple[str, str, int], list[Any]]:
        if not isinstance(series, dict):
            raise HistoryDataError("history series was not an object")
        item_id = series.get("item_id")
        city = series.get("location")
        quality = series.get("quality")
        points = series.get("data")
        if not isinstance(item_id, str) or not item_id:
            raise HistoryDataError("history series has no valid item_id")
        if not isinstance(city, str) or not city:
            raise HistoryDataError("history series has no valid location")
        if isinstance(quality, bool) or not isinstance(quality, int):
            raise HistoryDataError("history series has no valid quality")
        if not isinstance(points, list):
            raise HistoryDataError("history series data was not a list")
        if item_id.casefold() not in expected_ids:
            raise HistoryDataError(f"unexpected history item_id {item_id!r}")
        if _normalize_city(city) not in expected_cities:
            raise HistoryDataError(f"unexpected history location {city!r}")
        if quality not in expected_qualities:
            raise HistoryDataError(f"unexpected history quality {quality!r}")
        return (item_id, city, quality), points

    def _parse_point(
        self,
        point: Any,
        *,
        item_id: str,
        city: str,
        quality: int,
        time_scale: HistoryTimeScale,
        fetched_at: datetime,
    ) -> MarketHistoryInterval:
        if not isinstance(point, dict):
            raise HistoryDataError("history point was not an object")
        item_count = point.get("item_count")
        average_price = point.get("avg_price")
        if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count <= 0:
            raise HistoryDataError("history point has no positive item_count")
        if (
            isinstance(average_price, bool)
            or not isinstance(average_price, (int, float))
            or not math.isfinite(average_price)
            or average_price <= 0
        ):
            raise HistoryDataError("history point has no valid avg_price")
        observed_at = _parse_timestamp(point.get("timestamp"))
        if observed_at is None:
            raise HistoryDataError("history point has no valid timestamp")
        if future_offset_beyond_tolerance(observed_at, now=fetched_at) is not None:
            raise HistoryDataError("history point has a materially future-dated timestamp")
        return MarketHistoryInterval(
            item_id=item_id,
            city=city,
            quality=quality,
            region=self.region,
            observed_at=observed_at,
            item_count=item_count,
            average_price=float(average_price),
            time_scale=time_scale,
            fetched_at=fetched_at,
            provenance=Provenance.AODP_LIVE,
        )

    def _now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            raise ValueError("wall_clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _bounded_batches(
        self,
        item_ids: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        cities: Sequence[str],
        qualities: Sequence[int],
        time_scale: HistoryTimeScale,
    ) -> Iterator[tuple[str, ...]]:
        unique_ids = _deduplicate_item_ids(item_ids)
        current: list[str] = []
        for item_id in unique_ids:
            single_url = self.build_history_url(
                (item_id,),
                start_date=start_date,
                end_date=end_date,
                cities=cities,
                qualities=qualities,
                time_scale=time_scale,
            )
            if _url_length(single_url) > self.max_url_length:
                raise ValueError(f"AODP history URL is too long for item ID {item_id!r}")
            candidate = (*current, item_id)
            candidate_url = self.build_history_url(
                candidate,
                start_date=start_date,
                end_date=end_date,
                cities=cities,
                qualities=qualities,
                time_scale=time_scale,
            )
            if current and (
                len(candidate) > self.batch_size or _url_length(candidate_url) > self.max_url_length
            ):
                yield tuple(current)
                current = [item_id]
            else:
                current.append(item_id)
        if current:
            yield tuple(current)

    def _validate_window(
        self,
        start_date: date,
        end_date: date,
        time_scale: HistoryTimeScale,
    ) -> HistoryTimeScale:
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise ValueError("history dates must be date values")
        if end_date < start_date:
            raise ValueError("end_date cannot be earlier than start_date")
        if (end_date - start_date).days > self.max_date_span_days:
            raise ValueError(f"history date span cannot exceed {self.max_date_span_days} days")
        try:
            return HistoryTimeScale(time_scale)
        except ValueError as exc:
            raise ValueError("history time_scale must be 1, 6, or 24 hours") from exc

    @staticmethod
    def _validate_cities(cities: Sequence[str]) -> tuple[str, ...]:
        values = tuple(cities)
        if not values:
            raise ValueError("at least one explicit history city is required")
        if any(not isinstance(city, str) or not city.strip() for city in values):
            raise ValueError("history cities must contain non-empty strings")
        unique: dict[str, str] = {}
        for city in values:
            clean = city.strip()
            unique.setdefault(_normalize_city(clean), clean)
        return tuple(unique.values())
