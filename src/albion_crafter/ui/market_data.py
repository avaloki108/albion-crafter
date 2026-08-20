from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
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
from albion_crafter.market.aodp import (
    SAFE_AODP_URL_LENGTH,
    AODPClient,
    BatchFetchResult,
    BatchProgress,
    plan_price_requests,
)
from albion_crafter.market.cache import CachedMarketService
from albion_crafter.market.coverage import MarketCoverageService, MarketCoverageSummary
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
from albion_crafter.market.sync import (
    DEFAULT_ROYAL_SYNC_CITIES,
    OPTIONAL_ROYAL_SYNC_CITIES,
    MarketSyncStateRepository,
    RoyalMarketSyncProgress,
    RoyalMarketSyncResult,
    RoyalMarketSyncService,
    RoyalMarketUniverseService,
)

from .common import SortableItem, age_text, money
from .settings_view import DEFAULT_SETTINGS

MAX_CURRENT_ITEM_IDS = 500
MAX_HISTORY_ITEM_IDS = 100
MAX_HISTORY_CITIES = 3
MAX_HISTORY_WINDOW_DAYS = 30
HISTORY_RETENTION_DAYS = 30
MAX_MARKET_TABLE_ROWS = 1_000


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
    progress = Signal(object)
    error = Signal(str)


class MarketRefreshWorker(QRunnable):
    def __init__(
        self,
        region: Region,
        repository: MarketPriceRepository,
        item_ids: tuple[str, ...],
        *,
        client_factory: Callable[..., AODPClient] = AODPClient,
    ) -> None:
        super().__init__()
        self.region = region
        self.repository = repository
        self.item_ids = item_ids
        self.client_factory = client_factory
        self.signals = MarketWorkerSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        """Stop before the next bounded request; already saved batches remain cached."""

        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            plan = plan_price_requests(
                self.item_ids,
                region=self.region,
                cities=CITIES,
                qualities=(1,),
            )
            client = self.client_factory(self.region, max_batches=plan.batch_count)
            service = CachedMarketService(client, self.repository)
            result = service.refresh(
                self.item_ids,
                cities=CITIES,
                qualities=(1,),
                is_cancelled=self._cancelled.is_set,
                on_progress=self.signals.progress.emit,
            )
        except Exception as exc:  # worker boundary: errors must become visible status
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(
                len(result.records), len(result.failures), result.batch_count
            )
            self.signals.detailed.emit(result)


class RoyalMarketSyncSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
    error = Signal(str)


class RoyalMarketSyncWorker(QRunnable):
    def __init__(
        self,
        service: RoyalMarketSyncService,
        region: Region,
        cities: tuple[str, ...],
    ) -> None:
        super().__init__()
        self.service = service
        self.region = region
        self.cities = cities
        self.signals = RoyalMarketSyncSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            result = self.service.synchronize(
                self.region,
                self.cities,
                is_cancelled=self._cancelled.is_set,
                on_progress=self.signals.progress.emit,
            )
        except Exception as exc:  # worker boundary: errors must become visible status
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(result)


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
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

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
                is_cancelled=self._cancelled.is_set,
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
    COLUMN_WIDTHS = (240, 120, 70, 100, 190, 95, 100, 190, 95, 190, 120)

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
        self.universe_service = RoyalMarketUniverseService(catalog)
        self.sync_service = RoyalMarketSyncService(self.universe_service, repository)
        self.coverage_service = MarketCoverageService(repository)
        self.sync_state = MarketSyncStateRepository(settings)
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[QRunnable] = set()
        self._closing = False
        self._catalog_universe_before_update: int | None = None
        root = QVBoxLayout(self)
        title = QLabel("Market Data")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("dataBanner")
        root.addWidget(self.status)

        sync_group = QGroupBox("FULL ROYAL MARKET SYNC")
        sync_layout = QVBoxLayout(sync_group)
        sync_summary = QGridLayout()
        sync_summary.addWidget(QLabel("Region"), 0, 0)
        self.sync_region = QLabel()
        sync_summary.addWidget(self.sync_region, 0, 1)
        sync_summary.addWidget(QLabel("Market universe"), 1, 0)
        self.sync_universe = QLabel()
        self.sync_universe.setWordWrap(True)
        sync_summary.addWidget(self.sync_universe, 1, 1)
        sync_summary.addWidget(QLabel("Last full sync"), 2, 0)
        self.sync_last_completed = QLabel()
        self.sync_last_completed.setWordWrap(True)
        sync_summary.addWidget(self.sync_last_completed, 2, 1)
        sync_summary.addWidget(QLabel("Last sync result"), 3, 0)
        self.sync_last_result = QLabel()
        self.sync_last_result.setWordWrap(True)
        sync_summary.addWidget(self.sync_last_result, 3, 1)
        sync_layout.addLayout(sync_summary)

        cities_row = QHBoxLayout()
        cities_row.addWidget(QLabel("Market sync cities"))
        selected_cities = set(self.sync_state.cities())
        self.sync_city_checks: dict[str, QCheckBox] = {}
        for city in (*DEFAULT_ROYAL_SYNC_CITIES, *OPTIONAL_ROYAL_SYNC_CITIES):
            check = QCheckBox(city)
            check.setChecked(city in selected_cities)
            check.toggled.connect(self._save_selected_sync_cities)
            self.sync_city_checks[city] = check
            cities_row.addWidget(check)
        select_outer = QPushButton("Select all Outer Royals")
        select_outer.clicked.connect(self._select_outer_royals)
        cities_row.addWidget(select_outer)
        cities_row.addStretch(1)
        sync_layout.addLayout(cities_row)

        self.sync_progress = QProgressBar()
        self.sync_progress.setRange(0, 1)
        self.sync_progress.setValue(0)
        self.sync_progress.setFormat("Ready")
        sync_layout.addWidget(self.sync_progress)
        sync_buttons = QHBoxLayout()
        self.full_refresh_button = QPushButton("REFRESH ROYAL MARKETS")
        self.full_refresh_button.setToolTip(
            "Checks supported production outputs and their ingredients at Normal quality in "
            "the selected cities. Manual ID fields are ignored."
        )
        self.full_refresh_button.clicked.connect(self.refresh_all_from_network)
        self.cancel_sync_button = QPushButton("Cancel")
        self.cancel_sync_button.setEnabled(False)
        self.cancel_sync_button.clicked.connect(self.cancel_royal_sync)
        sync_buttons.addWidget(self.full_refresh_button, 1)
        sync_buttons.addWidget(self.cancel_sync_button)
        sync_layout.addLayout(sync_buttons)
        root.addWidget(sync_group)

        coverage_group = QGroupBox("ROYAL MARKET CACHE")
        coverage_layout = QVBoxLayout(coverage_group)
        self.coverage_summary = QLabel()
        self.coverage_summary.setWordWrap(True)
        coverage_layout.addWidget(self.coverage_summary)
        root.addWidget(coverage_group)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("Current-price IDs"))
        self.requested_items = QLineEdit("T4_MAIN_SWORD,T4_METALBAR,T4_LEATHER")
        self.requested_items.setPlaceholderText(
            "Type current-price item IDs here; history IDs below are separate"
        )
        actions.addWidget(self.requested_items, 1)
        self.refresh_button = QPushButton("Refresh current prices for IDs")
        self.refresh_button.clicked.connect(self.refresh_from_network)
        actions.addWidget(self.refresh_button)
        self.static_button = QPushButton("Update Static Game Data")
        self.static_button.setToolTip(
            "Updates recipes and Item Values only; it does not download market prices."
        )
        self.static_button.clicked.connect(self.update_static_data)
        actions.addWidget(self.static_button)
        root.addLayout(actions)
        market_note = QLabel(
            "Full and targeted current-price refreshes check AODP without re-dating observations. "
            "A row can remain Missing after a successful check when AODP has no reported player "
            "order. Static Item Value, station usage fees, and your Focus profile are separate "
            "inputs and cannot be supplied by the marketplace API."
        )
        market_note.setWordWrap(True)
        market_note.setObjectName("muted")
        root.addWidget(market_note)

        self.universe_group = QGroupBox("Advanced: Market universe inspection")
        self.universe_group.setCheckable(True)
        self.universe_group.setChecked(False)
        universe_layout = QVBoxLayout(self.universe_group)
        self.universe_note = QLabel()
        self.universe_note.setWordWrap(True)
        universe_layout.addWidget(self.universe_note)
        self.universe_table = QTableWidget(0, 5)
        self.universe_table.setHorizontalHeaderLabels(
            ("Canonical item ID", "Display name", "Tier", "Enchantment", "Reason included")
        )
        self.universe_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.universe_table.verticalHeader().setVisible(False)
        universe_layout.addWidget(self.universe_table)
        self.universe_group.toggled.connect(self._toggle_universe_inspection)
        self._toggle_universe_inspection(False)
        root.addWidget(self.universe_group)

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
        self.history_button = QPushButton("Refresh reported history (not current prices)")
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
        # Keeping a large, sortable QTableWidget in ResizeToContents mode makes Qt
        # recalculate every column width while it restores sorting after a refresh.
        # With a few thousand cached rows that blocks the GUI thread for minutes.
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate(self.COLUMN_WIDTHS):
            self.table.setColumnWidth(column, width)
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
        self._start_current_refresh(item_ids)

    def refresh_all_from_network(self) -> None:
        """Run the intentional, user-triggered Royal-market synchronization."""

        if self._closing:
            return
        if self._network_worker_running():
            self.status.setText(
                "Another network update is already running. Wait for it to finish or cancel the "
                "Royal Market Sync."
            )
            return
        cities = self._selected_sync_cities()
        if not cities:
            self.status.setText("Select at least one Royal market city before refreshing.")
            return
        if self.catalog.import_metadata() is None:
            self.status.setText(
                "No supported market universe is available. Update Static Game Data first."
            )
            return
        self.sync_state.save_cities(cities)
        region = self._region()
        self._set_network_running(True, royal_sync=True)
        self.sync_progress.setRange(0, 0)
        self.sync_progress.setFormat("Preparing bounded AODP request plan…")
        self.status.setText(
            f"Preparing supported market items from the catalog for {len(cities)} selected "
            f"cities on {region.display_name}. This is a user-triggered background sync; "
            "successful batches are saved immediately."
        )
        worker = RoyalMarketSyncWorker(self.sync_service, region, cities)
        self._workers.add(worker)
        worker.signals.progress.connect(
            lambda progress, w=worker: self._royal_sync_progress(w, progress)
        )
        worker.signals.finished.connect(
            lambda result, w=worker: self._royal_sync_finished(w, result)
        )
        worker.signals.error.connect(lambda message, w=worker: self._royal_sync_failed(w, message))
        self.thread_pool.start(worker)

    def cancel_royal_sync(self) -> None:
        worker = next(
            (active for active in self._workers if isinstance(active, RoyalMarketSyncWorker)),
            None,
        )
        if worker is None:
            return
        worker.cancel()
        self.cancel_sync_button.setEnabled(False)
        self.status.setText(
            "CANCELLING… Completed market batches have been kept. The current HTTP request will "
            "finish safely before additional work stops."
        )
        self.sync_progress.setFormat("Cancelling after current batch…")

    def _royal_sync_progress(
        self,
        worker: QRunnable,
        progress: RoyalMarketSyncProgress,
    ) -> None:
        if self._closing or worker not in self._workers:
            return
        self.sync_progress.setRange(0, progress.planned_batches)
        self.sync_progress.setValue(progress.completed_batches)
        self.sync_progress.setFormat(
            f"Batch {progress.completed_batches:,} / {progress.planned_batches:,}"
        )
        city_count = len(worker.cities) if isinstance(worker, RoyalMarketSyncWorker) else 0
        self.status.setText(
            f"Royal Market Sync — batch {progress.completed_batches:,}/"
            f"{progress.planned_batches:,}; {city_count} cities; "
            f"{progress.rows_received:,} rows received; "
            f"{progress.useful_sides_received:,} useful price sides received and "
            f"{progress.sides_updated:,} materially updated; "
            f"{progress.failed_batches:,} failed so far. Successful batches are already saved."
        )

    def _royal_sync_finished(
        self,
        worker: QRunnable,
        result: RoyalMarketSyncResult,
    ) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self.sync_state.save_result(result)
        self._set_network_running(False)
        self.sync_progress.setRange(0, max(result.planned_batches, 1))
        self.sync_progress.setValue(result.completed_batches)
        self.sync_progress.setFormat(
            "Cancelled — completed data kept"
            if result.cancelled
            else f"{result.successful_batches:,} successful / {result.planned_batches:,} planned"
        )
        self.status.setText(self._royal_sync_status(result))
        self.reload(update_status=False)
        self.data_changed.emit()

    @staticmethod
    def _royal_sync_status(result: RoyalMarketSyncResult) -> str:
        state = {
            "complete": "COMPLETE",
            "partial": "PARTIAL — retry the sync to revisit failed coverage",
            "failed": "FAILED — no batch completed successfully",
            "cancelled": "CANCELLED — completed data was kept",
        }[result.status]
        return (
            f"Royal Market Sync {state}: {result.item_count:,} supported items × "
            f"{result.city_count} cities; {result.successful_batches:,}/"
            f"{result.planned_batches:,} batches successful, {result.failed_batches:,} failed; "
            f"{result.rows_returned:,} rows returned; {result.useful_sides_received:,} useful "
            f"sides received, {result.sides_updated:,} materially updated, and "
            f"{result.missing_sides:,} missing sides; "
            f"{result.observations_le_2h:,} observations ≤2h, "
            f"{result.observations_le_4h:,} ≤4h, {result.observations_le_24h:,} ≤24h, "
            f"{result.observations_older_24h:,} >24h; "
            f"{result.rows_with_no_usable_side:,} requested item/city rows had no usable side "
            "returned; "
            f"{result.http_attempts:,} HTTP attempts, {result.retry_count:,} retries, "
            f"{len(result.record_failures):,} malformed rows skipped, maximum encoded URL "
            f"{result.max_url_bytes:,}/{SAFE_AODP_URL_LENGTH:,} bytes, "
            f"{result.elapsed_seconds:.2f}s. Fetching now "
            "never changes an older observation's "
            "timestamp or fabricates an absent order."
        )

    def _royal_sync_failed(self, worker: QRunnable, message: str) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._set_network_running(False)
        self.sync_progress.setRange(0, 1)
        self.sync_progress.setValue(0)
        self.sync_progress.setFormat("Stopped")
        self.status.setText(
            "Royal Market Sync stopped unexpectedly. Successful earlier batches remain cached "
            f"and existing observations were preserved. Error: {message}"
        )

    def _start_current_refresh(self, item_ids: tuple[str, ...]) -> None:
        if self._closing:
            return
        if self._network_worker_running():
            self.status.setText(
                "Another network update is already running. Wait for it to finish before "
                "refreshing the selected IDs."
            )
            return
        region = self._region()
        self._set_network_running(True)
        self.status.setText(
            f"Checking {len(item_ids):,} selected item IDs at Normal quality "
            f"across {len(CITIES)} supported cities on {region.display_name} AODP. "
            "Requests run sequentially in bounded background batches; every successful batch "
            "is saved immediately…"
        )
        worker = MarketRefreshWorker(region, self.repository, item_ids)
        self._workers.add(worker)
        worker.signals.progress.connect(
            lambda progress, w=worker: self._refresh_progress(w, progress)
        )
        worker.signals.detailed.connect(
            lambda result, w=worker: self._refresh_detailed_finished(w, result)
        )
        worker.signals.error.connect(lambda message, w=worker: self._refresh_failed(w, message))
        self.thread_pool.start(worker)

    def _refresh_progress(
        self,
        worker: QRunnable,
        progress: BatchProgress,
    ) -> None:
        if self._closing or worker not in self._workers:
            return
        outcome = "saved" if progress.successful else "failed"
        self.status.setText(
            f"Current-price refresh: batch {progress.completed_batches:,}/"
            f"{progress.batch_count:,} "
            f"{outcome}; latest batch returned {progress.records_returned:,} item/city rows. "
            "Successful batches are already persisted. The app remains usable while this runs."
        )

    def _refresh_detailed_finished(
        self,
        worker: QRunnable,
        result: BatchFetchResult,
    ) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._set_network_running(False)
        self.status.setText(self._current_refresh_status(result))
        self.reload(update_status=False)
        self.data_changed.emit()

    @staticmethod
    def _current_refresh_status(
        result: BatchFetchResult,
    ) -> str:
        sell_prices = sum(record.sell_price is not None for record in result.records)
        buy_prices = sum(record.buy_price is not None for record in result.records)
        empty_rows = sum(
            record.sell_price is None and record.buy_price is None for record in result.records
        )
        empty_note = (
            f" {empty_rows} returned item/city rows had no reported sell or buy order; "
            "a successful check cannot create a price when AODP has no observation."
            if empty_rows
            else ""
        )
        cancelled_note = (
            " Refresh was cancelled after the saved batches." if result.cancelled else ""
        )
        return (
            f"AODP current-price check: {result.items_requested:,} item IDs; "
            f"{result.http_batches} HTTP batches "
            f"({result.successful_batches} successful, {result.failed_batches} failed); "
            f"{sell_prices:,} sell-order prices and {buy_prices:,} buy-order prices returned "
            f"across {result.records_returned:,} item/city rows; "
            f"{len(result.record_failures)} malformed rows skipped; "
            f"{result.elapsed_seconds:.2f}s.{empty_note}{cancelled_note} Fetched (UTC) means AODP "
            "was checked; Sell/Buy Observation is the timestamp of an actual reported order."
        )

    def _refresh_failed(
        self,
        worker: QRunnable,
        message: str,
    ) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._set_network_running(False)
        self.status.setText(
            "Live market refresh stopped. Successful earlier batches remain saved and all prior "
            f"cache rows were preserved. Error: {message}"
        )

    def _network_worker_running(self) -> bool:
        return any(
            isinstance(
                worker,
                (
                    RoyalMarketSyncWorker,
                    MarketRefreshWorker,
                    HistoryRefreshWorker,
                    StaticDataWorker,
                ),
            )
            for worker in self._workers
        )

    def _set_network_running(self, running: bool, *, royal_sync: bool = False) -> None:
        if not running:
            self._set_network_buttons_idle()
            return
        self.refresh_button.setEnabled(False)
        self.full_refresh_button.setEnabled(False)
        self.history_button.setEnabled(False)
        self.static_button.setEnabled(False)
        self.cancel_sync_button.setEnabled(royal_sync)
        for check in self.sync_city_checks.values():
            check.setEnabled(False)

    def _set_network_buttons_idle(self) -> None:
        network_running = self._network_worker_running()
        self.refresh_button.setEnabled(not network_running)
        self.full_refresh_button.setEnabled(not network_running)
        self.history_button.setEnabled(not network_running and self.history is not None)
        self.static_button.setEnabled(not network_running)
        self.cancel_sync_button.setEnabled(False)
        for check in self.sync_city_checks.values():
            check.setEnabled(not network_running)

    def _selected_sync_cities(self) -> tuple[str, ...]:
        return tuple(city for city, check in self.sync_city_checks.items() if check.isChecked())

    def _select_outer_royals(self) -> None:
        for city, check in self.sync_city_checks.items():
            check.setChecked(city in DEFAULT_ROYAL_SYNC_CITIES)

    def _save_selected_sync_cities(self) -> None:
        cities = self._selected_sync_cities()
        if not cities:
            return
        try:
            self.sync_state.save_cities(cities)
        except (TypeError, ValueError):
            return

    def refresh_history_from_network(self) -> None:
        if self._network_worker_running():
            self.status.setText(
                "Another network update is already running. Wait for it to finish before "
                "refreshing history."
            )
            return
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
        self._set_network_running(True)
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
        if self._closing:
            return
        self._set_network_buttons_idle()
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
        # History refresh does not change the current top-of-book table. Rebuilding
        # thousands of unrelated rows here used to make a completed request look frozen.
        self.data_changed.emit()

    def _history_failed(self, worker: QRunnable, message: str) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._set_network_buttons_idle()
        self.status.setText(
            "History refresh could not complete. Previously cached history remains available; "
            "the current-price cache was not changed. Error: " + message
        )

    def update_static_data(self) -> None:
        if self._network_worker_running():
            self.status.setText(
                "Another network update is already running. Wait for it to finish before "
                "updating static game data."
            )
            return
        self._catalog_universe_before_update = self.universe_service.derive().item_count
        self._set_network_running(True)
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
        if self._closing:
            return
        self._set_network_buttons_idle()
        previous_universe = self._catalog_universe_before_update
        self._catalog_universe_before_update = None
        self.universe_service.invalidate()
        universe = self.universe_service.derive()
        tracked = set(self.repository.list_tracked_item_ids(self._region()))
        untracked = len(set(universe.item_ids) - tracked)
        message = (
            f"Static import complete: {items:,} items and {recipes:,} craftable variants "
            f"from commit {version}. Market universe: "
            f"{previous_universe if previous_universe is not None else 0:,} → "
            f"{universe.item_count:,}; {untracked:,} current universe items have no cached row. "
            f"Existing healthy market prices were preserved. {self._catalog_health_text()}"
        )
        self.status.setText(message)
        self._refresh_sync_dashboard()
        self.catalog_changed.emit()

    def _static_failed(self, worker: QRunnable, message: str) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._catalog_universe_before_update = None
        self._set_network_buttons_idle()
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
        total_records = self.repository.count(region)
        records = self.repository.list_for_display(region, limit=MAX_MARKET_TABLE_ROWS)
        sorting_enabled = self.table.isSortingEnabled()
        self.table.setUpdatesEnabled(False)
        try:
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
            self.table.setSortingEnabled(sorting_enabled)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.viewport().update()

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
        self._refresh_sync_dashboard()
        if update_status:
            self.status.setText(
                f"{self._market_health_text(records, policy, total_rows=total_records)}; "
                f"{len(overrides)} separate user overrides for {region.display_name}; "
                f"{self._catalog_health_text()}; {self._history_health_text(region)}."
            )

    def _refresh_sync_dashboard(self) -> None:
        region = self._region()
        universe = self.universe_service.derive()
        cities = self._selected_sync_cities()
        self.sync_region.setText(region.display_name)
        self.sync_universe.setText(
            f"{universe.item_count:,} deduplicated market IDs from "
            f"{universe.supported_output_items:,} supported production outputs and "
            f"{universe.required_ingredient_items:,} required ingredient IDs "
            f"({universe.total_catalog_items:,} total catalog records audited)."
        )
        last = self.sync_state.last_result()
        if last is None:
            self.sync_last_completed.setText("Never")
            self.sync_last_result.setText(
                "No full sync has run. Startup stays offline; press REFRESH ROYAL MARKETS when "
                "you want a sync."
            )
        else:
            self.sync_last_completed.setText(last.completed_at.isoformat())
            self.sync_last_result.setText(
                f"{last.status} · {last.item_count:,} items · {len(last.cities)} cities · "
                f"{last.successful_batches:,}/"
                f"{last.planned_batches:,} batches · {last.sides_updated:,} sides updated."
            )
        if not cities or not universe.item_ids:
            self.coverage_summary.setText(
                "Coverage is unavailable until catalog items and cities exist."
            )
            return
        coverage = self.coverage_service.summary(
            region,
            cities,
            universe.item_ids,
            as_of=datetime.now(UTC),
        )
        self.coverage_summary.setText(self._coverage_text(coverage))
        if self.universe_group.isChecked():
            self._populate_universe_table(universe)

    @staticmethod
    def _coverage_text(summary: MarketCoverageSummary) -> str:
        city_lines = " · ".join(
            f"{city.city}: {city.coverage_4h_percent:.1f}% rows ≤4h"
            for city in summary.city_coverage
        )
        return (
            f"Items tracked: {summary.item_count:,} · Cities: {summary.city_count} · "
            f"Expected item/city rows: {summary.expected_rows:,} · "
            f"Cached rows: {summary.cached_rows:,}\n"
            f"Price-side observations ≤2h: {summary.observations_le_2h:,} · "
            f"≤4h: {summary.observations_le_4h:,} · "
            f"≤24h: {summary.observations_le_24h:,} · "
            f">24h: {summary.observations_older_24h:,} · "
            f"Missing sides: {summary.missing_sides:,} · "
            f"Rows with no usable price: {summary.rows_with_no_usable_price:,}\n" + city_lines
        )

    def _toggle_universe_inspection(self, visible: bool) -> None:
        self.universe_note.setVisible(visible)
        self.universe_table.setVisible(visible)
        if visible:
            self._populate_universe_table(self.universe_service.derive())

    def _populate_universe_table(self, universe) -> None:
        display_limit = 500
        shown = universe.items[:display_limit]
        self.universe_note.setText(
            f"Showing {len(shown):,} of {universe.item_count:,} included IDs. The display cap is "
            "not a synchronization cap."
        )
        self.universe_table.setUpdatesEnabled(False)
        try:
            self.universe_table.setRowCount(len(shown))
            for row, entry in enumerate(shown):
                values = (
                    entry.item.item_id,
                    entry.item.display_name,
                    "Unknown" if entry.item.tier is None else str(entry.item.tier),
                    str(entry.item.enchantment),
                    " + ".join(entry.reasons),
                )
                for column, value in enumerate(values):
                    self.universe_table.setItem(row, column, SortableItem(value, value))
        finally:
            self.universe_table.setUpdatesEnabled(True)
        self.universe_table.resizeColumnsToContents()

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
        *,
        total_rows: int | None = None,
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
        total = len(records) if total_rows is None else total_rows
        scope = (
            f"Current cache {total:,} rows"
            if total == len(records)
            else f"Showing {len(records):,} useful rows from {total:,} cached rows"
        )
        return (
            f"{scope} / {len(records) * 2:,} displayed sides: "
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

    def shutdown(self) -> None:
        """Cancel bounded network work and detach callbacks before widget destruction."""

        if self._closing:
            return
        self._closing = True
        for worker in tuple(self._workers):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
            signals = getattr(worker, "signals", None)
            if signals is None:
                continue
            if isinstance(worker, MarketRefreshWorker):
                names = ("progress", "detailed", "error")
            elif isinstance(worker, RoyalMarketSyncWorker):
                names = ("progress", "finished", "error")
            else:
                names = ("finished", "error")
            for name in names:
                signal = getattr(signals, name, None)
                if signal is None:
                    continue
                try:
                    signal.disconnect()
                except RuntimeError:
                    pass
        self._workers.clear()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)
