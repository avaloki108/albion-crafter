from __future__ import annotations

import gzip
import json
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from albion_crafter.core.freshness import future_offset_beyond_tolerance
from albion_crafter.core.provenance import Provenance

from .models import MarketPrice, Region

SAFE_AODP_URL_LENGTH = 3_900
DEFAULT_PRICE_BATCH_SIZE = 100
DEFAULT_MAX_BATCHES = 25
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5


class MarketDataError(RuntimeError):
    """A recoverable failure while requesting or parsing market data."""


class MarketTransportError(MarketDataError):
    """A transport failure annotated with conservative retry information."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


Transport = Callable[[str, float], bytes]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
WallClock = Callable[[], datetime]
CancellationCheck = Callable[[], bool]


def _default_transport(url: str, timeout: float) -> bytes:
    # AODP publishes 180 requests/minute and 300 requests/5 minutes. Operations
    # remain sequential and preflight-bounded; the normal full-sync plan is well
    # below both windows, so it does not need an artificial per-request delay.
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "AlbionCrafter/0.6.2",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS hosts
        payload = response.read()
        if response.headers.get("Content-Encoding", "").casefold() == "gzip":
            return gzip.decompress(payload)
        return payload


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _retry_after_seconds(error: HTTPError, now: datetime) -> float | None:
    if error.code != 429 or error.headers is None:
        return None
    value = error.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _transport_failure(error: OSError, *, label: str, now: datetime) -> MarketTransportError:
    if isinstance(error, HTTPError):
        status = error.code
        return MarketTransportError(
            f"Unable to retrieve AODP {label}: {error}",
            retryable=status in {408, 425, 429} or 500 <= status <= 599,
            status_code=status,
            retry_after_seconds=_retry_after_seconds(error, now),
        )
    return MarketTransportError(
        f"Unable to retrieve AODP {label}: {error}",
        retryable=True,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MarketDataError(f"AODP returned an invalid timestamp: {value!r}")
    if value.startswith("0001-01-01"):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarketDataError(f"AODP returned an invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_price(value: Any) -> int | None:
    """AODP uses zero when an order side has no observation; expose it as missing."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataError(f"AODP returned an invalid price: {value!r}")
    if value < 0:
        raise MarketDataError(f"AODP returned an invalid price: {value!r}")
    return value or None


def _url_length(url: str) -> int:
    """Return the complete encoded URL length in bytes (AODP's documented unit)."""
    return len(url.encode("ascii"))


def _deduplicate_item_ids(item_ids: Sequence[str]) -> tuple[str, ...]:
    if not item_ids:
        raise ValueError("at least one item ID is required")
    unique: dict[str, str] = {}
    for item_id in item_ids:
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item IDs must be non-empty strings")
        clean = item_id.strip()
        unique.setdefault(clean.casefold(), clean)
    return tuple(unique.values())


def _validate_qualities(qualities: Sequence[int]) -> tuple[int, ...]:
    values = tuple(qualities)
    if not values:
        raise ValueError("at least one quality is required")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5
        for value in values
    ):
        raise ValueError("qualities must contain integers between 1 and 5")
    return tuple(dict.fromkeys(values))


def _normalize_city(value: str) -> str:
    return value.replace(" ", "").replace("'", "").casefold()


def _validate_current_cities(cities: Sequence[str]) -> tuple[str, ...]:
    values = tuple(cities)
    if any(not isinstance(city, str) or not city.strip() for city in values):
        raise ValueError("cities must contain non-empty strings")
    unique: dict[str, str] = {}
    for city in values:
        clean = city.strip()
        unique.setdefault(_normalize_city(clean), clean)
    return tuple(unique.values())


def _build_prices_url(
    region: Region,
    item_ids: Sequence[str],
    *,
    cities: Sequence[str],
    qualities: Sequence[int],
) -> str:
    item_path = ",".join(quote(item_id, safe="@_") for item_id in item_ids)
    params: dict[str, str] = {}
    if cities:
        params["locations"] = ",".join(cities)
    params["qualities"] = ",".join(str(q) for q in qualities)
    return f"{region.api_base_url}/api/v2/stats/prices/{item_path}.json?{urlencode(params)}"


@dataclass(frozen=True, slots=True)
class BatchFailure:
    batch_number: int
    item_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class RecordFailure:
    batch_number: int
    row_number: int
    message: str


@dataclass(frozen=True, slots=True)
class AODPRequestBatch:
    batch_number: int
    item_ids: tuple[str, ...]
    url: str
    url_bytes: int


@dataclass(frozen=True, slots=True)
class AODPRequestPlan:
    region: Region
    cities: tuple[str, ...]
    qualities: tuple[int, ...]
    items_requested: int
    batches: tuple[AODPRequestBatch, ...]

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    @property
    def max_url_bytes(self) -> int:
        return max((batch.url_bytes for batch in self.batches), default=0)

    @property
    def total_url_bytes(self) -> int:
        return sum(batch.url_bytes for batch in self.batches)

    @property
    def max_concurrency(self) -> int:
        return 1


@dataclass(frozen=True, slots=True)
class BatchProgress:
    batch_number: int
    batch_count: int
    item_ids: tuple[str, ...]
    successful: bool
    records_returned: int
    request_attempts: int
    retry_count: int
    failure: BatchFailure | None = None
    record_failures: tuple[RecordFailure, ...] = ()

    @property
    def completed_batches(self) -> int:
        return self.batch_number


BatchSuccessCallback = Callable[[tuple[MarketPrice, ...]], None]
BatchProgressCallback = Callable[[BatchProgress], None]


def plan_price_requests(
    item_ids: Sequence[str],
    *,
    region: Region = Region.AMERICAS,
    cities: Sequence[str] = (),
    qualities: Sequence[int] = (1,),
    batch_size: int = DEFAULT_PRICE_BATCH_SIZE,
    max_url_length: int = SAFE_AODP_URL_LENGTH,
) -> AODPRequestPlan:
    """Purely plan bounded current-price URLs without applying an execution cap."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not 512 <= max_url_length <= SAFE_AODP_URL_LENGTH:
        raise ValueError(f"max_url_length must be between 512 and {SAFE_AODP_URL_LENGTH}")
    unique_ids = _deduplicate_item_ids(item_ids)
    city_values = _validate_current_cities(cities)
    quality_values = _validate_qualities(qualities)
    grouped: list[tuple[str, ...]] = []
    current: list[str] = []
    for item_id in unique_ids:
        single_url = _build_prices_url(
            region,
            (item_id,),
            cities=city_values,
            qualities=quality_values,
        )
        if _url_length(single_url) > max_url_length:
            raise ValueError(f"AODP URL is too long for item ID {item_id!r}")
        candidate = (*current, item_id)
        candidate_url = _build_prices_url(
            region,
            candidate,
            cities=city_values,
            qualities=quality_values,
        )
        if current and (len(candidate) > batch_size or _url_length(candidate_url) > max_url_length):
            grouped.append(tuple(current))
            current = [item_id]
        else:
            current.append(item_id)
    if current:
        grouped.append(tuple(current))

    batches = tuple(
        AODPRequestBatch(
            batch_number=index,
            item_ids=batch,
            url=(
                url := _build_prices_url(
                    region,
                    batch,
                    cities=city_values,
                    qualities=quality_values,
                )
            ),
            url_bytes=_url_length(url),
        )
        for index, batch in enumerate(grouped, start=1)
    )
    return AODPRequestPlan(
        region=region,
        cities=city_values,
        qualities=quality_values,
        items_requested=len(unique_ids),
        batches=batches,
    )


@dataclass(frozen=True, slots=True)
class BatchFetchResult:
    records: tuple[MarketPrice, ...]
    failures: tuple[BatchFailure, ...]
    batch_count: int
    items_requested: int = 0
    successful_batches: int = 0
    elapsed_seconds: float = 0.0
    record_failures: tuple[RecordFailure, ...] = ()
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
        return len(self.records)

    @property
    def http_attempts(self) -> int:
        return self.request_attempts


class AODPClient:
    """Bounded current-price client executing one HTTP request at a time."""

    def __init__(
        self,
        region: Region = Region.AMERICAS,
        *,
        timeout: float = 10.0,
        batch_size: int = DEFAULT_PRICE_BATCH_SIZE,
        max_url_length: int = SAFE_AODP_URL_LENGTH,
        max_batches: int = DEFAULT_MAX_BATCHES,
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
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be finite and non-negative")
        self.region = region
        self.timeout = timeout
        self.batch_size = batch_size
        self.max_url_length = max_url_length
        self.max_batches = max_batches
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._transport = transport or _default_transport
        self._clock = clock
        self._sleeper = sleeper
        self._wall_clock = wall_clock

    def build_prices_url(
        self,
        item_ids: Sequence[str],
        *,
        cities: Sequence[str] = (),
        qualities: Sequence[int] = (1,),
    ) -> str:
        ids = _deduplicate_item_ids(item_ids)
        quality_values = _validate_qualities(qualities)
        city_values = _validate_current_cities(cities)
        return _build_prices_url(
            self.region,
            ids,
            cities=city_values,
            qualities=quality_values,
        )

    def plan_price_requests(
        self,
        item_ids: Sequence[str],
        *,
        cities: Sequence[str] = (),
        qualities: Sequence[int] = (1,),
    ) -> AODPRequestPlan:
        """Plan exact encoded URLs; execution separately enforces ``max_batches``."""
        return plan_price_requests(
            item_ids,
            region=self.region,
            cities=cities,
            qualities=qualities,
            batch_size=self.batch_size,
            max_url_length=self.max_url_length,
        )

    def fetch_prices(
        self,
        item_ids: Sequence[str],
        *,
        cities: Sequence[str] = (),
        qualities: Sequence[int] = (1,),
    ) -> list[MarketPrice]:
        """Fetch all batches, raising if a batch or individual record fails validation."""
        result = self.fetch_prices_batched(item_ids, cities=cities, qualities=qualities)
        if result.failures:
            failed = result.failures[0]
            raise MarketDataError(
                f"AODP batch {failed.batch_number} failed for "
                f"{len(failed.item_ids)} items: {failed.message}"
            )
        if result.record_failures:
            failed = result.record_failures[0]
            raise MarketDataError(
                f"AODP batch {failed.batch_number} row {failed.row_number} was invalid: "
                f"{failed.message}"
            )
        return list(result.records)

    def fetch_prices_batched(
        self,
        item_ids: Sequence[str],
        *,
        cities: Sequence[str] = (),
        qualities: Sequence[int] = (1,),
        is_cancelled: CancellationCheck | None = None,
        on_batch_success: BatchSuccessCallback | None = None,
        on_progress: BatchProgressCallback | None = None,
    ) -> BatchFetchResult:
        plan = self.plan_price_requests(item_ids, cities=cities, qualities=qualities)
        if plan.batch_count > self.max_batches:
            raise ValueError(
                f"AODP request needs {plan.batch_count} batches; configured maximum is "
                f"{self.max_batches}"
            )

        started_at = self._clock()
        records: list[MarketPrice] = []
        failures: list[BatchFailure] = []
        record_failures: list[RecordFailure] = []
        successful_batches = 0
        request_attempts = 0
        retry_count = 0
        completed_batches = 0
        completed_item_ids: list[str] = []
        cancelled = False
        for request_batch in plan.batches:
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
                        request_batch.url,
                        batch_number=request_batch.batch_number,
                        expected_item_ids=request_batch.item_ids,
                        expected_cities=plan.cities,
                        expected_qualities=plan.qualities,
                    )
                except MarketTransportError as exc:
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
                    failure = BatchFailure(
                        request_batch.batch_number,
                        request_batch.item_ids,
                        str(exc),
                    )
                    failures.append(failure)
                    break
                except MarketDataError as exc:
                    failure = BatchFailure(
                        request_batch.batch_number,
                        request_batch.item_ids,
                        str(exc),
                    )
                    failures.append(failure)
                    break
                else:
                    successful_batches += 1
                    records.extend(parsed)
                    record_failures.extend(invalid)
                    if on_batch_success is not None:
                        on_batch_success(tuple(parsed))
                    failure = None
                    break

            if cancelled:
                break
            completed_batches += 1
            completed_item_ids.extend(request_batch.item_ids)
            if on_progress is not None:
                on_progress(
                    BatchProgress(
                        batch_number=request_batch.batch_number,
                        batch_count=plan.batch_count,
                        item_ids=request_batch.item_ids,
                        successful=failure is None,
                        records_returned=len(parsed) if failure is None else 0,
                        request_attempts=attempts_for_batch,
                        retry_count=retries_for_batch,
                        failure=failure,
                        record_failures=tuple(invalid) if failure is None else (),
                    )
                )
        elapsed_seconds = max(self._clock() - started_at, 0.0)
        return BatchFetchResult(
            records=tuple(records),
            failures=tuple(failures),
            batch_count=plan.batch_count,
            items_requested=plan.items_requested,
            successful_batches=successful_batches,
            elapsed_seconds=elapsed_seconds,
            record_failures=tuple(record_failures),
            request_attempts=request_attempts,
            retry_count=retry_count,
            completed_batches=completed_batches,
            cancelled=cancelled,
            max_url_bytes=plan.max_url_bytes,
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
    ) -> tuple[list[MarketPrice], list[RecordFailure]]:
        try:
            payload = self._transport(url, self.timeout)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise _transport_failure(exc, label="market data", now=self._now()) from exc
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MarketDataError("AODP returned malformed JSON") from exc
        if not isinstance(decoded, list):
            raise MarketDataError("AODP response was not a list of price records")

        expected_ids = {item_id.casefold() for item_id in expected_item_ids}
        expected_city_keys = {_normalize_city(city) for city in expected_cities}
        expected_quality_values = set(expected_qualities)
        fetched_at = self._now()
        records: list[MarketPrice] = []
        failures: list[RecordFailure] = []
        for row_number, row in enumerate(decoded, start=1):
            try:
                records.append(
                    self._parse_row(
                        row,
                        fetched_at=fetched_at,
                        expected_ids=expected_ids,
                        expected_cities=expected_city_keys,
                        expected_qualities=expected_quality_values,
                    )
                )
            except (MarketDataError, TypeError, ValueError) as exc:
                failures.append(RecordFailure(batch_number, row_number, str(exc)))
        return records, failures

    def _parse_row(
        self,
        row: Any,
        *,
        fetched_at: datetime,
        expected_ids: set[str],
        expected_cities: set[str],
        expected_qualities: set[int],
    ) -> MarketPrice:
        if not isinstance(row, dict):
            raise MarketDataError("price record was not an object")
        item_id = row.get("item_id")
        city = row.get("city")
        quality = row.get("quality")
        if not isinstance(item_id, str) or not item_id:
            raise MarketDataError("price record has no valid item_id")
        if not isinstance(city, str) or not city:
            raise MarketDataError("price record has no valid city")
        if isinstance(quality, bool) or not isinstance(quality, int):
            raise MarketDataError("price record has no valid quality")
        if item_id.casefold() not in expected_ids:
            raise MarketDataError(f"unexpected item_id {item_id!r}")
        if expected_cities and _normalize_city(city) not in expected_cities:
            raise MarketDataError(f"unexpected city {city!r}")
        if quality not in expected_qualities:
            raise MarketDataError(f"unexpected quality {quality!r}")

        sell_price = _optional_price(row.get("sell_price_min"))
        sell_timestamp = _parse_timestamp(row.get("sell_price_min_date"))
        buy_price = _optional_price(row.get("buy_price_max"))
        buy_timestamp = _parse_timestamp(row.get("buy_price_max_date"))
        # A market side is decision-grade evidence only as a complete value/timestamp pair.
        # AODP occasionally represents absent observations with a zero value, a sentinel date,
        # or only one half of the pair; normalize every incomplete pair to honestly missing.
        if sell_price is None or sell_timestamp is None:
            sell_price = None
            sell_timestamp = None
        if buy_price is None or buy_timestamp is None:
            buy_price = None
            buy_timestamp = None
        for side, price, timestamp in (
            ("sell", sell_price, sell_timestamp),
            ("buy", buy_price, buy_timestamp),
        ):
            if price is not None and future_offset_beyond_tolerance(timestamp, now=fetched_at):
                raise MarketDataError(
                    f"price record has a materially future-dated {side} observation timestamp"
                )

        return MarketPrice(
            item_id=item_id,
            city=city,
            quality=quality,
            region=self.region,
            sell_price=sell_price,
            sell_price_timestamp=sell_timestamp,
            buy_price=buy_price,
            buy_price_timestamp=buy_timestamp,
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
        cities: Sequence[str],
        qualities: Sequence[int],
    ) -> Iterator[tuple[str, ...]]:
        plan = self.plan_price_requests(item_ids, cities=cities, qualities=qualities)
        yield from (batch.item_ids for batch in plan.batches)
