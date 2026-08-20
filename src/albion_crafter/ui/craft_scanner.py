from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from albion_crafter.core.city_bonuses import VERIFIED_CRAFTING_GROUPS
from albion_crafter.core.freshness import DEFAULT_CLOCK_SKEW_TOLERANCE
from albion_crafter.data.cities import CITIES
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import SettingsRepository
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Freshness, Region
from albion_crafter.opportunity.models import (
    CraftOpportunity,
    OpportunitySort,
    ScanConstraints,
    ScanProgress,
    ScanSnapshot,
)
from albion_crafter.opportunity.service import OpportunityScannerService

from .common import SortableItem, money, percent
from .scan_worker import OpportunityScanWorker
from .settings_view import DEFAULT_SETTINGS


class CraftScannerView(QWidget):
    """Background frontend for the GUI-independent opportunity service."""

    DISPLAY_LIMIT = 500
    HEADERS = (
        "Item",
        "Tier",
        "Enchant",
        "Buy city",
        "Craft",
        "Sell",
        "Profit",
        "ROI",
        "Margin",
        "Pre-revenue cash",
        "Station cost",
        "RRR",
        "Focus",
        "Silver / Focus",
        "Liquidity",
        "Oldest price",
        "Actionability",
    )

    def __init__(
        self,
        service: OpportunityScannerService,
        settings: SettingsRepository,
        catalog: CatalogRepository,
    ) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self.catalog = catalog
        self.thread_pool = QThreadPool.globalInstance()
        self.worker: OpportunityScanWorker | None = None
        self.snapshot: ScanSnapshot | None = None

        root = QVBoxLayout(self)
        title = QLabel("Craft Scanner")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        self.status = QLabel(
            "The scanner uses cached data only. Refresh market/static data explicitly when needed."
        )
        self.status.setWordWrap(True)
        self.status.setObjectName("dataBanner")
        root.addWidget(self.status)
        self.zero_results = QLabel()
        self.zero_results.setWordWrap(True)
        self.zero_results.setObjectName("dataBanner")
        self.zero_results.setVisible(False)
        root.addWidget(self.zero_results)
        self.show_nonactionable_button = QPushButton("Show non-actionable opportunities")
        self.show_nonactionable_button.setVisible(False)
        self.show_nonactionable_button.clicked.connect(self._show_nonactionable)
        root.addWidget(self.show_nonactionable_button)

        filters = QGridLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Item name or canonical Albion ID")
        self.tier_min = self._optional_spin("Any", 0, 8)
        self.tier_max = self._optional_spin("Any", 0, 8)
        self.enchantment = QComboBox()
        self.enchantment.addItem("Any enchantment", None)
        for value in range(5):
            self.enchantment.addItem(f".{value}", value)
        self.category = QComboBox()
        self.category.addItem("Any crafting category", None)
        for category in sorted(VERIFIED_CRAFTING_GROUPS):
            self.category.addItem(category.replace("_", " ").title(), category)
        filters.addWidget(QLabel("Search"), 0, 0)
        filters.addWidget(self.search, 0, 1, 1, 3)
        filters.addWidget(QLabel("Tier min"), 0, 4)
        filters.addWidget(self.tier_min, 0, 5)
        filters.addWidget(QLabel("Tier max"), 0, 6)
        filters.addWidget(self.tier_max, 0, 7)
        filters.addWidget(self.enchantment, 0, 8)
        filters.addWidget(self.category, 0, 9)

        self.craft_cities = QLineEdit(str(self._setting("default_craft_city")))
        self.craft_cities.setToolTip("Comma-separated craft cities")
        self.sell_cities = QLineEdit(str(self._setting("default_sell_city")))
        self.sell_cities.setToolTip("Comma-separated sell cities")
        self.material_city = QComboBox()
        self.material_city.addItem("Buy materials in craft city", None)
        self.material_city.addItems(CITIES)
        self.use_focus = QCheckBox("Use Focus")
        self.use_focus.setChecked(bool(self._setting("focus_enabled")))
        self.actionable_only = QCheckBox("Actionable only")
        self.actionable_only.setChecked(True)
        filters.addWidget(QLabel("Craft cities"), 1, 0)
        filters.addWidget(self.craft_cities, 1, 1, 1, 2)
        filters.addWidget(QLabel("Sell cities"), 1, 3)
        filters.addWidget(self.sell_cities, 1, 4, 1, 2)
        filters.addWidget(self.material_city, 1, 6, 1, 2)
        filters.addWidget(self.use_focus, 1, 8)
        filters.addWidget(self.actionable_only, 1, 9)

        self.minimum_profit = QDoubleSpinBox()
        self.minimum_profit.setRange(-1_000_000_000, 1_000_000_000)
        self.minimum_profit.setDecimals(0)
        self.minimum_profit.setPrefix("≥ ")
        self.minimum_profit.setSuffix(" silver")
        self.minimum_profit_enabled = QCheckBox("Min profit")
        self.minimum_profit.setEnabled(False)
        self.minimum_profit_enabled.toggled.connect(self.minimum_profit.setEnabled)
        self.minimum_roi = QDoubleSpinBox()
        self.minimum_roi.setRange(-1_000, 100_000)
        self.minimum_roi.setDecimals(1)
        self.minimum_roi.setPrefix("≥ ")
        self.minimum_roi.setSuffix("% ROI")
        self.minimum_roi_enabled = QCheckBox("Min ROI")
        self.minimum_roi.setEnabled(False)
        self.minimum_roi_enabled.toggled.connect(self.minimum_roi.setEnabled)
        self.maximum_capital = QDoubleSpinBox()
        self.maximum_capital.setRange(0, 10_000_000_000)
        self.maximum_capital.setDecimals(0)
        self.maximum_capital.setSpecialValueText("No capital limit")
        self.maximum_capital.setToolTip(
            "Maximum cash required before sale revenue: materials, station, and "
            "sell-order setup fee."
        )
        self.maximum_age = QSpinBox()
        self.maximum_age.setRange(1, 168)
        self.maximum_age.setSuffix("h max age")
        self.maximum_age.setValue(int(self._setting("max_market_age_hours")))
        self.liquidity = QComboBox()
        self.liquidity.addItem("Any liquidity", None)
        for level in LiquidityLevel:
            self.liquidity.addItem(level.value, level.value)
        self.ranking = QComboBox()
        self.ranking.addItem("Rank: Profit", OpportunitySort.PROFIT.value)
        self.ranking.addItem("Rank: ROI", OpportunitySort.ROI.value)
        self.ranking.addItem("Rank: Margin", OpportunitySort.MARGIN.value)
        self.ranking.addItem("Rank: Silver / Focus", OpportunitySort.SILVER_PER_FOCUS.value)
        filters.addWidget(self.minimum_profit_enabled, 2, 0)
        filters.addWidget(self.minimum_profit, 2, 1)
        filters.addWidget(self.minimum_roi_enabled, 2, 2)
        filters.addWidget(self.minimum_roi, 2, 3)
        filters.addWidget(self.maximum_capital, 2, 4, 1, 2)
        filters.addWidget(self.maximum_age, 2, 6)
        filters.addWidget(self.liquidity, 2, 7)
        filters.addWidget(self.ranking, 2, 8, 1, 2)
        root.addLayout(filters)

        controls = QGridLayout()
        self.scan_button = QPushButton("Scan full filtered catalog")
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.progress = QProgressBar()
        controls.addWidget(self.scan_button, 0, 0)
        controls.addWidget(self.cancel_button, 0, 1)
        controls.addWidget(self.progress, 0, 2, 1, 8)
        root.addLayout(controls)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._show_selected_detail)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Select an opportunity to inspect every assumption.")
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

    @staticmethod
    def _optional_spin(label: str, minimum: int, maximum: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setSpecialValueText(label)
        return widget

    def _setting(self, key: str):
        return self.settings.get(key, DEFAULT_SETTINGS[key])

    @staticmethod
    def _parse_cities(text: str) -> tuple[str, ...]:
        requested = tuple(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
        unknown = [city for city in requested if city not in CITIES]
        if unknown:
            raise ValueError("Unknown city: " + ", ".join(unknown))
        if not requested:
            raise ValueError("At least one city is required")
        return requested

    def _constraints(self) -> ScanConstraints:
        category = self.category.currentData()
        enchantment = self.enchantment.currentData()
        liquidity = self.liquidity.currentData()
        capital = self.maximum_capital.value()
        return ScanConstraints(
            region=Region(str(self._setting("region"))),
            craft_cities=self._parse_cities(self.craft_cities.text()),
            sell_cities=self._parse_cities(self.sell_cities.text()),
            material_city=self.material_city.currentData(),
            text=self.search.text(),
            tier_min=self.tier_min.value() or None,
            tier_max=self.tier_max.value() or None,
            enchantments=() if enchantment is None else (int(enchantment),),
            crafting_categories=() if category is None else (str(category),),
            use_focus=self.use_focus.isChecked(),
            available_focus=float(self._setting("available_focus")),
            premium=bool(self._setting("premium")),
            maximum_price_age=timedelta(hours=self.maximum_age.value()),
            maximum_station_fee_age=timedelta(
                hours=int(self._setting("max_station_fee_age_hours"))
            ),
            actionable_only=self.actionable_only.isChecked(),
            minimum_profit=(
                self.minimum_profit.value() if self.minimum_profit_enabled.isChecked() else None
            ),
            minimum_roi=(
                self.minimum_roi.value() / 100 if self.minimum_roi_enabled.isChecked() else None
            ),
            maximum_upfront_capital=capital or None,
            liquidity_levels=() if liquidity is None else (str(liquidity),),
            output_quality=1,
            sort_by=OpportunitySort(str(self.ranking.currentData())),
        )

    def refresh(self) -> None:
        if self.worker is None:
            self.start_scan()

    def start_scan(self) -> None:
        if self.worker is not None:
            self.status.setText("A scan is already running; cancel it before starting another.")
            return
        if self.catalog.import_metadata() is None:
            self.status.setText(
                "No production static catalog is installed. Use Update Static Game Data in "
                "Market Data; sample recipes are never scanned."
            )
            return
        try:
            constraints = self._constraints()
        except ValueError as error:
            self.status.setText(str(error))
            return
        worker = OpportunityScanWorker(self.service, constraints)
        self.worker = worker
        worker.signals.progress.connect(
            lambda value, selected=worker: self._scan_progress(selected, value)
        )
        worker.signals.finished.connect(
            lambda value, selected=worker: self._scan_finished(selected, value)
        )
        worker.signals.error.connect(
            lambda value, selected=worker: self._scan_failed(selected, value)
        )
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.status.setText("Starting background opportunity scan...")
        self.thread_pool.start(worker)

    def cancel_scan(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.status.setText("Cancellation requested; finishing the current in-memory step...")

    def _scan_progress(self, worker: OpportunityScanWorker, value: ScanProgress) -> None:
        if worker is not self.worker:
            return
        if value.fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(round(value.fraction * 100))
        self.status.setText(value.message)

    def _scan_finished(self, worker: OpportunityScanWorker, snapshot: ScanSnapshot) -> None:
        if worker is not self.worker:
            return
        self.worker = None
        self.snapshot = snapshot
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if not snapshot.cancelled else 0)
        self._render(snapshot)

    def _scan_failed(self, worker: OpportunityScanWorker, message: str) -> None:
        if worker is not self.worker:
            return
        self.worker = None
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("Opportunity scan failed without changing cached data: " + message)

    def _render(self, snapshot: ScanSnapshot) -> None:
        shown = snapshot.opportunities[: self.DISPLAY_LIMIT]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(shown))
        for row, opportunity in enumerate(shown):
            result = opportunity.calculation
            liquidity = opportunity.liquidity
            oldest_age = opportunity.pricing.oldest_required_age(snapshot.scan_time)
            item = opportunity.recipe.output
            values = (
                SortableItem(opportunity.display_name, opportunity.display_name.casefold()),
                SortableItem("?" if item.tier is None else str(item.tier), item.tier),
                SortableItem(str(item.enchantment), item.enchantment),
                SortableItem(opportunity.material_city, opportunity.material_city),
                SortableItem(opportunity.craft_city, opportunity.craft_city),
                SortableItem(opportunity.sell_city, opportunity.sell_city),
                SortableItem(money(result.profit), result.profit),
                SortableItem(percent(result.roi), result.roi),
                SortableItem(percent(result.margin), result.margin),
                SortableItem(
                    money(opportunity.upfront_capital_required),
                    opportunity.upfront_capital_required,
                ),
                SortableItem(money(result.station_fee), result.station_fee),
                SortableItem(percent(result.return_rate), result.return_rate),
                SortableItem(money(result.focus_used), result.focus_used),
                SortableItem(money(result.silver_per_focus), result.silver_per_focus),
                SortableItem(
                    liquidity.level.value if liquidity is not None else "Unknown",
                    liquidity.level.value if liquidity is not None else "Unknown",
                ),
                SortableItem(self._duration(oldest_age), self._seconds(oldest_age)),
                SortableItem(
                    result.actionability.status.value,
                    result.actionability.status.value,
                ),
            )
            values[0].setData(Qt.ItemDataRole.UserRole, opportunity)
            for column, table_item in enumerate(values):
                if not result.actionability.is_actionable:
                    table_item.setForeground(QColor("#ff8b8b"))
                elif liquidity is not None and liquidity.level in {
                    LiquidityLevel.LOW,
                    LiquidityLevel.UNKNOWN,
                }:
                    table_item.setForeground(QColor("#ffcf70"))
                elif opportunity.pricing.freshness is Freshness.AGING:
                    table_item.setForeground(QColor("#ffcf70"))
                self.table.setItem(row, column, table_item)
        self.table.setSortingEnabled(True)
        rank_column = {
            OpportunitySort.PROFIT: 6,
            OpportunitySort.ROI: 7,
            OpportunitySort.MARGIN: 8,
            OpportunitySort.SILVER_PER_FOCUS: 13,
        }[snapshot.constraints.sort_by]
        order = (
            Qt.SortOrder.DescendingOrder
            if snapshot.constraints.descending
            else Qt.SortOrder.AscendingOrder
        )
        self.table.sortItems(rank_column, order)
        qualifier = "cancelled partial scan" if snapshot.cancelled else "complete filtered scan"
        self.status.setText(
            f"{qualifier}: {snapshot.recipes_considered:,} recipes, "
            f"{snapshot.scenarios_evaluated:,} scenarios, "
            f"{snapshot.actionable_count:,} actionable; showing "
            f"{len(shown):,} of {len(snapshot.opportunities):,} ranked matches. "
            f"Loaded {snapshot.market_rows_loaded:,} market rows and "
            f"{snapshot.override_rows_loaded:,} overrides in "
            f"{snapshot.database_read_statements:,} bounded SQL reads. "
            f"Elapsed {snapshot.elapsed_seconds:.3f}s · ruleset {snapshot.ruleset_id}. "
            "The 500-row display cap is not an analysis cap."
        )
        empty_actionable = bool(
            snapshot.constraints.actionable_only and snapshot.actionable_count == 0
        )
        self.zero_results.setVisible(empty_actionable)
        self.show_nonactionable_button.setVisible(empty_actionable)
        if empty_actionable:
            self.zero_results.setText(
                "0 actionable results\n\n"
                f"{snapshot.scenarios_evaluated:,} scenarios checked. None passed every saved "
                "price-freshness, static-data, station-fee, profitability, and liquidity rule. "
                "Show non-actionable opportunities to inspect the individual reasons."
            )
        self.detail.clear()

    def _show_nonactionable(self) -> None:
        self.actionable_only.setChecked(False)
        self.start_scan()

    def _show_selected_detail(self) -> None:
        selected = self.table.selectedItems()
        if not selected:
            return
        first = self.table.item(selected[0].row(), 0)
        opportunity = first.data(Qt.ItemDataRole.UserRole)
        if isinstance(opportunity, CraftOpportunity):
            self.detail.setPlainText(self._detail_text(opportunity))

    def _detail_text(self, value: CraftOpportunity) -> str:
        result = value.calculation
        liquidity = value.liquidity
        as_of = self.snapshot.scan_time if self.snapshot is not None else None
        reason_text = (
            "\n".join(
                f"- [{reason.code.value}] {reason.message} ({reason.severity.value})"
                for reason in result.actionability.reasons
            )
            or "- None"
        )
        requirements = {line.item_id: line for line in value.recipe.materials}
        evidence_lines: list[str] = []
        for line in value.pricing.evidence:
            age = self._duration(line.age(as_of)) if as_of is not None else "Unknown"
            detail = ""
            if line.role in {"material", "returned_material_informational"}:
                requirement = requirements[line.item_id]
                quantity = requirement.quantity * result.crafts
                if requirement.returnable is True and result.return_rate is not None:
                    returned_quantity: float | None = quantity * result.return_rate
                elif requirement.returnable is False:
                    returned_quantity = 0.0
                else:
                    returned_quantity = None
                returned_value = (
                    returned_quantity * line.price
                    if returned_quantity is not None and line.price is not None
                    else None
                )
                returnability = (
                    "returnable"
                    if requirement.returnable is True
                    else "not returnable"
                    if requirement.returnable is False
                    else "returnability unknown"
                )
                expected_quantity = (
                    "unknown" if returned_quantity is None else f"{returned_quantity:,.3f}"
                )
                if line.role == "material":
                    detail = (
                        f" · required={quantity:,.3f} · {returnability} · "
                        f"expected returned={expected_quantity} ({money(returned_value)})"
                    )
                else:
                    detail = (
                        " · optional craft-city valuation · "
                        f"expected returned={expected_quantity} ({money(returned_value)})"
                    )
            else:
                detail = f" · batch output={result.output_quantity:,}"
            evidence_lines.append(
                f"- {line.role}: {line.item_id}{detail} · {line.city} · "
                f"{line.side} · unit price={money(line.price)} · "
                f"{line.provenance.value} · observed="
                f"{self._timestamp(line.observation_timestamp)} · age={age} · "
                f"fetched={self._timestamp(line.fetched_at)}"
            )
        evidence = "\n".join(evidence_lines)
        liquidity_text = (
            "Unknown (history not loaded)"
            if liquidity is None
            else (
                f"{liquidity.level.value}; reported volume={liquidity.reported_volume}; "
                f"active intervals={liquidity.active_intervals}; "
                f"weighted mean={money(liquidity.weighted_mean_price)}; "
                f"current deviation={percent(liquidity.current_price_deviation)}\n"
                + "\n".join(f"- {reason}" for reason in liquidity.reasons)
            )
        )
        observed = (
            value.station_fee_observed_at.isoformat()
            if value.station_fee_observed_at
            else "unknown"
        )
        displayed_fee = (
            value.station_displayed_fee if value.station_displayed_fee is not None else "unknown"
        )
        output_age = self._duration(value.pricing.output_age(as_of)) if as_of else "Unknown"
        material_age = (
            self._duration(value.pricing.oldest_material_age(as_of)) if as_of else "Unknown"
        )
        required_age = (
            self._duration(value.pricing.oldest_required_age(as_of)) if as_of else "Unknown"
        )
        return (
            f"{value.display_name} · {value.item_id}\n"
            f"Material city: {value.material_city}\nCraft city: {value.craft_city}\n"
            f"Sell city: {value.sell_city}\nRuleset: {result.ruleset_id}\n\n"
            "MECHANICS\n"
            f"Station: {value.station_type or 'unknown'}; displayed fee={displayed_fee}; "
            f"provenance={value.station_fee_provenance.value}; observed={observed}\n"
            f"Production bonus: {percent(value.production_bonus)} "
            f"({value.production_bonus_status}); RRR={percent(result.return_rate)}\n"
            f"FCE: {money(value.focus_efficiency)} ({value.focus_efficiency_source}); "
            f"Focus/batch={money(result.focus_used)}; "
            f"max affordable crafts={value.maximum_focus_crafts}\n\n"
            "ECONOMICS\n"
            f"Gross material purchase cash: {money(result.gross_material_purchase_cash)}\n"
            f"Station cash: {money(result.station_cash)}\n"
            f"Sell-order setup cash: {money(result.listing_setup_cash)}\n"
            "Total pre-revenue cash (materials + station + setup): "
            f"{money(value.upfront_capital_required)}\n"
            "Expected returned-material cost-basis value: "
            f"{money(result.returned_material_cost_basis_value)}\n"
            "Expected returned-material craft-city market value (informational only): "
            f"{money(result.returned_material_craft_city_market_value)}\n"
            f"Effective material cost: {money(result.effective_material_cost)}\n"
            f"Station cost: {money(result.station_fee)}\n"
            f"Gross sale: {money(result.gross_sale_value)}\n"
            f"Marketplace fees: {money(result.market_fees)}\n"
            f"Net sale: {money(result.net_sale_value)}\nProfit: {money(result.profit)}\n"
            f"ROI: {percent(result.roi)} · Margin: {percent(result.margin)} · "
            f"Break-even: {money(result.break_even_price)}\n\n"
            "PRICE EVIDENCE\n"
            f"Output age: {output_age}; oldest material age: {material_age}; "
            f"oldest required age: {required_age}\n"
            f"{evidence}\n\nLIQUIDITY (reported history, not order depth)\n"
            f"{liquidity_text}\n\nACTIONABILITY\n"
            f"{result.actionability.status.value}\n{reason_text}\n\n"
            "Execution caveat: current prices are top-of-book unit observations. "
            "Order depth and guaranteed execution quantity are not modeled."
        )

    @staticmethod
    def _seconds(value: timedelta | None) -> float | None:
        return value.total_seconds() if value is not None else None

    @staticmethod
    def _timestamp(value) -> str:
        return value.isoformat() if value is not None else "unknown time"

    @staticmethod
    def _duration(value: timedelta | None) -> str:
        if value is None:
            return "Unknown"
        seconds = value.total_seconds()
        if seconds < -DEFAULT_CLOCK_SKEW_TOLERANCE.total_seconds():
            future = abs(seconds)
            if future < 3600:
                offset = f"{future / 60:.0f}m"
            elif future < 86_400:
                offset = f"{future / 3600:.1f}h"
            else:
                offset = f"{future / 86_400:.1f}d"
            return f"Future-dated by {offset} (invalid)"
        seconds = max(seconds, 0)
        if seconds < 3600:
            return f"{seconds / 60:.0f}m"
        return f"{seconds / 3600:.1f}h"

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.cancel_scan()
        super().closeEvent(event)
