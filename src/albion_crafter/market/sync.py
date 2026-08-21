from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic

from albion_crafter.core.freshness import DEFAULT_CLOCK_SKEW_TOLERANCE
from albion_crafter.core.models import ActionKind, Item, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import station_type_for_item
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    MarketPriceRepository,
    SettingsRepository,
)

from .aodp import (
    DEFAULT_PRICE_BATCH_SIZE,
    SAFE_AODP_URL_LENGTH,
    AODPClient,
    BatchFailure,
    BatchFetchResult,
    BatchProgress,
    CancellationCheck,
    Clock,
    RecordFailure,
    plan_price_requests,
)
from .backfill import (
    MissingSellHistoryBackfillService,
    MissingSellHistoryProgress,
)
from .models import MarketPrice, Region

DEFAULT_ROYAL_SYNC_CITIES: tuple[str, ...] = (
    "Bridgewatch",
    "Fort Sterling",
    "Lymhurst",
    "Martlock",
    "Thetford",
)
OPTIONAL_ROYAL_SYNC_CITIES: tuple[str, ...] = ("Caerleon",)
ROYAL_SYNC_CITIES: tuple[str, ...] = (
    *DEFAULT_ROYAL_SYNC_CITIES,
    *OPTIONAL_ROYAL_SYNC_CITIES,
)
MARKET_SYNC_CITIES_SETTING = "royal_market_sync_cities"
MARKET_SYNC_LAST_RESULT_SETTING = "royal_market_sync_last_result"
MARKET_SYNC_LAST_COMPLETE_SETTING = "royal_market_sync_last_complete"


@dataclass(frozen=True, slots=True)
class MarketUniverseItem:
    item: Item
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketUniverse:
    source_version: str | None
    total_catalog_items: int
    total_catalog_recipes: int
    supported_output_items: int
    required_ingredient_items: int
    items: tuple[MarketUniverseItem, ...]

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(entry.item.item_id for entry in self.items)

    @property
    def item_count(self) -> int:
        return len(self.items)


class RoyalMarketUniverseService:
    """Derive the market IDs needed by supported production and arbitrage workflows."""

    def __init__(self, catalog: CatalogRepository) -> None:
        self.catalog = catalog
        self._cache_key: tuple[str | None, datetime | None, int, int] | None = None
        self._cached: MarketUniverse | None = None

    def invalidate(self) -> None:
        self._cache_key = None
        self._cached = None

    def derive(self) -> MarketUniverse:
        metadata = self.catalog.import_metadata()
        total_items, total_recipes = self.catalog.counts()
        source_version = metadata.source_version if metadata is not None else None
        imported_at = metadata.imported_at if metadata is not None else None
        cache_key = (source_version, imported_at, total_items, total_recipes)
        if cache_key == self._cache_key and self._cached is not None:
            return self._cached

        supported = tuple(
            recipe for recipe in self.catalog.list_recipes() if self._is_supported(recipe)
        )
        reasons_by_id: dict[str, set[str]] = {}
        for recipe in supported:
            output_kind = (
                "Refining output" if recipe.action_kind is ActionKind.REFINE else "Crafting output"
            )
            ingredient_kind = (
                "Refining ingredient"
                if recipe.action_kind is ActionKind.REFINE
                else "Crafting ingredient"
            )
            reasons_by_id.setdefault(recipe.output.item_id, set()).add(output_kind)
            reasons_by_id[recipe.output.item_id].add("Arbitrage output")
            for material in recipe.materials:
                reasons_by_id.setdefault(material.item_id, set()).add(ingredient_kind)

        item_ids = tuple(sorted(reasons_by_id))
        catalog_items = {item.item_id: item for item in self.catalog.list_items(item_ids)}
        entries = tuple(
            MarketUniverseItem(catalog_items[item_id], tuple(sorted(reasons_by_id[item_id])))
            for item_id in item_ids
            if item_id in catalog_items
        )
        output_ids = {recipe.output.item_id for recipe in supported}
        ingredient_ids = {
            material.item_id
            for recipe in supported
            for material in recipe.materials
            if material.item_id in catalog_items
        }
        universe = MarketUniverse(
            source_version=source_version,
            total_catalog_items=total_items,
            total_catalog_recipes=total_recipes,
            supported_output_items=len(output_ids),
            required_ingredient_items=len(ingredient_ids),
            items=entries,
        )
        self._cache_key = cache_key
        self._cached = universe
        return universe

    @staticmethod
    def _is_supported(recipe: Recipe) -> bool:
        return bool(
            recipe.provenance is Provenance.STATIC_GAME_DATA
            and not recipe.recipe_ambiguous
            and recipe.item_value is not None
            and all(material.returnable is not None for material in recipe.materials)
            and station_type_for_item(recipe.output) is not None
        )


@dataclass(frozen=True, slots=True)
class RoyalMarketSyncProgress:
    planned_batches: int
    completed_batches: int
    successful_batches: int
    failed_batches: int
    rows_received: int
    useful_sides_received: int
    sides_updated: int
    missing_sides: int
    request_attempts: int
    retry_count: int
    current_batch: BatchProgress | None
    phase: str = "current"
    history_progress: MissingSellHistoryProgress | None = None


RoyalMarketSyncProgressCallback = Callable[[RoyalMarketSyncProgress], None]


@dataclass(frozen=True, slots=True)
class RoyalMarketSyncResult:
    started_at: datetime
    completed_at: datetime
    region: Region
    cities: tuple[str, ...]
    item_ids: tuple[str, ...]
    total_catalog_items: int
    supported_output_items: int
    required_ingredient_items: int
    planned_batches: int
    completed_batches: int
    successful_batches: int
    failures: tuple[BatchFailure, ...]
    record_failures: tuple[RecordFailure, ...]
    cancelled_batches: int
    rows_returned: int
    useful_sides_received: int
    sides_updated: int
    missing_sides: int
    observations_le_2h: int
    observations_le_4h: int
    observations_le_24h: int
    observations_older_24h: int
    rows_with_no_usable_side: int
    http_attempts: int
    retry_count: int
    elapsed_seconds: float
    cancelled: bool
    oldest_observation: datetime | None
    newest_observation: datetime | None
    per_city_rows: tuple[tuple[str, int], ...]
    max_url_bytes: int
    history_sell_keys_requested: int = 0
    historical_sell_estimates_available: int = 0
    unresolved_sell_sides_after_history: int = 0
    history_planned_batches: int = 0
    history_completed_batches: int = 0
    history_successful_batches: int = 0
    history_failed_batches: int = 0
    history_record_failures: int = 0

    @property
    def item_count(self) -> int:
        return len(self.item_ids)

    @property
    def city_count(self) -> int:
        return len(self.cities)

    @property
    def failed_batches(self) -> int:
        return len(self.failures)

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if (
            self.failures
            or self.record_failures
            or self.history_failed_batches
            or self.history_record_failures
        ):
            return (
                "partial"
                if self.successful_batches or self.history_successful_batches
                else "failed"
            )
        return "complete"


class RoyalMarketSyncService:
    """Synchronize the intentional Royal-market universe through bounded AODP batches."""

    def __init__(
        self,
        universe: RoyalMarketUniverseService,
        repository: MarketPriceRepository,
        *,
        client_factory: Callable[..., AODPClient] = AODPClient,
        batch_size: int = DEFAULT_PRICE_BATCH_SIZE,
        max_url_length: int = SAFE_AODP_URL_LENGTH,
        history_backfill: MissingSellHistoryBackfillService | None = None,
        clock: Clock = monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.universe = universe
        self.repository = repository
        self.client_factory = client_factory
        self.batch_size = batch_size
        self.max_url_length = max_url_length
        self.history_backfill = history_backfill
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))

    def synchronize(
        self,
        region: Region,
        cities: Sequence[str],
        *,
        is_cancelled: CancellationCheck | None = None,
        on_progress: RoyalMarketSyncProgressCallback | None = None,
    ) -> RoyalMarketSyncResult:
        city_values = self._validate_cities(cities)
        market_universe = self.universe.derive()
        if not market_universe.item_ids:
            raise ValueError("the active catalog has no supported market synchronization items")
        request_plan = plan_price_requests(
            market_universe.item_ids,
            region=region,
            cities=city_values,
            qualities=(1,),
            batch_size=self.batch_size,
            max_url_length=self.max_url_length,
        )
        client = self.client_factory(
            region,
            batch_size=self.batch_size,
            max_url_length=self.max_url_length,
            max_batches=request_plan.batch_count,
        )
        if client.region != region:
            raise ValueError("market sync client factory returned the wrong region")

        started_at = self._aware_now()
        started_clock = self._clock()
        rows_received = 0
        useful_sides = 0
        sides_updated = 0
        missing_sides = 0
        successful_batches = 0
        failed_batches = 0
        request_attempts = 0
        retry_count = 0

        def persist_batch(records: tuple[MarketPrice, ...]) -> None:
            nonlocal rows_received, useful_sides, sides_updated, missing_sides
            rows_received += len(records)
            useful_sides += sum(
                int(record.sell_price is not None and record.sell_price_timestamp is not None)
                + int(record.buy_price is not None and record.buy_price_timestamp is not None)
                for record in records
            )
            missing_sides += sum(
                int(record.sell_price is None or record.sell_price_timestamp is None)
                + int(record.buy_price is None or record.buy_price_timestamp is None)
                for record in records
            )
            sides_updated += self._count_side_updates(records)
            self.repository.upsert_many(records)

        def report(progress: BatchProgress) -> None:
            nonlocal successful_batches, failed_batches, request_attempts, retry_count
            successful_batches += int(progress.successful)
            failed_batches += int(not progress.successful)
            request_attempts += progress.request_attempts
            retry_count += progress.retry_count
            if on_progress is None:
                return
            on_progress(
                RoyalMarketSyncProgress(
                    planned_batches=request_plan.batch_count,
                    completed_batches=progress.completed_batches,
                    successful_batches=successful_batches,
                    failed_batches=failed_batches,
                    rows_received=rows_received,
                    useful_sides_received=useful_sides,
                    sides_updated=sides_updated,
                    missing_sides=missing_sides,
                    request_attempts=request_attempts,
                    retry_count=retry_count,
                    current_batch=progress,
                )
            )

        result = client.fetch_prices_batched(
            market_universe.item_ids,
            cities=city_values,
            qualities=(1,),
            is_cancelled=is_cancelled,
            on_progress=report,
            on_batch_success=persist_batch,
        )
        completed_at = self._aware_now()
        current_sync = self._build_result(
            market_universe,
            region,
            city_values,
            request_plan.batch_count,
            result,
            started_at,
            completed_at,
            max(self._clock() - started_clock, 0.0),
            sides_updated,
        )
        if result.cancelled or self.history_backfill is None:
            return current_sync

        def report_history(progress: MissingSellHistoryProgress) -> None:
            if on_progress is None:
                return
            on_progress(
                RoyalMarketSyncProgress(
                    planned_batches=request_plan.batch_count,
                    completed_batches=result.completed_batches,
                    successful_batches=successful_batches,
                    failed_batches=failed_batches,
                    rows_received=rows_received,
                    useful_sides_received=useful_sides,
                    sides_updated=sides_updated,
                    missing_sides=current_sync.missing_sides,
                    request_attempts=request_attempts,
                    retry_count=retry_count,
                    current_batch=None,
                    phase="history",
                    history_progress=progress,
                )
            )

        history_result = self.history_backfill.refresh_missing(
            region,
            market_universe.item_ids,
            city_values,
            quality=1,
            as_of=self._aware_now(),
            is_cancelled=is_cancelled,
            on_progress=report_history,
        )
        completed_at = self._aware_now()
        return replace(
            current_sync,
            completed_at=completed_at,
            elapsed_seconds=max(self._clock() - started_clock, 0.0),
            cancelled=history_result.cancelled,
            history_sell_keys_requested=history_result.requested_count,
            historical_sell_estimates_available=history_result.resolved_count,
            unresolved_sell_sides_after_history=history_result.unresolved_count,
            history_planned_batches=history_result.batches_planned,
            history_completed_batches=history_result.batches_completed,
            history_successful_batches=history_result.batches_succeeded,
            history_failed_batches=history_result.batches_failed,
            history_record_failures=history_result.record_failures,
        )

    @staticmethod
    def _validate_cities(cities: Sequence[str]) -> tuple[str, ...]:
        known = {city.casefold(): city for city in ROYAL_SYNC_CITIES}
        selected: list[str] = []
        unknown: list[str] = []
        for raw_city in cities:
            city = known.get(raw_city.strip().casefold())
            if city is None:
                unknown.append(raw_city)
            elif city not in selected:
                selected.append(city)
        if unknown:
            raise ValueError("unsupported Royal sync cities: " + ", ".join(unknown))
        if not selected:
            raise ValueError("select at least one Royal market city")
        return tuple(selected)

    @staticmethod
    def _build_result(
        universe: MarketUniverse,
        region: Region,
        cities: tuple[str, ...],
        planned_batches: int,
        result: BatchFetchResult,
        started_at: datetime,
        completed_at: datetime,
        elapsed_seconds: float,
        sides_updated: int,
    ) -> RoyalMarketSyncResult:
        timestamps: list[datetime] = []
        le_2h = 0
        le_4h = 0
        le_24h = 0
        older_24h = 0
        missing_sides = 0
        no_usable_rows = 0
        useful_sides = 0
        city_rows: Counter[str] = Counter()
        returned_keys: set[tuple[str, str, int]] = set()
        for record in result.records:
            city_rows[record.city] += 1
            returned_keys.add((record.item_id.casefold(), _city_key(record.city), record.quality))
            if not any(
                price is not None and observed_at is not None
                for price, observed_at in (
                    (record.sell_price, record.sell_price_timestamp),
                    (record.buy_price, record.buy_price_timestamp),
                )
            ):
                no_usable_rows += 1
            for price, observed_at in (
                (record.sell_price, record.sell_price_timestamp),
                (record.buy_price, record.buy_price_timestamp),
            ):
                if price is None or observed_at is None:
                    missing_sides += 1
                    continue
                useful_sides += 1
                timestamps.append(observed_at)
                age = max(completed_at - observed_at, timedelta())
                if age <= timedelta(hours=2):
                    le_2h += 1
                if age <= timedelta(hours=4):
                    le_4h += 1
                if age <= timedelta(hours=24):
                    le_24h += 1
                else:
                    older_24h += 1
        expected_rows = len(universe.item_ids) * len(cities)
        unreturned_rows = max(expected_rows - len(returned_keys), 0)
        missing_sides += unreturned_rows * 2
        no_usable_rows += unreturned_rows
        return RoyalMarketSyncResult(
            started_at=started_at,
            completed_at=completed_at,
            region=region,
            cities=cities,
            item_ids=universe.item_ids,
            total_catalog_items=universe.total_catalog_items,
            supported_output_items=universe.supported_output_items,
            required_ingredient_items=universe.required_ingredient_items,
            planned_batches=planned_batches,
            completed_batches=result.completed_batches,
            successful_batches=result.successful_batches,
            failures=result.failures,
            record_failures=result.record_failures,
            cancelled_batches=(
                max(planned_batches - result.completed_batches, 0) if result.cancelled else 0
            ),
            rows_returned=result.records_returned,
            useful_sides_received=useful_sides,
            sides_updated=sides_updated,
            missing_sides=missing_sides,
            observations_le_2h=le_2h,
            observations_le_4h=le_4h,
            observations_le_24h=le_24h,
            observations_older_24h=older_24h,
            rows_with_no_usable_side=no_usable_rows,
            http_attempts=result.request_attempts,
            retry_count=result.retry_count,
            elapsed_seconds=elapsed_seconds,
            cancelled=result.cancelled,
            oldest_observation=min(timestamps, default=None),
            newest_observation=max(timestamps, default=None),
            per_city_rows=tuple((city, city_rows[city]) for city in cities),
            max_url_bytes=result.max_url_bytes,
        )

    def _count_side_updates(self, records: tuple[MarketPrice, ...]) -> int:
        """Count useful sides that will materially change the side-specific cache."""

        if not records:
            return 0
        existing = self.repository.list_for_scan(
            records[0].region,
            cities=tuple(dict.fromkeys(record.city for record in records)),
            qualities=tuple(dict.fromkeys(record.quality for record in records)),
            item_ids=tuple(dict.fromkeys(record.item_id for record in records)),
        )
        sides: dict[tuple[str, str, int, str], tuple[int | None, datetime | None]] = {}
        for row in existing:
            base = (row.item_id.casefold(), _city_key(row.city), row.quality)
            sides[(*base, "sell")] = (row.sell_price, row.sell_price_timestamp)
            sides[(*base, "buy")] = (row.buy_price, row.buy_price_timestamp)
        future_boundary = self._aware_now() + DEFAULT_CLOCK_SKEW_TOLERANCE
        updates = 0
        for row in records:
            base = (row.item_id.casefold(), _city_key(row.city), row.quality)
            for side, price, observed_at in (
                ("sell", row.sell_price, row.sell_price_timestamp),
                ("buy", row.buy_price, row.buy_price_timestamp),
            ):
                if price is None or observed_at is None:
                    continue
                key = (*base, side)
                old = sides.get(key, (None, None))
                old_observed_at = old[1]
                accepted = bool(
                    old_observed_at is None
                    or old_observed_at > future_boundary
                    or observed_at >= old_observed_at
                )
                incoming = (price, observed_at)
                if accepted:
                    if incoming != old:
                        updates += 1
                    sides[key] = incoming
        return updates

    def _aware_now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            raise ValueError("market sync wall clock must be timezone-aware")
        return value.astimezone(UTC)


def _city_key(value: str) -> str:
    return value.replace(" ", "").replace("'", "").casefold()


@dataclass(frozen=True, slots=True)
class StoredMarketSyncResult:
    started_at: datetime
    completed_at: datetime
    region: Region
    cities: tuple[str, ...]
    item_count: int
    planned_batches: int
    completed_batches: int
    successful_batches: int
    failed_batches: int
    rows_returned: int
    useful_sides_received: int
    sides_updated: int
    missing_sides: int
    observations_le_2h: int
    observations_le_4h: int
    observations_le_24h: int
    observations_older_24h: int
    rows_with_no_usable_side: int
    http_attempts: int
    retry_count: int
    elapsed_seconds: float
    cancelled: bool
    status: str
    history_sell_keys_requested: int = 0
    historical_sell_estimates_available: int = 0
    unresolved_sell_sides_after_history: int = 0
    history_planned_batches: int = 0
    history_successful_batches: int = 0
    history_failed_batches: int = 0


class MarketSyncStateRepository:
    """Persist small sync preferences and summaries in the existing settings table."""

    def __init__(self, settings: SettingsRepository) -> None:
        self.settings = settings

    def cities(self) -> tuple[str, ...]:
        try:
            raw = self.settings.get(
                MARKET_SYNC_CITIES_SETTING,
                list(DEFAULT_ROYAL_SYNC_CITIES),
            )
        except (TypeError, ValueError):
            return DEFAULT_ROYAL_SYNC_CITIES
        if not isinstance(raw, list):
            return DEFAULT_ROYAL_SYNC_CITIES
        try:
            return RoyalMarketSyncService._validate_cities(tuple(str(city) for city in raw))
        except ValueError:
            return DEFAULT_ROYAL_SYNC_CITIES

    def save_cities(self, cities: Sequence[str]) -> None:
        selected = RoyalMarketSyncService._validate_cities(cities)
        self.settings.set(MARKET_SYNC_CITIES_SETTING, list(selected))

    def save_result(self, result: RoyalMarketSyncResult) -> None:
        value = self._serialize_result(result)
        self.settings.set(MARKET_SYNC_LAST_RESULT_SETTING, value)
        if result.status == "complete":
            self.settings.set(MARKET_SYNC_LAST_COMPLETE_SETTING, value)

    def last_result(self) -> StoredMarketSyncResult | None:
        return self._stored_result(MARKET_SYNC_LAST_RESULT_SETTING)

    def last_complete_result(self) -> StoredMarketSyncResult | None:
        stored = self._stored_result(MARKET_SYNC_LAST_COMPLETE_SETTING)
        if stored is not None:
            return stored
        # Upgrade path for V0.6.2 prerelease settings that only stored the latest attempt.
        latest = self.last_result()
        return latest if latest is not None and latest.status == "complete" else None

    @staticmethod
    def _serialize_result(result: RoyalMarketSyncResult) -> dict[str, object]:
        return {
            "format_version": 1,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
            "region": result.region.value,
            "cities": list(result.cities),
            "item_count": result.item_count,
            "planned_batches": result.planned_batches,
            "completed_batches": result.completed_batches,
            "successful_batches": result.successful_batches,
            "failed_batches": result.failed_batches,
            "rows_returned": result.rows_returned,
            "useful_sides_received": result.useful_sides_received,
            "sides_updated": result.sides_updated,
            "missing_sides": result.missing_sides,
            "observations_le_2h": result.observations_le_2h,
            "observations_le_4h": result.observations_le_4h,
            "observations_le_24h": result.observations_le_24h,
            "observations_older_24h": result.observations_older_24h,
            "rows_with_no_usable_side": result.rows_with_no_usable_side,
            "http_attempts": result.http_attempts,
            "retry_count": result.retry_count,
            "elapsed_seconds": result.elapsed_seconds,
            "cancelled": result.cancelled,
            "status": result.status,
            "history_sell_keys_requested": result.history_sell_keys_requested,
            "historical_sell_estimates_available": (result.historical_sell_estimates_available),
            "unresolved_sell_sides_after_history": (result.unresolved_sell_sides_after_history),
            "history_planned_batches": result.history_planned_batches,
            "history_successful_batches": result.history_successful_batches,
            "history_failed_batches": result.history_failed_batches,
        }

    def _stored_result(self, key: str) -> StoredMarketSyncResult | None:
        try:
            raw = self.settings.get(key)
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            started_at = datetime.fromisoformat(str(raw["started_at"]))
            completed_at = datetime.fromisoformat(str(raw["completed_at"]))
            if started_at.tzinfo is None or completed_at.tzinfo is None:
                return None
            return StoredMarketSyncResult(
                started_at=started_at.astimezone(UTC),
                completed_at=completed_at.astimezone(UTC),
                region=Region(str(raw["region"])),
                cities=tuple(str(city) for city in raw["cities"]),
                item_count=int(raw["item_count"]),
                planned_batches=int(raw["planned_batches"]),
                completed_batches=int(raw["completed_batches"]),
                successful_batches=int(raw["successful_batches"]),
                failed_batches=int(raw["failed_batches"]),
                rows_returned=int(raw["rows_returned"]),
                useful_sides_received=int(raw["useful_sides_received"]),
                sides_updated=int(raw.get("sides_updated", raw["useful_sides_received"])),
                missing_sides=int(raw.get("missing_sides", 0)),
                observations_le_2h=int(raw.get("observations_le_2h", 0)),
                observations_le_4h=int(raw.get("observations_le_4h", 0)),
                observations_le_24h=int(raw.get("observations_le_24h", 0)),
                observations_older_24h=int(raw.get("observations_older_24h", 0)),
                rows_with_no_usable_side=int(raw.get("rows_with_no_usable_side", 0)),
                http_attempts=int(raw.get("http_attempts", 0)),
                retry_count=int(raw.get("retry_count", 0)),
                elapsed_seconds=float(raw.get("elapsed_seconds", 0.0)),
                cancelled=bool(raw["cancelled"]),
                status=str(raw["status"]),
                history_sell_keys_requested=int(raw.get("history_sell_keys_requested", 0)),
                historical_sell_estimates_available=int(
                    raw.get("historical_sell_estimates_available", 0)
                ),
                unresolved_sell_sides_after_history=int(
                    raw.get("unresolved_sell_sides_after_history", 0)
                ),
                history_planned_batches=int(raw.get("history_planned_batches", 0)),
                history_successful_batches=int(raw.get("history_successful_batches", 0)),
                history_failed_batches=int(raw.get("history_failed_batches", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None
