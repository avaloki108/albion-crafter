from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from albion_crafter.data.cities import CITIES
from albion_crafter.data.static_importer import StaticDataClient
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
    default_data_directory,
)
from albion_crafter.database.v3 import HistoryCoverage, MarketHistoryRepository
from albion_crafter.market.aodp import AODPClient, BatchFetchResult
from albion_crafter.market.cache import CachedMarketService
from albion_crafter.market.history import (
    AODPHistoryClient,
    HistoryFetchResult,
    HistoryTimeScale,
)
from albion_crafter.market.models import (
    Freshness,
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)

from .common import SortableItem, age_text, money
from .settings_view import DEFAULT_SETTINGS

MAX_CURRENT_ITEM_IDS = 500
MAX_HISTORY_ITEM_IDS = 100
MAX_HISTORY_CITIES = 3
MAX_HISTORY_WINDOW_DAYS = 30
HISTORY_RETENTION_DAYS = 30


def _normalized_city(value: str) -> str:
    return value.replace(" ", "").replace("'", "").casefold()


@dataclass(frozen=True, slots=True)
class HistoryRefreshSummary:
    result: HistoryFetchResult
    coverage_total: int
    success_coverage: int
    empty_coverage: int
    partial_coverage: int
    failed_coverage: int
    pruned_intervals: int


class MarketWorkerSignals(QObject):
    finished = Signal(int, int, int)
    detailed = Signal(object)
    error = Signal(str)


class MarketRefreshWorker(QRunnable):
    def __init__(
        self,
        region: Region,
        repository: MarketPriceRepository,
        item_ids: tuple[str, ...],
    ) -> None:
        super().__init__()
        self.region = region
        self.repository = repository
        self.item_ids = item_ids
        self.signals = MarketWorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            service = CachedMarketService(AODPClient(self.region), self.repository)
            result = service.refresh(self.item_ids, cities=CITIES, qualities=(1,))
        except Exception as exc:  # worker boundary: errors must become visible status
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(
                len(result.records), len(result.failures), result.batch_count
            )
            self.signals.detailed.emit(result)


class HistoryWorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class HistoryRefreshWorker(QRunnable):
    """Fetch and persist an explicit, bounded six-hour history window."""

    def __init__(
        self,
        region: Region,
        repository: MarketHistoryRepository,
        item_ids: tuple[str, ...],
        cities: tuple[str, ...],
        window_days: int,
        *,
        client_factory: Callable[[Region], AODPHistoryClient] = AODPHistoryClient,
    ) -> None:
        super().__init__()
        self.region = region
        self.repository = repository
        self.item_ids = item_ids
        self.cities = cities
        self.window_days = window_days
        self.client_factory = client_factory
        self.signals = HistoryWorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            requested_at = datetime.now(UTC)
            end_date = requested_at.date()
            start_date = end_date - timedelta(days=self.window_days)
            result = self.client_factory(self.region).fetch_history(
                self.item_ids,
                start_date=start_date,
                end_date=end_date,
                cities=self.cities,
                qualities=(1,),
                time_scale=HistoryTimeScale.SIX_HOURLY,
            )
            completed_at = datetime.now(UTC)
            self.repository.upsert_many(result.intervals)
            coverage_counts = self._store_coverage(result, completed_at)
            pruned = self.repository.prune_before(
                completed_at - timedelta(days=HISTORY_RETENTION_DAYS)
            )
            summary = HistoryRefreshSummary(
                result=result,
                coverage_total=sum(coverage_counts.values()),
                success_coverage=coverage_counts["success"],
                empty_coverage=coverage_counts["empty"],
                partial_coverage=coverage_counts["partial"],
                failed_coverage=coverage_counts["failed"],
                pruned_intervals=pruned,
            )
        except Exception as exc:  # worker boundary: errors must become visible status
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(summary)

    def _store_coverage(
        self,
        result: HistoryFetchResult,
        fetched_at: datetime,
    ) -> Counter[str]:
        retention_start = fetched_at - timedelta(days=HISTORY_RETENTION_DAYS)
        record_counts: Counter[tuple[str, str]] = Counter(
            (interval.item_id.casefold(), _normalized_city(interval.city))
            for interval in result.intervals
            if interval.quality == 1 and interval.observed_at >= retention_start
        )
        failed_messages: dict[str, list[str]] = {}
        for failure in result.failures:
            for item_id in failure.item_ids:
                failed_messages.setdefault(item_id.casefold(), []).append(failure.message)

        malformed_message = None
        if result.record_failures:
            malformed_message = (
                f"{len(result.record_failures)} malformed history rows/series were skipped"
            )

        window_start = max(
            datetime.combine(result.start_date, time.min, tzinfo=UTC),
            retention_start,
        )
        window_end = min(
            datetime.combine(result.end_date, time.max, tzinfo=UTC),
            fetched_at,
        )
        statuses: Counter[str] = Counter()
        for item_id in self.item_ids:
            item_key = item_id.casefold()
            for city in self.cities:
                count = record_counts[(item_key, _normalized_city(city))]
                failures = failed_messages.get(item_key)
                if failures:
                    status = "failed"
                    error_message = "; ".join(dict.fromkeys(failures))
                elif malformed_message is not None:
                    # AODP row failures do not identify a trustworthy series key. Conservatively
                    # mark all otherwise-successful keys incomplete instead of claiming coverage.
                    status = "partial"
                    error_message = malformed_message
                elif count:
                    status = "success"
                    error_message = None
                else:
                    # A successful HTTP response with no valid points is evidence of an empty
                    # window, not a transport failure and not zero market activity.
                    status = "empty"
                    error_message = None
                self.repository.set_coverage(
                    HistoryCoverage(
                        region=self.region,
                        item_id=item_id,
                        city=city,
                        quality=1,
                        time_scale=HistoryTimeScale.SIX_HOURLY,
                        window_start=window_start,
                        window_end=window_end,
                        fetched_at=fetched_at,
                        status=status,
                        record_count=count,
                        error_message=error_message,
                    )
                )
                statuses[status] += 1
        return statuses


class StaticWorkerSignals(QObject):
    finished = Signal(int, int, str)
    error = Signal(str)


class StaticDataWorker(QRunnable):
    def __init__(self, repository: CatalogRepository) -> None:
        super().__init__()
        self.repository = repository
        self.signals = StaticWorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            metadata = StaticDataClient().update_catalog(
                self.repository, default_data_directory() / "static-cache"
            )
        except Exception as exc:  # worker boundary: errors must become visible status
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(
                metadata.item_count, metadata.recipe_count, metadata.source_version
            )


class MarketDataView(QWidget):
    data_changed = Signal()
    catalog_changed = Signal()

    HEADERS = (
        "Item ID",
        "City",
        "Quality",
        "Sell Min",
        "Sell Observation (UTC)",
        "Sell Age",
        "Buy Max",
        "Buy Observation (UTC)",
        "Buy Age",
        "Fetched (UTC)",
        "Provenance",
    )

    def __init__(
        self,
        repository: MarketPriceRepository,
        overrides: PriceOverrideRepository,
        catalog: CatalogRepository,
        settings: SettingsRepository,
        history: MarketHistoryRepository | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.overrides = overrides
        self.catalog = catalog
        self.settings = settings
        self.history = history
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[QRunnable] = set()
        root = QVBoxLayout(self)
        title = QLabel("Market Data")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("dataBanner")
        root.addWidget(self.status)

        actions = QHBoxLayout()
        self.requested_items = QLineEdit("T4_MAIN_SWORD,T4_METALBAR,T4_LEATHER")
        self.requested_items.setPlaceholderText("Comma-separated canonical Albion item IDs")
        actions.addWidget(self.requested_items, 1)
        self.refresh_button = QPushButton("Refresh listed IDs from AODP")
        self.refresh_button.clicked.connect(self.refresh_from_network)
        actions.addWidget(self.refresh_button)
        self.static_button = QPushButton("Update Static Game Data")
        self.static_button.clicked.connect(self.update_static_data)
        actions.addWidget(self.static_button)
        root.addLayout(actions)

        history_group = QGroupBox("Optional AODP reported-activity history")
        history_layout = QFormLayout(history_group)
        self.history_items = QLineEdit("T4_MAIN_SWORD,T4_BAG")
        self.history_items.setPlaceholderText("Comma-separated canonical output item IDs")
        self.history_cities = QLineEdit("Bridgewatch")
        self.history_cities.setPlaceholderText("Up to 3 comma-separated Royal Cities")
        self.history_window = QSpinBox()
        self.history_window.setRange(1, MAX_HISTORY_WINDOW_DAYS)
        self.history_window.setValue(7)
        self.history_window.setSuffix(" days")
        self.history_button = QPushButton("Refresh six-hour history")
        self.history_button.clicked.connect(self.refresh_history_from_network)
        history_layout.addRow("Item IDs", self.history_items)
        history_layout.addRow("Cities", self.history_cities)
        history_layout.addRow("Window", self.history_window)
        history_layout.addRow(self.history_button)
        history_note = QLabel(
            f"Manual only: at most {MAX_HISTORY_ITEM_IDS} IDs, {MAX_HISTORY_CITIES} cities, "
            f"and {MAX_HISTORY_WINDOW_DAYS} days per request. Cached intervals older than "
            f"{HISTORY_RETENTION_DAYS} days are pruned. Reported activity is not order-book "
            "depth."
        )
        history_note.setWordWrap(True)
        history_layout.addRow(history_note)
        if self.history is None:
            self.history_button.setEnabled(False)
            history_group.setToolTip(
                "No history repository was supplied; current-price refresh remains available."
            )
        root.addWidget(history_group)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table)

        override_group = QGroupBox("User price override (kept separate from AODP history)")
        override_layout = QFormLayout(override_group)
        self.override_item = QLineEdit()
        self.override_city = QComboBox()
        self.override_city.addItems(CITIES)
        self.override_quality = QSpinBox()
        self.override_quality.setRange(1, 5)
        self.override_side = QComboBox()
        self.override_side.addItem("Material/output sell order", MarketSide.SELL_ORDER.value)
        self.override_side.addItem("Instant-sale buy order", MarketSide.BUY_ORDER.value)
        self.override_price = QSpinBox()
        self.override_price.setRange(1, 2_000_000_000)
        override_layout.addRow("Item ID", self.override_item)
        override_layout.addRow("City", self.override_city)
        override_layout.addRow("Quality", self.override_quality)
        override_layout.addRow("Market side", self.override_side)
        override_layout.addRow("Observed price", self.override_price)
        buttons = QHBoxLayout()
        set_button = QPushButton("Set override")
        set_button.clicked.connect(self.set_override)
        remove_button = QPushButton("Remove override")
        remove_button.clicked.connect(self.remove_override)
        buttons.addWidget(set_button)
        buttons.addWidget(remove_button)
        override_layout.addRow(buttons)
        self.override_table = QTableWidget(0, 7)
        self.override_table.setHorizontalHeaderLabels(
            ("Item", "City", "Quality", "Side", "Price", "Entered (UTC)", "Provenance")
        )
        self.override_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        override_layout.addRow(self.override_table)
        root.addWidget(override_group)
        self.reload()

    def _region(self) -> Region:
        return Region(str(self.settings.get("region", DEFAULT_SETTINGS["region"])))

    def refresh_from_network(self) -> None:
        item_ids = tuple(
            dict.fromkeys(
                value.strip() for value in self.requested_items.text().split(",") if value.strip()
            )
        )
        if not item_ids:
            self.status.setText("Enter one or more canonical item IDs before refreshing.")
            return
        if len(item_ids) > MAX_CURRENT_ITEM_IDS:
            self.status.setText(
                f"A manual GUI refresh is limited to {MAX_CURRENT_ITEM_IDS} distinct item IDs."
            )
            return
        region = self._region()
        self.refresh_button.setEnabled(False)
        self.status.setText(
            f"Requesting {len(item_ids)} item IDs for all Royal Cities from "
            f"{region.display_name} AODP in bounded background batches…"
        )
        worker = MarketRefreshWorker(region, self.repository, item_ids)
        self._workers.add(worker)
        worker.signals.detailed.connect(
            lambda result, w=worker: self._refresh_detailed_finished(w, result)
        )
        worker.signals.error.connect(lambda message, w=worker: self._refresh_failed(w, message))
        self.thread_pool.start(worker)

    def _refresh_detailed_finished(
        self,
        worker: QRunnable,
        result: BatchFetchResult,
    ) -> None:
        self._workers.discard(worker)
        self.refresh_button.setEnabled(True)
        self.status.setText(
            f"AODP current refresh: {result.items_requested} item IDs; "
            f"{result.http_batches} HTTP batches "
            f"({result.successful_batches} successful, {result.failed_batches} failed); "
            f"{result.records_returned} valid records; "
            f"{len(result.record_failures)} malformed rows skipped; "
            f"{result.elapsed_seconds:.2f}s. Successful observations were merged side-by-side; "
            "existing cache timestamps were preserved where incoming data was missing or older."
        )
        self.reload(update_status=False)
        self.data_changed.emit()

    def _refresh_finished(
        self,
        worker: QRunnable,
        records: int,
        failures: int,
        batches: int,
    ) -> None:
        self._workers.discard(worker)
        self.refresh_button.setEnabled(True)
        self.status.setText(
            f"AODP refresh: {records} records from {batches - failures}/{batches} successful "
            f"batches; {failures} failed. Successful observations were merged side-by-side; "
            "existing cache timestamps were preserved where incoming data was missing or older."
        )
        self.reload(update_status=False)
        self.data_changed.emit()

    def _refresh_failed(self, worker: QRunnable, message: str) -> None:
        self._workers.discard(worker)
        self.refresh_button.setEnabled(True)
        self.status.setText(
            "Live market refresh failed. Existing cache remains unchanged. Error: " + message
        )

    def refresh_history_from_network(self) -> None:
        if self.history is None:
            self.status.setText(
                "History refresh is unavailable because no history repository was supplied."
            )
            return
        item_ids = self._history_item_values(self.history_items.text())
        if not item_ids:
            self.status.setText("Enter one or more canonical item IDs before refreshing history.")
            return
        if len(item_ids) > MAX_HISTORY_ITEM_IDS:
            self.status.setText(
                f"A manual history refresh is limited to {MAX_HISTORY_ITEM_IDS} distinct item IDs."
            )
            return
        cities, unknown_cities = self._history_city_values(self.history_cities.text())
        if unknown_cities:
            self.status.setText(
                "Unknown history cities: "
                + ", ".join(unknown_cities)
                + ". Use Royal City names from the application."
            )
            return
        if not cities:
            self.status.setText("Enter one or more Royal Cities before refreshing history.")
            return
        if len(cities) > MAX_HISTORY_CITIES:
            self.status.setText(
                f"A manual history refresh is limited to {MAX_HISTORY_CITIES} distinct cities."
            )
            return

        region = self._region()
        window_days = self.history_window.value()
        self.history_button.setEnabled(False)
        self.status.setText(
            f"Requesting {window_days} days of six-hour reported activity for "
            f"{len(item_ids)} item IDs in {len(cities)} cities from "
            f"{region.display_name} AODP in bounded background batches…"
        )
        worker = HistoryRefreshWorker(
            region,
            self.history,
            item_ids,
            cities,
            window_days,
        )
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda summary, w=worker: self._history_finished(w, summary)
        )
        worker.signals.error.connect(lambda message, w=worker: self._history_failed(w, message))
        self.thread_pool.start(worker)

    def _history_finished(
        self,
        worker: QRunnable,
        summary: HistoryRefreshSummary,
    ) -> None:
        self._workers.discard(worker)
        self.history_button.setEnabled(self.history is not None)
        result = summary.result
        self.status.setText(
            f"AODP history refresh: {result.items_requested} item IDs; "
            f"{result.http_batches} HTTP batches "
            f"({result.successful_batches} successful, {result.failed_batches} failed); "
            f"{result.records_returned} valid six-hour intervals; "
            f"{len(result.record_failures)} malformed rows/series skipped; "
            f"{result.elapsed_seconds:.2f}s. Coverage: {summary.success_coverage} successful, "
            f"{summary.empty_coverage} successful-empty, {summary.partial_coverage} partial, "
            f"{summary.failed_coverage} failed ({summary.coverage_total} keys). "
            f"Pruned {summary.pruned_intervals} intervals older than "
            f"{HISTORY_RETENTION_DAYS} days."
        )
        self.reload(update_status=False)
        self.data_changed.emit()

    def _history_failed(self, worker: QRunnable, message: str) -> None:
        self._workers.discard(worker)
        self.history_button.setEnabled(self.history is not None)
        self.status.setText(
            "History refresh could not complete. Previously cached history remains available; "
            "the current-price cache was not changed. Error: " + message
        )

    def update_static_data(self) -> None:
        self.static_button.setEnabled(False)
        self.status.setText(
            "Checking the maintained ao-data/ao-bin-dumps release and importing it in the "
            "background. Cached release files will be reused when possible…"
        )
        worker = StaticDataWorker(self.catalog)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda items, recipes, version, w=worker: self._static_finished(
                w, items, recipes, version
            )
        )
        worker.signals.error.connect(lambda message, w=worker: self._static_failed(w, message))
        self.thread_pool.start(worker)

    def _static_finished(self, worker: QRunnable, items: int, recipes: int, version: str) -> None:
        self._workers.discard(worker)
        self.static_button.setEnabled(True)
        self.status.setText(
            f"Static import complete: {items:,} items and {recipes:,} craftable variants "
            f"from commit {version}. {self._catalog_health_text()}"
        )
        self.catalog_changed.emit()

    def _static_failed(self, worker: QRunnable, message: str) -> None:
        self._workers.discard(worker)
        self.static_button.setEnabled(True)
        self.status.setText(
            "Static-data update failed. The previously imported catalog remains intact. Error: "
            + message
            + ". "
            + self._catalog_health_text()
        )

    def set_override(self) -> None:
        item_id = self.override_item.text().strip()
        if not item_id:
            self.status.setText("An item ID is required for a price override.")
            return
        self.overrides.set(
            UserPriceOverride(
                item_id=item_id,
                city=self.override_city.currentText(),
                quality=self.override_quality.value(),
                region=self._region(),
                side=MarketSide(str(self.override_side.currentData())),
                price=self.override_price.value(),
                entered_at=datetime.now(UTC),
            )
        )
        self.status.setText("User override saved without changing AODP cache history.")
        self.reload(update_status=False)
        self.data_changed.emit()

    def remove_override(self) -> None:
        removed = self.overrides.remove(
            self.override_item.text().strip(),
            self.override_city.currentText(),
            self.override_quality.value(),
            self._region(),
            MarketSide(str(self.override_side.currentData())),
        )
        self.status.setText("User override removed." if removed else "No matching override found.")
        self.reload(update_status=False)
        if removed:
            self.data_changed.emit()

    def reload(self, *, update_status: bool = True) -> None:
        region = self._region()
        policy = FreshnessPolicy(
            timedelta(
                hours=int(
                    self.settings.get(
                        "max_market_age_hours", DEFAULT_SETTINGS["max_market_age_hours"]
                    )
                )
            )
        )
        records = self.repository.list_all(region)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            sell_state = policy.classify(record.sell_price_timestamp)
            buy_state = policy.classify(record.buy_price_timestamp)
            values = (
                SortableItem(record.item_id, record.item_id),
                SortableItem(record.city, record.city),
                SortableItem(str(record.quality), record.quality),
                SortableItem(money(record.sell_price), record.sell_price),
                self._timestamp_item(record.sell_price_timestamp),
                SortableItem(
                    age_text(record.sell_price_timestamp),
                    self._sort_timestamp(record.sell_price_timestamp),
                ),
                SortableItem(money(record.buy_price), record.buy_price),
                self._timestamp_item(record.buy_price_timestamp),
                SortableItem(
                    age_text(record.buy_price_timestamp),
                    self._sort_timestamp(record.buy_price_timestamp),
                ),
                self._timestamp_item(record.fetched_at),
                SortableItem(record.provenance.value, record.provenance.value),
            )
            for column, item in enumerate(values):
                state = (
                    sell_state
                    if column in (3, 4, 5)
                    else buy_state
                    if column in (6, 7, 8)
                    else None
                )
                if state in {Freshness.STALE, Freshness.FUTURE}:
                    item.setForeground(QColor("#ff6b6b"))
                elif state in (Freshness.AGING, Freshness.UNKNOWN):
                    item.setForeground(QColor("#ffb454"))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)

        overrides = self.overrides.list_all(region)
        self.override_table.setRowCount(len(overrides))
        for row, override in enumerate(overrides):
            values = (
                override.item_id,
                override.city,
                str(override.quality),
                override.side.value,
                money(override.price),
                override.entered_at.isoformat(),
                override.provenance.value,
            )
            for column, value in enumerate(values):
                self.override_table.setItem(row, column, SortableItem(value, value))
        self.override_table.resizeColumnsToContents()
        if update_status:
            self.status.setText(
                f"{self._market_health_text(records, policy)}; "
                f"{len(overrides)} separate user overrides for {region.display_name}; "
                f"{self._catalog_health_text()}; {self._history_health_text(region)}."
            )

    @staticmethod
    def _csv_values(text: str) -> tuple[str, ...]:
        values: list[str] = []
        seen: set[str] = set()
        for raw_value in text.split(","):
            value = raw_value.strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                values.append(value)
        return tuple(values)

    @classmethod
    def _history_item_values(cls, text: str) -> tuple[str, ...]:
        return tuple(value.upper() for value in cls._csv_values(text))

    @classmethod
    def _history_city_values(cls, text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        known = {_normalized_city(city): city for city in CITIES}
        cities: list[str] = []
        unknown: list[str] = []
        for value in cls._csv_values(text):
            canonical = known.get(_normalized_city(value))
            if canonical is None:
                unknown.append(value)
            elif canonical not in cities:
                cities.append(canonical)
        return tuple(cities), tuple(unknown)

    @staticmethod
    def _market_health_text(
        records: Sequence[MarketPrice],
        policy: FreshnessPolicy,
    ) -> str:
        states: Counter[str] = Counter()
        for record in records:
            for price, timestamp in (
                (record.sell_price, record.sell_price_timestamp),
                (record.buy_price, record.buy_price_timestamp),
            ):
                if price is None:
                    states["missing"] += 1
                else:
                    states[policy.classify(timestamp).value.casefold()] += 1
        latest_fetch = max((record.fetched_at for record in records), default=None)
        latest_text = latest_fetch.isoformat() if latest_fetch is not None else "never"
        return (
            f"Current cache {len(records)} rows / {len(records) * 2} sides: "
            f"{states['fresh']} fresh, {states['aging']} aging, {states['stale']} stale, "
            f"{states['future']} future-dated (invalid), {states['missing']} missing, "
            f"{states['unknown']} unknown timestamp; "
            f"latest fetch {latest_text}"
        )

    def _catalog_health_text(self) -> str:
        metadata = self.catalog.import_metadata()
        if metadata is None:
            active = "active static catalog missing"
        else:
            active = (
                f"active static catalog {metadata.source_version[:12]} "
                f"({metadata.item_count:,} items, {metadata.recipe_count:,} recipes)"
            )

        latest_report = getattr(self.catalog, "latest_import_report", None)
        if latest_report is None:
            return active + "; no import validation report"
        report = latest_report()
        if report is None:
            return active + "; no import validation report"

        validation = report.validation_status.replace("_", " ")
        activation = "activated" if report.activated else "rejected; active catalog preserved"
        messages = ""
        if report.validation_messages:
            shown = "; ".join(report.validation_messages[:2])
            remainder = len(report.validation_messages) - 2
            messages = f"; diagnostics: {shown}"
            if remainder > 0:
                messages += f" (+{remainder} more)"
        return (
            f"{active}; latest import {validation}, {activation}: "
            f"{report.ingredient_count:,} ingredients, "
            f"{report.unknown_returnability_count:,} unknown returnability, "
            f"{report.skipped_malformed_count:,} malformed skipped{messages}"
        )

    def _history_health_text(self, region: Region) -> str:
        if self.history is None:
            return "history cache not configured"
        list_coverage = getattr(self.history, "list_coverage", None)
        if list_coverage is None:
            return "history repository does not expose coverage diagnostics"
        item_ids = self._history_item_values(self.history_items.text())[:MAX_HISTORY_ITEM_IDS]
        cities, unknown_cities = self._history_city_values(self.history_cities.text())
        cities = cities[:MAX_HISTORY_CITIES]
        if not item_ids or not cities:
            return "history health awaiting a valid item/city scope"
        if unknown_cities:
            return "history health unavailable for the invalid city scope"
        try:
            coverage = list_coverage(
                region,
                item_ids,
                cities,
                1,
                HistoryTimeScale.SIX_HOURLY,
            )
        except Exception as exc:  # health display must not prevent offline startup
            return f"history coverage diagnostics unavailable: {exc}"
        statuses = Counter(row.status for row in coverage)
        expected = len(item_ids) * len(cities)
        missing = max(expected - len(coverage), 0)
        reported_intervals = sum(row.record_count for row in coverage)
        latest = max((row.fetched_at for row in coverage), default=None)
        latest_text = latest.isoformat() if latest is not None else "never"
        return (
            f"selected history scope {reported_intervals:,} reported intervals / "
            f"{len(coverage)}/{expected} coverage keys: {statuses['success']} successful, "
            f"{statuses['empty']} empty, {statuses['partial']} partial, "
            f"{statuses['failed']} failed, {missing} never fetched; latest {latest_text}"
        )

    @staticmethod
    def _sort_timestamp(value: datetime | None) -> float | None:
        return value.timestamp() if value else None

    @classmethod
    def _timestamp_item(cls, value: datetime | None) -> SortableItem:
        return SortableItem(value.isoformat() if value else "Missing", cls._sort_timestamp(value))
