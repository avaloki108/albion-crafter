from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from albion_crafter.core.freshness import Freshness
from albion_crafter.core.models import ActionKind, SaleMethod
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation
from albion_crafter.data.cities import CITIES
from albion_crafter.database.v4 import (
    FindMoneyPreferencesRepository,
    PlanSnapshotRepository,
)
from albion_crafter.market.models import Region, UserPriceOverride
from albion_crafter.planning.explanations import build_plan_explanation
from albion_crafter.planning.export import export_plan_csv, export_plan_json
from albion_crafter.planning.models import (
    OUTER_ROYAL_CITIES,
    ArbitrageScope,
    FindMoneyConstraints,
    MinimumLiquidity,
    PlanAction,
    PlanReasonSeverity,
    PlanSnapshot,
    PlanStatus,
    TransportPolicy,
)
from albion_crafter.planning.preflight import (
    FindMoneyPreflight,
    PriceRequirementAssessment,
    StationFeeRequirement,
)
from albion_crafter.planning.service import (
    FindMoneyRunResult,
    FindMoneyService,
    PlanningProgress,
    PlanningStage,
)

from .common import SortableItem, age_text, money, percent
from .find_money_worker import FindMoneyWorker, PlanningCancellationToken

ServiceFactory = Callable[[Region], FindMoneyService]


class TrustPreset(StrEnum):
    FAST = "fast"
    CAREFUL = "careful"
    STRICT = "strict"


class FindMoneyView(QWidget):
    """Simple-first Qt workflow over the reproducible staged planner."""

    plan_completed = Signal(object)
    focus_setup_requested = Signal()
    evidence_saved = Signal()
    ACTION_HEADERS = (
        "Action",
        "Item",
        "Units / batches",
        "Focused",
        "Non-Focus",
        "Buy / material city",
        "Production city",
        "Sell city",
        "Station",
        "Station fee",
        "Capital",
        "Economic cost",
        "Revenue",
        "Profit",
        "ROI",
        "Margin",
        "Focus",
        "Silver / Focus",
        "Liquidity",
        "Oldest price",
        "Actionability",
    )
    RECENT_HEADERS = (
        "Generated",
        "Status",
        "Optimization",
        "Region",
        "Actions",
        "Capital",
        "Focus",
        "Expected profit",
        "Snapshot ID",
    )
    EXCLUDED_HEADERS = (
        "Action",
        "Item",
        "Route",
        "Expected profit / unit or batch",
        "Why excluded",
    )
    MAX_EXCLUDED_ROWS = 500
    STAGES = tuple(PlanningStage)

    def __init__(
        self,
        service: FindMoneyService,
        snapshots: PlanSnapshotRepository | None = None,
        preferences: FindMoneyPreferencesRepository | None = None,
        *,
        service_factory: ServiceFactory | None = None,
        default_constraints: FindMoneyConstraints | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.service_factory = service_factory
        self.snapshots = snapshots
        self.preferences = preferences
        self.default_constraints = default_constraints or FindMoneyConstraints(
            available_silver=1_000_000,
            available_focus=10_000,
        )
        self.preflight: FindMoneyPreflight | None = None
        self.run_result: FindMoneyRunResult | None = None
        self.displayed_snapshot: PlanSnapshot | None = None
        self._preflight_service = service
        self._thread: QThread | None = None
        self._worker: FindMoneyWorker | None = None
        self._cancellation: PlanningCancellationToken | None = None
        self._close_when_finished = False
        self._loading_controls = False
        self._simple_run_requested = False
        self._station_setup_requirements: tuple[StationFeeRequirement, ...] = ()
        self._price_setup_requirements: tuple[PriceRequirementAssessment, ...] = ()

        root = QVBoxLayout(self)
        title = QLabel("Find Me Money")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        intro = QLabel(
            "Enter your bankroll, choose a home city, and press FIND ME MONEY. The app checks "
            "crafting, refining, and configured outer-Royal arbitrage, refreshes only the market "
            "data it needs, and shows the best defensible plan. No network request starts until "
            "you press the button."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        root.addWidget(intro)

        self._build_inputs(root)
        self._build_run_controls(root)
        self._build_output_tabs(root)
        self._connect_dirty_controls()
        self._load_preferences()
        self._refresh_focus_availability()
        self._toggle_advanced(False)
        self.refresh_recent_snapshots()

    def _build_inputs(self, root: QVBoxLayout) -> None:
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Advanced Mode")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        root.addWidget(self.advanced_toggle)

        self.simple_inputs = QGroupBox("Simple search")
        simple = QGridLayout(self.simple_inputs)
        self.available_silver = self._silver_spin(1_000_000)
        self.silver_reserve = self._silver_spin(0)
        self.home_city = QComboBox()
        self.home_city.addItems(CITIES)
        self.premium = QCheckBox("Premium")
        self.use_focus = QCheckBox("Use Focus where profile is known")
        self.premium.setChecked(True)
        self.use_focus.setChecked(False)
        self.item_query = QLineEdit()
        self.item_query.setPlaceholderText(
            "Optional item search — leave blank to search everything"
        )
        self.trust_preset = QComboBox()
        self.trust_preset.addItem("Fast / broad search", TrustPreset.FAST.value)
        self.trust_preset.addItem("Careful", TrustPreset.CAREFUL.value)
        self.trust_preset.addItem("Strict", TrustPreset.STRICT.value)
        self.trust_preset.setCurrentIndex(1)
        self.trust_explanation = QLabel()
        self.trust_explanation.setWordWrap(True)
        self.trust_explanation.setObjectName("muted")
        self.focus_status = QLabel()
        self.focus_status.setWordWrap(True)
        self.focus_status.setObjectName("muted")
        self.focus_setup_button = QPushButton("Set up Focus")
        self.focus_setup_button.clicked.connect(self.focus_setup_requested.emit)
        simple.addWidget(QLabel("Available silver"), 0, 0)
        simple.addWidget(self.available_silver, 0, 1)
        simple.addWidget(QLabel("Keep silver reserve"), 0, 2)
        simple.addWidget(self.silver_reserve, 0, 3)
        simple.addWidget(QLabel("Home city"), 0, 4)
        simple.addWidget(self.home_city, 0, 5)
        simple.addWidget(self.premium, 1, 0)
        simple.addWidget(self.use_focus, 1, 1, 1, 2)
        simple.addWidget(self.focus_setup_button, 1, 3)
        simple.addWidget(self.focus_status, 1, 4, 1, 2)
        simple.addWidget(QLabel("Item search"), 2, 0)
        simple.addWidget(self.item_query, 2, 1, 1, 3)
        simple.addWidget(QLabel("Trust preset"), 2, 4)
        simple.addWidget(self.trust_preset, 2, 5)
        simple.addWidget(self.trust_explanation, 3, 0, 1, 6)
        root.addWidget(self.simple_inputs)

        action_box = QGroupBox("Action types and refining families")
        self.action_inputs = action_box
        action_layout = QHBoxLayout(action_box)
        self.craft_actions = QCheckBox("Crafting")
        self.refine_actions = QCheckBox("Refining")
        self.arbitrage_actions = QCheckBox("Market arbitrage")
        self.craft_actions.setChecked(True)
        self.refine_actions.setChecked(True)
        action_layout.addWidget(self.craft_actions)
        action_layout.addWidget(self.refine_actions)
        action_layout.addWidget(self.arbitrage_actions)
        self.refining_family_checks: dict[str, QCheckBox] = {}
        for family, label in (
            ("ore", "Ore / Metal Bars"),
            ("wood", "Wood / Planks"),
            ("hide", "Hide / Leather"),
            ("fiber", "Fiber / Cloth"),
            ("rock", "Rock / Stone Blocks"),
        ):
            widget = QCheckBox(label)
            widget.setChecked(True)
            self.refining_family_checks[family] = widget
            action_layout.addWidget(widget)
        action_layout.addStretch(1)
        root.addWidget(action_box)

        arbitrage_box = QGroupBox("Market arbitrage universe (Outer Royal cities only)")
        self.arbitrage_inputs = arbitrage_box
        arbitrage_layout = QGridLayout(arbitrage_box)
        self.arbitrage_scope = QComboBox()
        self.arbitrage_scope.addItem(
            "All production outputs", ArbitrageScope.ALL_PRODUCTION_OUTPUTS.value
        )
        self.arbitrage_scope.addItem("Crafted outputs", ArbitrageScope.CRAFTED_OUTPUTS.value)
        self.arbitrage_scope.addItem("Refined resources", ArbitrageScope.REFINED_RESOURCES.value)
        outer_cities = ", ".join(OUTER_ROYAL_CITIES)
        self.arbitrage_source_cities = QLineEdit(outer_cities)
        self.arbitrage_destination_cities = QLineEdit(outer_cities)
        for widget in (self.arbitrage_source_cities, self.arbitrage_destination_cities):
            widget.setToolTip(
                "Comma-separated subset of Bridgewatch, Fort Sterling, Lymhurst, Martlock, "
                "and Thetford"
            )
        arbitrage_layout.addWidget(QLabel("Item scope"), 0, 0)
        arbitrage_layout.addWidget(self.arbitrage_scope, 0, 1)
        arbitrage_layout.addWidget(QLabel("Source cities"), 0, 2)
        arbitrage_layout.addWidget(self.arbitrage_source_cities, 0, 3)
        arbitrage_layout.addWidget(QLabel("Destination cities"), 0, 4)
        arbitrage_layout.addWidget(self.arbitrage_destination_cities, 0, 5)
        root.addWidget(arbitrage_box)

        core = QGroupBox("Planning budget and route universe")
        self.core_inputs = core
        layout = QGridLayout(core)
        self.available_focus = QSpinBox()
        self.available_focus.setRange(0, 1_000_000_000)
        self.available_focus.setSingleStep(500)
        self.focus_reserve = QSpinBox()
        self.focus_reserve.setRange(0, 1_000_000_000)
        self.focus_reserve.setSingleStep(500)
        self.region = QComboBox()
        for region in Region:
            self.region.addItem(region.display_name, region.value)

        layout.addWidget(QLabel("Available Focus"), 0, 0)
        layout.addWidget(self.available_focus, 0, 1)
        layout.addWidget(QLabel("Keep Focus reserve"), 0, 2)
        layout.addWidget(self.focus_reserve, 0, 3)
        layout.addWidget(QLabel("Server"), 0, 4)
        layout.addWidget(self.region, 0, 5)

        self.tier_min = QSpinBox()
        self.tier_min.setRange(1, 8)
        self.tier_min.setValue(4)
        self.tier_max = QSpinBox()
        self.tier_max.setRange(1, 8)
        self.tier_max.setValue(8)
        self.enchantments = QLineEdit("0, 1, 2, 3")
        self.enchantments.setToolTip("Comma-separated enchantments from 0 through 4")
        self.categories = QLineEdit()
        self.categories.setPlaceholderText("Optional crafting categories, comma-separated")
        layout.addWidget(QLabel("Tier min"), 1, 0)
        layout.addWidget(self.tier_min, 1, 1)
        layout.addWidget(QLabel("Tier max"), 1, 2)
        layout.addWidget(self.tier_max, 1, 3)
        layout.addWidget(QLabel("Enchantments"), 1, 4)
        layout.addWidget(self.enchantments, 1, 5)
        layout.addWidget(QLabel("Categories"), 1, 6)
        layout.addWidget(self.categories, 1, 7)

        self.material_cities = QLineEdit("Bridgewatch")
        self.craft_cities = QLineEdit("Bridgewatch")
        self.sell_cities = QLineEdit("Bridgewatch")
        for widget in (self.material_cities, self.craft_cities, self.sell_cities):
            widget.setToolTip("Comma-separated Albion city names")
        layout.addWidget(QLabel("Material cities"), 2, 0)
        layout.addWidget(self.material_cities, 2, 1)
        layout.addWidget(QLabel("Production cities"), 2, 2)
        layout.addWidget(self.craft_cities, 2, 3)
        layout.addWidget(QLabel("Sell cities"), 2, 4)
        layout.addWidget(self.sell_cities, 2, 5, 1, 3)
        root.addWidget(core)
        self.advanced = QGroupBox("Advanced")
        self.advanced.setVisible(False)
        advanced = QGridLayout(self.advanced)

        self.sale_method = QComboBox()
        self.sale_method.addItem("Sell Order", SaleMethod.SELL_ORDER.value)
        self.sale_method.addItem("Instant Sell", SaleMethod.INSTANT_SELL.value)
        self.transport_policy = QComboBox()
        self.transport_policy.addItem("Local only", TransportPolicy.LOCAL_ONLY.value)
        self.transport_policy.addItem(
            "Cross-city: acknowledged, uncosted (advisory)",
            TransportPolicy.ACKNOWLEDGED_UNCOSTED.value,
        )
        self.transport_policy.addItem(
            "Cross-city: explicit silver cost",
            TransportPolicy.EXPLICIT_COST.value,
        )
        self.transport_cost = self._silver_spin(0)
        self.transport_cost.setEnabled(False)
        advanced.addWidget(QLabel("Sale method"), 0, 0)
        advanced.addWidget(self.sale_method, 0, 1)
        advanced.addWidget(QLabel("Transport policy"), 0, 2)
        advanced.addWidget(self.transport_policy, 0, 3, 1, 2)
        advanced.addWidget(QLabel("Transport / action unit"), 0, 5)
        advanced.addWidget(self.transport_cost, 0, 6)

        self.market_age = QSpinBox()
        self.market_age.setRange(1, 720)
        self.market_age.setValue(4)
        self.market_age.setSuffix(" h")
        self.station_age = QSpinBox()
        self.station_age.setRange(1, 2_160)
        self.station_age.setValue(24)
        self.station_age.setSuffix(" h")
        self.allow_stale_station = QCheckBox("Allow stale station fees as advisory")
        advanced.addWidget(QLabel("Max market age"), 1, 0)
        advanced.addWidget(self.market_age, 1, 1)
        advanced.addWidget(QLabel("Max station-fee age"), 1, 2)
        advanced.addWidget(self.station_age, 1, 3)
        advanced.addWidget(self.allow_stale_station, 1, 4, 1, 3)

        self.minimum_profit_enabled = QCheckBox("Minimum profit / batch")
        self.minimum_profit = self._silver_spin(0, allow_negative=True)
        self.minimum_profit.setEnabled(False)
        self.minimum_roi_enabled = QCheckBox("Minimum ROI")
        self.minimum_roi = QDoubleSpinBox()
        self.minimum_roi.setRange(-1_000, 100_000)
        self.minimum_roi.setDecimals(1)
        self.minimum_roi.setSuffix(" %")
        self.minimum_roi.setEnabled(False)
        self.minimum_liquidity = QComboBox()
        for value, label in (
            (MinimumLiquidity.ANY, "Any / Unknown allowed"),
            (MinimumLiquidity.LOW, "Low+"),
            (MinimumLiquidity.MODERATE, "Moderate+"),
            (MinimumLiquidity.HIGH, "High only"),
        ):
            self.minimum_liquidity.addItem(label, value.value)
        advanced.addWidget(self.minimum_profit_enabled, 2, 0)
        advanced.addWidget(self.minimum_profit, 2, 1)
        advanced.addWidget(self.minimum_roi_enabled, 2, 2)
        advanced.addWidget(self.minimum_roi, 2, 3)
        advanced.addWidget(QLabel("Minimum liquidity"), 2, 4)
        advanced.addWidget(self.minimum_liquidity, 2, 5, 1, 2)

        self.per_item_cap = QSpinBox()
        self.per_item_cap.setRange(1, 10_000)
        self.per_item_cap.setValue(10)
        self.volume_share_enabled = QCheckBox("24h reported-volume ceiling")
        self.volume_share_enabled.setChecked(True)
        self.volume_share = QDoubleSpinBox()
        self.volume_share.setRange(0.1, 100.0)
        self.volume_share.setDecimals(1)
        self.volume_share.setValue(20.0)
        self.volume_share.setSuffix(" %")
        self.history_enabled = QCheckBox("Enrich shortlisted outputs with history")
        self.history_enabled.setChecked(True)
        self.history_shortlist = QSpinBox()
        self.history_shortlist.setRange(1, 5_000)
        self.history_shortlist.setValue(200)
        self.force_refresh = QCheckBox("Force current-price refresh")
        advanced.addWidget(QLabel("Shared action cap / market key"), 3, 0)
        advanced.addWidget(self.per_item_cap, 3, 1)
        advanced.addWidget(self.volume_share_enabled, 3, 2)
        advanced.addWidget(self.volume_share, 3, 3)
        advanced.addWidget(self.history_enabled, 3, 4, 1, 2)
        advanced.addWidget(QLabel("History shortlist"), 3, 6)
        advanced.addWidget(self.history_shortlist, 3, 7)
        advanced.addWidget(self.force_refresh, 4, 0, 1, 3)
        root.addWidget(self.advanced)

        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.home_city.currentTextChanged.connect(self._home_city_changed)
        self.trust_preset.currentIndexChanged.connect(self._trust_preset_changed)
        self.transport_policy.currentIndexChanged.connect(self._transport_changed)
        self.minimum_profit_enabled.toggled.connect(self.minimum_profit.setEnabled)
        self.minimum_roi_enabled.toggled.connect(self.minimum_roi.setEnabled)
        self.volume_share_enabled.toggled.connect(self.volume_share.setEnabled)
        self.history_enabled.toggled.connect(self.history_shortlist.setEnabled)

    def _build_run_controls(self, root: QVBoxLayout) -> None:
        self.simple_run_button = QPushButton("FIND ME MONEY")
        self.simple_run_button.setObjectName("findMoneySimpleButton")
        self.simple_run_button.setMinimumHeight(54)
        self.simple_run_button.clicked.connect(self.find_money)
        root.addWidget(self.simple_run_button)

        self.advanced_run_controls = QWidget()
        buttons = QHBoxLayout(self.advanced_run_controls)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.preflight_button = QPushButton("FIND ME MONEY — Preflight")
        self.preflight_button.setObjectName("findMoneyPreflightButton")
        self.preflight_button.clicked.connect(self.prepare_preflight)
        self.run_button = QPushButton("Run / Refresh & Plan")
        self.run_button.setObjectName("findMoneyRunButton")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.start_plan)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_plan)
        buttons.addWidget(self.preflight_button)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        root.addWidget(self.advanced_run_controls)

        self.status = QLabel("Ready. No Find Me Money preflight, scan, or network request has run.")
        self.status.setObjectName("dataBanner")
        self.status.setProperty("freshness", "unknown")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.market_summary = QLabel("Market prices will be checked after you press FIND ME MONEY.")
        self.market_summary.setObjectName("muted")
        self.market_summary.setWordWrap(True)
        root.addWidget(self.market_summary)
        self._build_station_setup(root)
        self._build_price_setup(root)
        progress_row = QHBoxLayout()
        self.stage_label = QLabel("Not started")
        self.stage_label.setMinimumWidth(260)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        progress_row.addWidget(self.stage_label)
        progress_row.addWidget(self.progress, 1)
        root.addLayout(progress_row)

    def _build_station_setup(self, root: QVBoxLayout) -> None:
        self.station_setup = QGroupBox("SETUP REQUIRED — saved station fees")
        layout = QVBoxLayout(self.station_setup)
        note = QLabel(
            "Enter the exact usage fee Albion currently displays. Missing fees are never "
            "assumed to be zero."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.station_setup_table = QTableWidget(0, 4)
        self.station_setup_table.setHorizontalHeaderLabels(
            ("City", "Station", "Current state", "Displayed fee")
        )
        self.station_setup_table.verticalHeader().setVisible(False)
        self.station_setup_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.station_setup_table)
        self.station_setup_status = QLabel()
        self.station_setup_status.setObjectName("muted")
        self.station_setup_status.setWordWrap(True)
        layout.addWidget(self.station_setup_status)
        save = QPushButton("Save & Continue")
        save.clicked.connect(self.save_station_fees_and_continue)
        layout.addWidget(save)
        self.station_setup.setVisible(False)
        root.addWidget(self.station_setup)

    def _build_price_setup(self, root: QVBoxLayout) -> None:
        self.price_setup = QGroupBox("MARKET DATA UNAVAILABLE")
        layout = QVBoxLayout(self.price_setup)
        self.price_setup_note = QLabel(
            "No AODP observation is currently available for these required market prices. "
            "You may enter the current in-game price, or try refreshing again."
        )
        self.price_setup_note.setWordWrap(True)
        layout.addWidget(self.price_setup_note)
        self.price_setup_table = QTableWidget(0, 5)
        self.price_setup_table.setHorizontalHeaderLabels(
            ("Item", "City", "Quality", "Required market side", "Albion price")
        )
        self.price_setup_table.verticalHeader().setVisible(False)
        self.price_setup_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.price_setup_table)
        self.price_setup_status = QLabel()
        self.price_setup_status.setObjectName("muted")
        self.price_setup_status.setWordWrap(True)
        layout.addWidget(self.price_setup_status)
        controls = QHBoxLayout()
        save = QPushButton("Save prices & Continue")
        save.clicked.connect(self.save_price_overrides_and_continue)
        retry = QPushButton("Refresh again")
        retry.clicked.connect(self.find_money)
        controls.addWidget(save)
        controls.addWidget(retry)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.price_setup.setVisible(False)
        root.addWidget(self.price_setup)

    def _build_output_tabs(self, root: QVBoxLayout) -> None:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_preflight_tab(), "Preflight")
        self.tabs.addTab(self._build_plan_tab(), "Plan")
        self.tabs.addTab(self._build_excluded_tab(), "Excluded / near misses")
        self.tabs.addTab(self._build_recent_tab(), "Recent immutable snapshots")
        root.addWidget(self.tabs, 1)
        self.tabs.setCurrentIndex(1)

    def _build_preflight_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.preflight_heading = QLabel(
            "Press FIND ME MONEY to inspect the candidate universe and required data first."
        )
        self.preflight_heading.setWordWrap(True)
        layout.addWidget(self.preflight_heading)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.preflight_counts = QTableWidget(0, 2)
        self.preflight_counts.setHorizontalHeaderLabels(("Preflight count", "Value"))
        self._configure_table(self.preflight_counts)
        self.preflight_counts.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.preflight_blockers = QTableWidget(0, 3)
        self.preflight_blockers.setHorizontalHeaderLabels(("Severity", "Code", "Message"))
        self._configure_table(self.preflight_blockers)
        self.preflight_blockers.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        splitter.addWidget(self.preflight_counts)
        splitter.addWidget(self.preflight_blockers)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 2)
        station_label = QLabel("Station-fee observations required by candidate routes")
        station_label.setObjectName("muted")
        layout.addWidget(station_label)
        self.station_requirements = QTableWidget(0, 7)
        self.station_requirements.setHorizontalHeaderLabels(
            ("Region", "City", "Station", "Displayed fee", "Freshness", "Route uses", "State")
        )
        self._configure_table(self.station_requirements)
        self.station_requirements.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.station_requirements, 1)
        return page

    def _build_plan_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plan_banner = QLabel("No plan has been generated or opened.")
        self.plan_banner.setObjectName("dataBanner")
        self.plan_banner.setProperty("freshness", "unknown")
        self.plan_banner.setWordWrap(True)
        layout.addWidget(self.plan_banner)
        self.simple_result_summary = QLabel(
            "Your best plan will appear here after you press FIND ME MONEY."
        )
        self.simple_result_summary.setWordWrap(True)
        self.simple_result_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.simple_result_summary)
        self.plan_totals = QLabel("—")
        self.plan_totals.setWordWrap(True)
        layout.addWidget(self.plan_totals)
        self.plan_details_toggle = QToolButton()
        self.plan_details_toggle.setText("Why this works / Evidence / Advanced details")
        self.plan_details_toggle.setCheckable(True)
        self.plan_details_toggle.setChecked(False)
        self.plan_details_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.plan_details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.plan_details_toggle.toggled.connect(self._toggle_plan_details)
        layout.addWidget(self.plan_details_toggle)
        self.plan_explanation = QPlainTextEdit()
        self.plan_explanation.setReadOnly(True)
        self.plan_explanation.setMaximumHeight(190)
        self.plan_explanation.setVisible(False)
        self.plan_explanation.setPlaceholderText(
            "Plan reasons, assumptions, unused-resource explanation, and rejection counts."
        )
        layout.addWidget(self.plan_explanation)
        actions = QSplitter(Qt.Orientation.Vertical)
        self.action_table = QTableWidget(0, len(self.ACTION_HEADERS))
        self.action_table.setHorizontalHeaderLabels(self.ACTION_HEADERS)
        self._configure_table(self.action_table, sortable=True)
        self.action_table.itemSelectionChanged.connect(self._show_selected_action)
        self.action_detail = QPlainTextEdit()
        self.action_detail.setReadOnly(True)
        self.action_detail.setPlaceholderText(
            "Select a plan action to inspect prices, timestamps, recipe evidence, station fee, "
            "Focus profile, liquidity, cash timing, and warnings."
        )
        actions.addWidget(self.action_table)
        actions.addWidget(self.action_detail)
        actions.setStretchFactor(0, 3)
        actions.setStretchFactor(1, 2)
        layout.addWidget(actions, 1)
        controls = QHBoxLayout()
        self.replan_button = QPushButton("Refresh & Replan from these inputs")
        self.replan_button.setEnabled(False)
        self.replan_button.clicked.connect(self.replan_displayed_snapshot)
        self.export_json_button = QPushButton("Export JSON")
        self.export_csv_button = QPushButton("Export CSV")
        self.export_json_button.setEnabled(False)
        self.export_csv_button.setEnabled(False)
        self.export_json_button.clicked.connect(self.export_displayed_json)
        self.export_csv_button.clicked.connect(self.export_displayed_csv)
        controls.addWidget(self.replan_button)
        controls.addStretch(1)
        controls.addWidget(self.export_json_button)
        controls.addWidget(self.export_csv_button)
        layout.addLayout(controls)
        return page

    def _build_recent_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Opening a snapshot never refreshes prices. It is labeled historical and remains "
            "immutable; Refresh & Replan creates a new snapshot."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.recent_table = QTableWidget(0, len(self.RECENT_HEADERS))
        self.recent_table.setHorizontalHeaderLabels(self.RECENT_HEADERS)
        self._configure_table(self.recent_table, sortable=True)
        self.recent_table.itemDoubleClicked.connect(lambda _item: self.open_selected_snapshot())
        layout.addWidget(self.recent_table, 1)
        controls = QHBoxLayout()
        self.open_snapshot_button = QPushButton("Open selected historical snapshot")
        self.open_snapshot_button.clicked.connect(self.open_selected_snapshot)
        self.refresh_snapshots_button = QPushButton("Refresh list")
        self.refresh_snapshots_button.clicked.connect(self.refresh_recent_snapshots)
        controls.addWidget(self.open_snapshot_button)
        controls.addWidget(self.refresh_snapshots_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        return page

    def _build_excluded_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.excluded_heading = QLabel(
            "Run a plan to inspect a bounded, profit-ordered view of excluded routes. "
            "Complete rejection counts remain in the immutable snapshot."
        )
        self.excluded_heading.setWordWrap(True)
        self.excluded_heading.setObjectName("muted")
        layout.addWidget(self.excluded_heading)
        self.excluded_table = QTableWidget(0, len(self.EXCLUDED_HEADERS))
        self.excluded_table.setHorizontalHeaderLabels(self.EXCLUDED_HEADERS)
        self._configure_table(self.excluded_table, sortable=True)
        self.excluded_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.excluded_table, 1)
        return page

    @staticmethod
    def _configure_table(table: QTableWidget, *, sortable: bool = False) -> None:
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.setSortingEnabled(sortable)

    @staticmethod
    def _silver_spin(value: int, *, allow_negative: bool = False) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setDecimals(0)
        widget.setRange(-1_000_000_000_000 if allow_negative else 0, 1_000_000_000_000)
        widget.setSingleStep(100_000)
        widget.setGroupSeparatorShown(True)
        widget.setValue(value)
        return widget

    def _connect_dirty_controls(self) -> None:
        for widget in (
            self.available_silver,
            self.silver_reserve,
            self.available_focus,
            self.focus_reserve,
            self.tier_min,
            self.tier_max,
            self.transport_cost,
            self.market_age,
            self.station_age,
            self.minimum_profit,
            self.minimum_roi,
            self.per_item_cap,
            self.volume_share,
            self.history_shortlist,
        ):
            widget.valueChanged.connect(self._mark_preflight_dirty)
        for widget in (
            self.region,
            self.sale_method,
            self.transport_policy,
            self.minimum_liquidity,
            self.arbitrage_scope,
        ):
            widget.currentIndexChanged.connect(self._mark_preflight_dirty)
        for widget in (
            self.premium,
            self.use_focus,
            self.craft_actions,
            self.refine_actions,
            self.arbitrage_actions,
            *self.refining_family_checks.values(),
            self.allow_stale_station,
            self.minimum_profit_enabled,
            self.minimum_roi_enabled,
            self.volume_share_enabled,
            self.history_enabled,
            self.force_refresh,
        ):
            widget.toggled.connect(self._mark_preflight_dirty)
        for widget in (
            self.item_query,
            self.enchantments,
            self.categories,
            self.material_cities,
            self.craft_cities,
            self.sell_cities,
            self.arbitrage_source_cities,
            self.arbitrage_destination_cities,
        ):
            widget.textChanged.connect(self._mark_preflight_dirty)

    def _toggle_advanced(self, visible: bool) -> None:
        if visible:
            self._simple_run_requested = False
        self.action_inputs.setVisible(visible)
        self.arbitrage_inputs.setVisible(visible)
        self.core_inputs.setVisible(visible)
        self.advanced.setVisible(visible)
        self.advanced_run_controls.setVisible(visible)
        self.simple_run_button.setVisible(not visible)
        self.tabs.setTabVisible(0, visible)
        self.tabs.setTabVisible(2, visible)
        self.plan_totals.setVisible(visible)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )
        self.advanced_toggle.setText("Simple Mode" if visible else "Advanced Mode")
        if not visible:
            self.tabs.setCurrentIndex(1)

    def _toggle_plan_details(self, visible: bool) -> None:
        self.plan_explanation.setVisible(visible)
        self.plan_details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )

    def _home_city_changed(self, city: str) -> None:
        if self._loading_controls or not city:
            return
        self._loading_controls = True
        try:
            self.material_cities.setText(city)
            self.craft_cities.setText(city)
            self.sell_cities.setText(city)
        finally:
            self._loading_controls = False
        self._mark_preflight_dirty()

    def _trust_preset_changed(self) -> None:
        preset = TrustPreset(str(self.trust_preset.currentData()))
        descriptions = {
            TrustPreset.FAST: (
                "Fast / broad: accepts market observations up to 24 hours old, allows unknown "
                "liquidity, and uses the explicit action cap without downloading history."
            ),
            TrustPreset.CAREFUL: (
                "Careful: uses market observations up to 4 hours old and historical liquidity "
                "when available, while retaining opportunities whose liquidity is unknown."
            ),
            TrustPreset.STRICT: (
                "Strict: requires observations no older than 2 hours, current station fees, "
                "history enrichment, and at least moderate liquidity."
            ),
        }
        self.trust_explanation.setText(descriptions[preset])
        if self._loading_controls:
            return
        self._loading_controls = True
        try:
            if preset is TrustPreset.FAST:
                self.market_age.setValue(24)
                self.station_age.setValue(168)
                self.allow_stale_station.setChecked(True)
                self.minimum_liquidity.setCurrentIndex(
                    self.minimum_liquidity.findData(MinimumLiquidity.ANY.value)
                )
                self.history_enabled.setChecked(False)
                self.volume_share_enabled.setChecked(False)
            elif preset is TrustPreset.CAREFUL:
                self.market_age.setValue(4)
                self.station_age.setValue(24)
                self.allow_stale_station.setChecked(False)
                self.minimum_liquidity.setCurrentIndex(
                    self.minimum_liquidity.findData(MinimumLiquidity.ANY.value)
                )
                self.history_enabled.setChecked(True)
                self.volume_share_enabled.setChecked(True)
                self.volume_share.setValue(20.0)
            else:
                self.market_age.setValue(2)
                self.station_age.setValue(12)
                self.allow_stale_station.setChecked(False)
                self.minimum_liquidity.setCurrentIndex(
                    self.minimum_liquidity.findData(MinimumLiquidity.MODERATE.value)
                )
                self.history_enabled.setChecked(True)
                self.volume_share_enabled.setChecked(True)
                self.volume_share.setValue(10.0)
        finally:
            self._loading_controls = False
        self._transport_changed()
        self._mark_preflight_dirty()

    def _refresh_focus_availability(self) -> None:
        service = self._service_backend(self.service)
        profile = service.crafting_profiles.load()
        configured = bool(
            profile is not None
            and (
                profile.assume_zero_for_unspecified
                or profile.complete_groups
                or profile.skill_levels
                or profile.manual_fce_overrides
            )
        )
        self.use_focus.setEnabled(configured)
        self.focus_setup_button.setVisible(not configured)
        if configured:
            self.focus_status.setText(
                "Configured profile found. Focus is used only for recipes whose specialization "
                "evidence resolves; valid non-Focus choices remain available."
            )
        else:
            self._loading_controls = True
            try:
                self.use_focus.setChecked(False)
            finally:
                self._loading_controls = False
            self.focus_status.setText("Focus profile not configured. Focus remains off.")

    @staticmethod
    def _service_backend(service) -> FindMoneyService:
        backend = getattr(service, "service", service)
        if not isinstance(backend, FindMoneyService):
            raise TypeError("Find Money service does not expose its evidence repositories")
        return backend

    def _transport_changed(self) -> None:
        policy = TransportPolicy(str(self.transport_policy.currentData()))
        self.transport_cost.setEnabled(policy is TransportPolicy.EXPLICIT_COST)

    def _mark_preflight_dirty(self, *_args) -> None:
        if self._loading_controls or self._thread is not None:
            return
        if self.preflight is not None:
            self.preflight = None
            self.run_button.setEnabled(False)
            self._set_status(
                (
                    "Inputs changed. Press FIND ME MONEY to check data and build a new plan."
                    if not self.advanced_toggle.isChecked()
                    else "Inputs changed. Run preflight again before refreshing data."
                ),
                "aging",
            )

    def _load_preferences(self) -> None:
        constraints = self.default_constraints
        if self.preferences is not None:
            try:
                constraints = self.preferences.load(constraints) or constraints
            except Exception as error:
                self._set_status(
                    f"Saved Find Me Money preferences could not be loaded: {error}",
                    "unknown",
                )
        self._apply_constraints(constraints)

    def _apply_constraints(self, constraints: FindMoneyConstraints) -> None:
        self._loading_controls = True
        try:
            self.available_silver.setValue(constraints.available_silver)
            self.silver_reserve.setValue(constraints.silver_reserve)
            self.available_focus.setValue(constraints.available_focus)
            self.focus_reserve.setValue(constraints.focus_reserve)
            self._select_combo(self.region, constraints.region.value)
            self.premium.setChecked(constraints.premium)
            self.use_focus.setChecked(constraints.use_focus)
            self.item_query.setText(constraints.item_query)
            self.tier_min.setValue(min(constraints.tiers))
            self.tier_max.setValue(max(constraints.tiers))
            self.enchantments.setText(
                ", ".join(str(value) for value in sorted(constraints.enchantments))
            )
            self.categories.setText(", ".join(sorted(constraints.categories)))
            self.craft_actions.setChecked(ActionKind.CRAFT in constraints.action_kinds)
            self.refine_actions.setChecked(ActionKind.REFINE in constraints.action_kinds)
            self.arbitrage_actions.setChecked(ActionKind.ARBITRAGE in constraints.action_kinds)
            for family, widget in self.refining_family_checks.items():
                widget.setChecked(family in constraints.refining_families)
            self.material_cities.setText(", ".join(constraints.material_cities))
            self.craft_cities.setText(", ".join(constraints.craft_cities))
            self.sell_cities.setText(", ".join(constraints.sell_cities))
            home = constraints.craft_cities[0]
            self.home_city.setCurrentIndex(max(self.home_city.findText(home), 0))
            self._select_combo(self.arbitrage_scope, constraints.arbitrage_scope.value)
            self.arbitrage_source_cities.setText(", ".join(constraints.arbitrage_source_cities))
            self.arbitrage_destination_cities.setText(
                ", ".join(constraints.arbitrage_destination_cities)
            )
            self._select_combo(self.sale_method, constraints.sale_method.value)
            self._select_combo(self.transport_policy, constraints.transport_policy.value)
            self.transport_cost.setValue(constraints.transport_cost_per_craft or 0)
            self.market_age.setValue(round(constraints.max_market_age.total_seconds() / 3_600))
            self.station_age.setValue(
                round(constraints.max_station_fee_age.total_seconds() / 3_600)
            )
            self.allow_stale_station.setChecked(constraints.allow_stale_station_fees)
            self.minimum_profit_enabled.setChecked(constraints.minimum_profit is not None)
            self.minimum_profit.setValue(constraints.minimum_profit or 0)
            self.minimum_roi_enabled.setChecked(constraints.minimum_roi is not None)
            self.minimum_roi.setValue((constraints.minimum_roi or 0) * 100)
            self._select_combo(self.minimum_liquidity, constraints.minimum_liquidity.value)
            self.per_item_cap.setValue(constraints.per_item_craft_cap)
            self.volume_share_enabled.setChecked(constraints.historical_volume_share is not None)
            self.volume_share.setValue((constraints.historical_volume_share or 0.20) * 100)
            self.history_enabled.setChecked(constraints.history_enabled)
            self.history_shortlist.setValue(constraints.history_shortlist_limit)
            self.force_refresh.setChecked(constraints.force_current_price_refresh)
            preset = self._infer_trust_preset(constraints)
            self.trust_preset.setCurrentIndex(self.trust_preset.findData(preset.value))
            self._trust_preset_changed()
            self._transport_changed()
        finally:
            self._loading_controls = False

    @staticmethod
    def _infer_trust_preset(constraints: FindMoneyConstraints) -> TrustPreset:
        hours = constraints.max_market_age.total_seconds() / 3_600
        if hours >= 24 and not constraints.history_enabled:
            return TrustPreset.FAST
        if (
            hours <= 2
            and constraints.history_enabled
            and constraints.minimum_liquidity.minimum_rank
            >= MinimumLiquidity.MODERATE.minimum_rank
        ):
            return TrustPreset.STRICT
        return TrustPreset.CAREFUL

    @staticmethod
    def _select_combo(widget: QComboBox, value: str) -> None:
        index = widget.findData(value)
        widget.setCurrentIndex(max(index, 0))

    def constraints(self) -> FindMoneyConstraints:
        tier_min = self.tier_min.value()
        tier_max = self.tier_max.value()
        if tier_min > tier_max:
            raise ValueError("Tier minimum cannot exceed tier maximum")
        transport = TransportPolicy(str(self.transport_policy.currentData()))
        return FindMoneyConstraints(
            available_silver=int(self.available_silver.value()),
            available_focus=self.available_focus.value(),
            region=Region(str(self.region.currentData())),
            silver_reserve=int(self.silver_reserve.value()),
            focus_reserve=self.focus_reserve.value(),
            premium=self.premium.isChecked(),
            item_query=self.item_query.text().strip(),
            tiers=frozenset(range(tier_min, tier_max + 1)),
            enchantments=self._parse_int_set(self.enchantments.text(), 0, 4, "enchantments"),
            categories=frozenset(self._parse_free_values(self.categories.text())),
            material_cities=self._parse_cities(self.material_cities.text()),
            craft_cities=self._parse_cities(self.craft_cities.text()),
            sell_cities=self._parse_cities(self.sell_cities.text()),
            use_focus=self.use_focus.isChecked(),
            max_market_age=timedelta(hours=self.market_age.value()),
            max_station_fee_age=timedelta(hours=self.station_age.value()),
            allow_stale_station_fees=self.allow_stale_station.isChecked(),
            minimum_profit=(
                int(self.minimum_profit.value())
                if self.minimum_profit_enabled.isChecked()
                else None
            ),
            minimum_roi=(
                self.minimum_roi.value() / 100 if self.minimum_roi_enabled.isChecked() else None
            ),
            minimum_liquidity=MinimumLiquidity(str(self.minimum_liquidity.currentData())),
            sale_method=SaleMethod(str(self.sale_method.currentData())),
            transport_policy=transport,
            transport_cost_per_craft=(
                int(self.transport_cost.value())
                if transport is TransportPolicy.EXPLICIT_COST
                else None
            ),
            per_item_craft_cap=self.per_item_cap.value(),
            historical_volume_share=(
                self.volume_share.value() / 100 if self.volume_share_enabled.isChecked() else None
            ),
            history_enabled=self.history_enabled.isChecked(),
            history_shortlist_limit=self.history_shortlist.value(),
            force_current_price_refresh=self.force_refresh.isChecked(),
            action_kinds=frozenset(
                kind
                for kind, checked in (
                    (ActionKind.CRAFT, self.craft_actions.isChecked()),
                    (ActionKind.REFINE, self.refine_actions.isChecked()),
                    (ActionKind.ARBITRAGE, self.arbitrage_actions.isChecked()),
                )
                if checked
            ),
            refining_families=frozenset(
                family
                for family, widget in self.refining_family_checks.items()
                if widget.isChecked()
            ),
            arbitrage_scope=ArbitrageScope(str(self.arbitrage_scope.currentData())),
            arbitrage_source_cities=self._parse_cities(self.arbitrage_source_cities.text()),
            arbitrage_destination_cities=self._parse_cities(
                self.arbitrage_destination_cities.text()
            ),
        )

    @staticmethod
    def _parse_free_values(text: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))

    @classmethod
    def _parse_cities(cls, text: str) -> tuple[str, ...]:
        requested = cls._parse_free_values(text)
        canonical = {city.casefold(): city for city in CITIES}
        unknown = tuple(value for value in requested if value.casefold() not in canonical)
        if unknown:
            raise ValueError("Unknown Albion city: " + ", ".join(unknown))
        values = tuple(dict.fromkeys(canonical[value.casefold()] for value in requested))
        if not values:
            raise ValueError("Each route city field must contain at least one city")
        return values

    @classmethod
    def _parse_int_set(
        cls,
        text: str,
        minimum: int,
        maximum: int,
        label: str,
    ) -> frozenset[int]:
        raw = cls._parse_free_values(text)
        try:
            values = frozenset(int(value) for value in raw)
        except ValueError as error:
            raise ValueError(f"{label.title()} must be comma-separated integers") from error
        if not values or any(value < minimum or value > maximum for value in values):
            raise ValueError(
                f"{label.title()} must contain values from {minimum} through {maximum}"
            )
        return values

    def find_money(self) -> None:
        """Run preflight and the complete refresh/plan pipeline from one player action."""

        if self._thread is not None:
            self._set_status("A Find Me Money run is already active.", "aging")
            return
        self._simple_run_requested = True
        self.station_setup.setVisible(False)
        self.price_setup.setVisible(False)
        if not self.prepare_preflight():
            return
        assert self.preflight is not None
        station_blockers = self._blocking_station_requirements(self.preflight)
        if station_blockers:
            self._render_station_setup(station_blockers)
            self._show_setup_required(station_blockers)
            return
        if not self._preflight_can_run():
            self._render_preflight_no_result(self.preflight)
            return
        self.start_plan()

    def prepare_preflight(self) -> bool:
        if self._thread is not None:
            return False
        try:
            constraints = self.constraints()
            service = (
                self.service_factory(constraints.region)
                if self.service_factory is not None
                else self.service
            )
            preflight = service.preflight(constraints)
        except Exception as error:
            self.preflight = None
            self.run_button.setEnabled(False)
            self._clear_preflight_tables()
            self._set_status(f"Preflight failed: {type(error).__name__}: {error}", "unknown")
            return False
        self.preflight = preflight
        self._preflight_service = service
        if self.preferences is not None:
            try:
                self.preferences.save(constraints)
            except Exception as error:
                self._set_status(
                    f"Preflight succeeded, but preferences were not saved: {error}", "aging"
                )
        self._render_preflight(preflight)
        has_blocker = any(
            reason.severity is PlanReasonSeverity.BLOCKING for reason in preflight.blockers
        )
        self.run_button.setEnabled(preflight.has_eligible_routes and not has_blocker)
        attention = len(preflight.attention_station_fees)
        refresh_keys = len(preflight.market_refresh.refresh_keys)
        state = "aging" if attention or preflight.blockers else "fresh"
        workload_note = (
            " A workload warning is shown; safety limits may make the result Approximate."
            if preflight.workload.warning is not None
            else " Planner workload is below the preflight warning thresholds."
        )
        self._set_status(
            f"Preflight complete: {len(preflight.eligible):,} eligible recipe routes and "
            f"{len(preflight.arbitrage_routes):,} arbitrage routes; "
            f"{attention:,} station fees need attention; {refresh_keys:,} current-price keys "
            f"need refresh in {preflight.market_refresh.estimated_batches:,} bounded batches. "
            f"Review this evidence, then explicitly run Refresh & Plan.{workload_note}",
            state,
        )
        self._render_simple_preflight_summary(preflight)
        self.tabs.setCurrentIndex(0 if self.advanced_toggle.isChecked() else 1)
        return True

    @staticmethod
    def _blocking_station_requirements(
        preflight: FindMoneyPreflight,
    ) -> tuple[StationFeeRequirement, ...]:
        return tuple(
            requirement
            for requirement in preflight.station_requirements
            if requirement.observation is None
            or requirement.freshness is Freshness.FUTURE
            or (
                requirement.freshness in {Freshness.STALE, Freshness.UNKNOWN}
                and not preflight.constraints.allow_stale_station_fees
            )
        )

    def _render_station_setup(
        self,
        requirements: tuple[StationFeeRequirement, ...],
    ) -> None:
        self._station_setup_requirements = requirements
        self.station_setup_table.setRowCount(len(requirements))
        for row, requirement in enumerate(requirements):
            observation = requirement.observation
            values = (
                requirement.city,
                requirement.station_type.display_name,
                "Missing" if observation is None else requirement.freshness.value.title(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.station_setup_table.setItem(row, column, item)
            self.station_setup_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    "" if observation is None else f"{observation.displayed_fee:g}"
                ),
            )
        self.station_setup_status.setText(
            f"{len(requirements):,} saved station fee"
            f"{'s' if len(requirements) != 1 else ''} need your input."
        )
        self.station_setup.setVisible(True)

    def _show_setup_required(
        self,
        requirements: tuple[StationFeeRequirement, ...],
    ) -> None:
        self.plan_banner.setText("SETUP REQUIRED")
        self._repolish(self.plan_banner, "unknown")
        names = ", ".join(
            f"{value.city} {value.station_type.display_name}" for value in requirements
        )
        self.simple_result_summary.setText(
            "Planning stopped before any market request because the following information can "
            f"only come from Albion: {names}. Enter it above, then Save & Continue."
        )
        self.tabs.setCurrentIndex(1)
        self._set_status(
            "Setup required: enter the missing or stale saved station fees to continue.",
            "unknown",
        )

    def save_station_fees_and_continue(self) -> None:
        if not self._station_setup_requirements:
            return
        parsed: list[tuple[StationFeeRequirement, float]] = []
        for row, requirement in enumerate(self._station_setup_requirements):
            item = self.station_setup_table.item(row, 3)
            raw = "" if item is None else item.text().strip().replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                self.station_setup_status.setText(
                    f"Enter a numeric displayed fee for {requirement.city} "
                    f"{requirement.station_type.display_name}."
                )
                return
            if value < 0:
                self.station_setup_status.setText("Displayed station fees cannot be negative.")
                return
            parsed.append((requirement, value))
        backend = self._service_backend(self._preflight_service)
        observed_at = self._manual_timestamp()
        for requirement, value in parsed:
            backend.preflight_planner.station_fees.set(
                StationFeeObservation(
                    requirement.region,
                    requirement.city,
                    requirement.station_type,
                    value,
                    observed_at,
                    Provenance.USER_OVERRIDE,
                )
            )
        self.evidence_saved.emit()
        self.station_setup_status.setText("Saved. Continuing Find Me Money…")
        self.find_money()

    def _render_price_setup(
        self,
        assessments: tuple[PriceRequirementAssessment, ...],
    ) -> None:
        unique: dict[tuple, PriceRequirementAssessment] = {}
        for assessment in assessments:
            requirement = assessment.requirement
            key = requirement.key
            unique[(key.region, key.item_id, key.city, key.quality, requirement.side)] = assessment
        requirements = tuple(
            unique[key]
            for key in sorted(
                unique,
                key=lambda value: (
                    value[0].value,
                    value[2].casefold(),
                    value[1],
                    value[3],
                    value[4].value,
                ),
            )
        )
        self._price_setup_requirements = requirements
        self.price_setup_table.setRowCount(len(requirements))
        for row, assessment in enumerate(requirements):
            requirement = assessment.requirement
            key = requirement.key
            values = (
                key.item_id,
                key.city,
                str(key.quality),
                requirement.side.value.replace("_", " ").title(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.price_setup_table.setItem(row, column, item)
            self.price_setup_table.setItem(
                row,
                4,
                QTableWidgetItem("" if assessment.price is None else f"{assessment.price:g}"),
            )
        self.price_setup_status.setText(
            f"{len(requirements):,} required market price"
            f"{'s remain' if len(requirements) != 1 else ' remains'} unavailable or too old."
        )
        self.price_setup.setVisible(bool(requirements))

    def save_price_overrides_and_continue(self) -> None:
        if not self._price_setup_requirements:
            return
        parsed: list[tuple[PriceRequirementAssessment, int]] = []
        for row, assessment in enumerate(self._price_setup_requirements):
            item = self.price_setup_table.item(row, 4)
            raw = "" if item is None else item.text().strip().replace(",", "")
            try:
                value = int(raw)
            except ValueError:
                self.price_setup_status.setText(
                    f"Enter a whole-silver price for {assessment.requirement.key.item_id}."
                )
                return
            if value <= 0:
                self.price_setup_status.setText("Market price overrides must be positive.")
                return
            parsed.append((assessment, value))
        backend = self._service_backend(self._preflight_service)
        entered_at = self._manual_timestamp()
        for assessment, value in parsed:
            requirement = assessment.requirement
            key = requirement.key
            backend.overrides.set(
                UserPriceOverride(
                    key.item_id,
                    key.city,
                    key.quality,
                    key.region,
                    requirement.side,
                    value,
                    entered_at,
                    Provenance.USER_OVERRIDE,
                )
            )
        self.evidence_saved.emit()
        self.price_setup_status.setText("Saved. Continuing Find Me Money…")
        self.find_money()

    def _manual_timestamp(self) -> datetime:
        if self.run_result is not None:
            return self.run_result.completed_at
        if self.preflight is not None:
            return self.preflight.created_at
        return datetime.now(UTC)

    def _clear_preflight_tables(self) -> None:
        for table in (
            self.preflight_counts,
            self.preflight_blockers,
            self.station_requirements,
        ):
            table.setRowCount(0)

    def _render_preflight(self, preflight: FindMoneyPreflight) -> None:
        self._clear_preflight_tables()
        summary_rows = [
            (field.name.replace("_", " ").title(), getattr(preflight.summary, field.name))
            for field in fields(preflight.summary)
        ]
        summary_rows.extend(
            (f"Rejected · {code.replace('_', ' ')}", count)
            for code, count in preflight.rejection_counts
        )
        self.preflight_counts.setRowCount(len(summary_rows))
        for row, (label, value) in enumerate(summary_rows):
            self.preflight_counts.setItem(row, 0, QTableWidgetItem(label))
            if isinstance(value, tuple):
                display = ", ".join(
                    item.value.title() if isinstance(item, ActionKind) else str(item)
                    for item in value
                )
            elif isinstance(value, StrEnum):
                display = value.value.replace("_", " ").title()
            elif isinstance(value, str):
                display = value
            else:
                display = f"{value:,}"
            self.preflight_counts.setItem(row, 1, SortableItem(display, value))

        blockers = list(preflight.blockers)
        self.preflight_blockers.setRowCount(len(blockers))
        for row, reason in enumerate(blockers):
            self.preflight_blockers.setItem(row, 0, QTableWidgetItem(reason.severity.value))
            self.preflight_blockers.setItem(row, 1, QTableWidgetItem(reason.code.value))
            self.preflight_blockers.setItem(row, 2, QTableWidgetItem(reason.message))
        if not blockers:
            self.preflight_blockers.setRowCount(1)
            self.preflight_blockers.setItem(0, 0, QTableWidgetItem("None"))
            self.preflight_blockers.setItem(0, 1, QTableWidgetItem("—"))
            self.preflight_blockers.setItem(
                0, 2, QTableWidgetItem("No plan-wide preflight blocker was found.")
            )

        requirements = preflight.station_requirements
        self.station_requirements.setRowCount(len(requirements))
        for row, requirement in enumerate(requirements):
            observation = requirement.observation
            station = requirement.station_type.display_name
            values = (
                requirement.region,
                requirement.city,
                station,
                "Missing" if observation is None else f"{observation.displayed_fee:,.0f}",
                requirement.freshness.value,
                f"{requirement.route_uses:,}",
                "Needs update in Settings" if requirement.needs_attention else "Ready",
            )
            for column, value in enumerate(values):
                self.station_requirements.setItem(row, column, QTableWidgetItem(value))
        self.preflight_heading.setText(
            f"Preflight built {preflight.created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"using {preflight.database_read_statements:,} bounded database read statements."
        )

    def _render_simple_preflight_summary(self, preflight: FindMoneyPreflight) -> None:
        summary = preflight.summary
        fresh = summary.fresh_cached_requirements + summary.aging_cached_requirements
        unavailable = summary.missing_current_requirements + summary.future_current_requirements
        self.market_summary.setText(
            "MARKET DATA · "
            f"{summary.required_current_price_keys:,} prices required · "
            f"{fresh:,} usable now · {summary.stale_current_requirements:,} stale · "
            f"{unavailable:,} unavailable/invalid · "
            f"{summary.refresh_requirements:,} will be refreshed."
        )
        supported = getattr(summary, "supported_catalog_recipes", summary.candidate_recipes)
        matched = getattr(summary, "matched_recipes", summary.candidate_recipes)
        static_ready = getattr(
            summary,
            "static_supported_matching_recipes",
            summary.candidate_recipes,
        )
        query_suffix = (
            f" {preflight.constraints.item_query!r}"
            if preflight.constraints.item_query
            else ""
        )
        self.simple_result_summary.setText(
            "SEARCH COVERAGE\n"
            f"Supported catalog: {supported:,}\n"
            f"Matched{query_suffix}: {matched:,}\n"
            f"Supported matching recipes: {static_ready:,}\n"
            f"Eligible production routes: {len(preflight.eligible):,}\n"
            f"Eligible arbitrage routes: {len(preflight.arbitrage_routes):,}"
        )

    def _render_preflight_no_result(self, preflight: FindMoneyPreflight) -> None:
        counts = dict(preflight.rejection_counts)
        unsupported = sum(
            count
            for code, count in counts.items()
            if code
            in {
                "unknown_item_value",
                "unknown_station_type",
                "unknown_returnability",
                "ambiguous_recipe",
                "untrusted_recipe",
                "unsupported_city_bonus",
            }
        )
        if unsupported:
            heading = "NOT ENOUGH SUPPORTED DATA TO KNOW"
            message = (
                "The selected catalog matches are outside verified production coverage. "
                "Unsupported static entries are excluded rather than asking you to invent Item "
                "Value or station mappings."
            )
        else:
            heading = "NO MATCHING ROUTES"
            message = (
                "No crafting, refining, or configured arbitrage route matches these filters and "
                "transport settings. Broaden the item search or review Advanced Mode."
            )
        self.plan_banner.setText(heading)
        self._repolish(self.plan_banner, "unknown")
        self.simple_result_summary.setText(message)
        self.tabs.setCurrentIndex(1)
        self._set_status(message, "unknown")

    def start_plan(self) -> None:
        if self._thread is not None:
            self._set_status("A Find Me Money run is already active.", "aging")
            return
        if self.preflight is None:
            self._set_status("Run FIND ME MONEY preflight before starting data refresh.", "unknown")
            return
        cancellation = PlanningCancellationToken()
        thread = QThread(self)
        worker = FindMoneyWorker(
            self._preflight_service,
            self.preflight,
            refresh_current=True,
            refresh_history=self.preflight.constraints.history_enabled,
            cancellation=cancellation,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Connect worker signals to QObject-bound slots. Context-free lambdas can
        # execute in the emitting worker thread in PySide, which would make the
        # UI rendering below an illegal cross-thread widget mutation.
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.error.connect(self._on_worker_failed)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self._cancellation = cancellation
        self._set_running(True)
        self._reset_stage_progress()
        self._set_status(
            "Starting explicit current-price refresh and planning pipeline...",
            "aging",
        )
        thread.start()

    def cancel_plan(self) -> None:
        if self._cancellation is None:
            return
        self._cancellation.cancel()
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("Cancellation requested")
        self._set_status(
            "Cancellation requested. The current safe batch/in-memory step will finish; valid "
            "cache writes remain intact.",
            "aging",
        )

    @Slot(object)
    def _on_worker_progress(self, value: PlanningProgress) -> None:
        if self._worker is not None:
            self._worker_progress(self._worker, value)

    @Slot(object)
    def _on_worker_finished(self, result: FindMoneyRunResult) -> None:
        if self._worker is not None:
            self._worker_finished(self._worker, result)

    @Slot(str)
    def _on_worker_failed(self, message: str) -> None:
        if self._worker is not None:
            self._worker_failed(self._worker, message)

    @Slot()
    def _on_thread_finished(self) -> None:
        sender = self.sender()
        if sender is self._thread and self._thread is not None:
            self._thread_finished(self._thread)

    def _worker_progress(self, worker: FindMoneyWorker, value: PlanningProgress) -> None:
        if worker is not self._worker:
            return
        self.stage_label.setText(f"{value.stage.value.replace('_', ' ').title()} · {value.message}")
        stage_index = self.STAGES.index(value.stage)
        stage_fraction = value.fraction or 0.0
        if value.stage is PlanningStage.COMPLETE:
            numeric = 100
        else:
            numeric = round(100 * (stage_index + stage_fraction) / len(self.STAGES))
        self.progress.setValue(numeric)
        self._set_status(value.message, "aging")

    def _worker_finished(self, worker: FindMoneyWorker, result: FindMoneyRunResult) -> None:
        if worker is not self._worker:
            return
        self.run_result = result
        if result.cancelled:
            self.progress.setValue(0)
            self.stage_label.setText("Cancelled")
            self._set_status(
                "Planning cancelled safely. No plan snapshot was created; completed cache writes "
                "remain valid.",
                "aging",
            )
        elif result.snapshot is None:
            self.progress.setValue(0)
            self.stage_label.setText("No actionable plan")
            self._set_status(
                "Planning completed without an actionable snapshot. Inspect rejection counts and "
                "preflight evidence.",
                "unknown",
            )
        else:
            self.progress.setValue(100)
            self.stage_label.setText("Complete")
            self._render_excluded(result)
            self._render_snapshot(result.snapshot, historical=False)
            if not result.snapshot.actions:
                unresolved = self._unresolved_price_requirements(result.preflight)
                if unresolved:
                    self._render_price_setup(unresolved)
                self._render_no_result(result, unresolved=bool(unresolved))
            self.refresh_recent_snapshots()
            self.plan_completed.emit(result.snapshot)

    @staticmethod
    def _unresolved_price_requirements(
        preflight: FindMoneyPreflight,
    ) -> tuple[PriceRequirementAssessment, ...]:
        return tuple(
            value
            for value in preflight.market_refresh.assessments
            if value.requirement.required_for_actionability
            and (
                value.price is None
                or value.freshness in {Freshness.STALE, Freshness.FUTURE, Freshness.UNKNOWN}
            )
        )

    def _render_no_result(self, result: FindMoneyRunResult, *, unresolved: bool) -> None:
        preflight = result.preflight
        initial = result.initial_evaluation
        fully_priced = len(initial.candidates) if initial is not None else 0
        profitable = (
            sum(
                max(
                    candidate.economics.nonfocused_profit_per_craft,
                    candidate.economics.focused_profit_per_craft or -(10**30),
                )
                > 0
                for candidate in initial.candidates
            )
            if initial is not None
            else 0
        )
        if unresolved:
            heading = "NOT ENOUGH DATA TO KNOW"
            message = (
                f"{preflight.summary.total_candidates:,} routes matched the search. "
                f"{fully_priced:,} were fully priced, while required AODP observations remain "
                "missing, stale, or invalid. Enter current Albion prices above or refresh again."
            )
        elif fully_priced:
            heading = "NO PROFIT FOUND"
            message = (
                f"WE CHECKED {fully_priced:,} FULLY-PRICED OPPORTUNITIES\n"
                f"{profitable:,} had positive per-unit economics before final bankroll, trust, "
                "and shared-capacity allocation. None produced a selectable plan under the "
                "current budget and policies."
            )
        else:
            heading = "NOT ENOUGH SUPPORTED DATA TO KNOW"
            message = (
                "Matching routes existed, but none had complete verified static and market "
                "evidence. Unsupported catalog coverage is kept out of the ordinary result."
            )
        self.plan_banner.setText(heading)
        self._repolish(self.plan_banner, "unknown" if unresolved or not fully_priced else "aging")
        self.simple_result_summary.setText(
            message + "\n\n" + self._player_rejection_summary(dict(result.rejection_counts))
        )
        self._set_status(heading.replace(" TO KNOW", "").title(), "unknown")
        self.tabs.setCurrentIndex(1)

    @staticmethod
    def _player_rejection_summary(rejections: Mapping[str, int]) -> str:
        groups = {
            "USER ACTION REQUIRED": {
                "missing_station_fee",
                "stale_station_fee",
                "future_station_fee",
                "future_user_override",
                "stale_user_override",
            },
            "MARKET DATA UNAVAILABLE": {
                "missing_material_price",
                "missing_output_price",
                "stale_market_data",
                "future_market_data",
                "stale_price",
                "future_timestamp",
                "unknown_timestamp",
            },
            "CURRENTLY UNPROFITABLE": {
                "validation_failed",
                "no_feasible_actions",
            },
            "UNSUPPORTED / STATIC-DATA COVERAGE": {
                "unknown_item_value",
                "unknown_station_type",
                "unknown_returnability",
                "ambiguous_recipe",
                "untrusted_recipe",
                "unsupported_city_bonus",
            },
            "ADVANCED TRUST / LIQUIDITY REJECTION": {
                "unknown_liquidity",
                "low_liquidity",
                "untrusted_provenance",
                "unverified_mechanics",
            },
        }
        lines: list[str] = []
        for label, codes in groups.items():
            count = sum(rejections.get(code, 0) for code in codes)
            if count:
                lines.append(f"{label}: {count:,}")
        return "\n".join(lines) or "No additional player-actionable rejection class was retained."

    def _worker_failed(self, worker: FindMoneyWorker, message: str) -> None:
        if worker is not self._worker:
            return
        self.progress.setValue(0)
        self.stage_label.setText("Failed")
        self._set_status(
            "Find Me Money failed without invalidating successful cache writes: " + message,
            "unknown",
        )

    def _thread_finished(self, thread: QThread) -> None:
        if thread is not self._thread:
            return
        self._thread = None
        self._worker = None
        self._cancellation = None
        self._set_running(False)
        if self._close_when_finished:
            self._close_when_finished = False
            self.close()

    def _set_running(self, running: bool) -> None:
        # A preflight is a reproducible contract for the worker. Keep the
        # controls immutable while that contract is executing so the visible
        # inputs can never drift away from the constraints used by the plan.
        self.simple_inputs.setEnabled(not running)
        self.action_inputs.setEnabled(not running)
        self.arbitrage_inputs.setEnabled(not running)
        self.core_inputs.setEnabled(not running)
        self.advanced_toggle.setEnabled(not running)
        self.advanced.setEnabled(not running)
        self.preflight_button.setEnabled(not running)
        self.run_button.setEnabled(not running and self._preflight_can_run())
        self.simple_run_button.setEnabled(not running)
        self.station_setup.setEnabled(not running)
        self.price_setup.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def _preflight_can_run(self) -> bool:
        if self.preflight is None or not self.preflight.has_eligible_routes:
            return False
        return not any(
            reason.severity is PlanReasonSeverity.BLOCKING for reason in self.preflight.blockers
        )

    def _reset_stage_progress(self) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_label.setText("Starting")

    def _render_snapshot(self, snapshot: PlanSnapshot, *, historical: bool) -> None:
        self.displayed_snapshot = snapshot
        age = age_text(snapshot.completed_at)
        if historical:
            headline = (
                f"HISTORICAL SNAPSHOT · generated {age} ago · prices have NOT been refreshed. "
                "This is not a current recommendation."
            )
            freshness = "aging"
            self.excluded_table.setRowCount(0)
            self.excluded_heading.setText(
                "This historical snapshot retains aggregate rejection counts, not the potentially "
                "large route-level near-miss set. Refresh & Replan to inspect current exclusions."
            )
        else:
            headline = (
                (
                    "BEST PLAN · "
                    f"{snapshot.plan_status.value.replace('_', ' ').title()} · "
                    f"{snapshot.optimizer.status.value.title()} optimization"
                )
                if snapshot.actions and self._simple_run_requested
                else (
                    f"PLAN STATUS: {snapshot.plan_status.value.replace('_', ' ').title()} · "
                    f"Optimization: {snapshot.optimizer.status.value.title()} · generated now."
                    if snapshot.actions
                    else "No profitable action is ready yet."
                )
            )
            freshness = "fresh" if snapshot.plan_status.value == "decision_grade" else "aging"
        self.plan_banner.setText(headline)
        self._repolish(self.plan_banner, freshness)
        if snapshot.actions:
            plan_roi = (
                None
                if snapshot.total_pre_revenue_cash <= 0
                else snapshot.total_expected_profit / snapshot.total_pre_revenue_cash
            )
            action_lines = []
            for index, action in enumerate(snapshot.actions, start=1):
                route = (
                    f"Buy in {action.route.buy_city} → sell in {action.route.sell_city}"
                    if action.action_kind is ActionKind.ARBITRAGE
                    else (
                        f"Buy in {action.route.material_city} → produce in "
                        f"{action.route.production_city} → sell in {action.route.sell_city}"
                    )
                )
                action_lines.extend(
                    (
                        f"{index}. {action.action_kind.value.upper()} — {action.display_name}",
                        f"   {route}",
                        f"   {action.quantity:,} action units/batches · cash needed "
                        f"{money(action.pre_revenue_cash_required)} · expected profit "
                        f"{money(action.expected_profit)}",
                    )
                )
            self.simple_result_summary.setText(
                "\n".join(
                    (
                        f"Expected profit       {money(snapshot.total_expected_profit)}",
                        f"Capital required      {money(snapshot.total_pre_revenue_cash)}",
                        f"Capital remaining     {money(snapshot.silver_remaining)}",
                        f"Expected ROI          {percent(plan_roi)}",
                        "",
                        f"{len(snapshot.actions):,} ACTION"
                        f"{'S' if len(snapshot.actions) != 1 else ''}",
                        "",
                        *action_lines,
                    )
                )
            )
        oldest = (
            "Unknown"
            if snapshot.oldest_market_observed_at is None
            else f"{age_text(snapshot.oldest_market_observed_at)} old"
        )
        station_oldest = (
            "N/A"
            if snapshot.data_health.station_fees_used == 0
            else (
                "Unknown"
                if snapshot.oldest_station_observed_at is None
                else f"{age_text(snapshot.oldest_station_observed_at)} old"
            )
        )
        completed_text = snapshot.completed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        routes_before = (
            snapshot.optimizer.candidate_routes_before_pruning or snapshot.optimizer.candidate_count
        )
        routes_after = (
            snapshot.optimizer.candidate_routes_after_pruning or snapshot.optimizer.candidate_count
        )
        self.plan_totals.setText(
            f"Expected profit {money(snapshot.total_expected_profit)} · "
            f"starting capital {money(snapshot.constraints.available_silver)} · "
            f"reserve {money(snapshot.constraints.silver_reserve)} · "
            f"capital used {money(snapshot.total_pre_revenue_cash)} · "
            f"capital remaining {money(snapshot.silver_remaining)} · "
            f"starting Focus {snapshot.constraints.available_focus:,} · "
            f"Focus reserve {snapshot.constraints.focus_reserve:,} · "
            f"Focus used {snapshot.total_focus:,} · Focus remaining {snapshot.focus_remaining:,}\n"
            f"Plan completed: {completed_text} "
            f"· oldest required current-price observation: {oldest} · oldest station fee: "
            f"{station_oldest}\n"
            f"Data health: {snapshot.data_health.market_observations_used:,} market observations "
            f"({snapshot.data_health.market_fresh:,} fresh, "
            f"{snapshot.data_health.market_stale:,} stale, "
            f"{snapshot.data_health.user_overrides_used:,} overrides); "
            f"{snapshot.data_health.station_fees_used:,} station fees "
            f"({snapshot.data_health.station_fees_fresh:,} fresh, "
            f"{snapshot.data_health.station_fees_stale:,} stale). Catalog "
            f"{snapshot.catalog_source_version}; mechanics {snapshot.mechanics_ruleset_id} "
            f"({snapshot.data_health.mechanics_status}).\n"
            f"Refresh: current {snapshot.current_refresh.batches_completed:,}/"
            f"{snapshot.current_refresh.batches_planned:,} batches, history "
            f"{snapshot.history_refresh.batches_completed:,}/"
            f"{snapshot.history_refresh.batches_planned:,} batches. "
            f"Optimizer: {snapshot.optimizer.method}; {snapshot.optimizer.candidate_count:,} "
            f"candidates ({routes_before:,} routes before pruning → {routes_after:,} after; "
            f"{snapshot.optimizer.candidate_local_modes_removed:,} local modes removed; "
            f"{snapshot.optimizer.equivalent_routes_collapsed:,} equivalents collapsed). "
            f"Quantity states {snapshot.optimizer.quantity_states_generated:,} generated → "
            f"{snapshot.optimizer.quantity_states_after_pruning:,} retained; portfolio states "
            f"{snapshot.optimizer.portfolio_states_considered:,} considered / "
            f"{snapshot.optimizer.portfolio_states_pruned:,} pruned; peak frontier "
            f"{snapshot.optimizer.peak_frontier_size:,}; total "
            f"{snapshot.optimizer.states_considered:,} transitions in "
            f"{snapshot.optimizer.elapsed_seconds:.3f}s.\n"
            f"Actions: {len(snapshot.actions):,} total · "
            f"{sum(action.action_kind is ActionKind.CRAFT for action in snapshot.actions):,} "
            f"crafting · "
            f"{sum(action.action_kind is ActionKind.REFINE for action in snapshot.actions):,} "
            f"refining · "
            f"{sum(action.action_kind is ActionKind.ARBITRAGE for action in snapshot.actions):,} "
            "arbitrage."
        )
        self._render_plan_explanation(snapshot)
        self._render_actions(snapshot, historical=historical)
        self.replan_button.setEnabled(True)
        self.export_json_button.setEnabled(True)
        self.export_csv_button.setEnabled(True)
        self.tabs.setCurrentIndex(1)
        if not historical:
            self._set_status(
                (
                    f"Plan ready: {len(snapshot.actions):,} actions and "
                    f"{snapshot.total_expected_profit:,} expected silver profit."
                    if snapshot.actions
                    else "Planning finished; classifying why no action is ready."
                ),
                freshness,
            )

    def _render_excluded(self, result: FindMoneyRunResult) -> None:
        selected_ids = {
            action.candidate_id
            for action in (result.snapshot.actions if result.snapshot is not None else ())
        }
        rows: dict[tuple[str, str], tuple[str, str, str, int | None, str]] = {}
        for evaluation in (result.initial_evaluation, result.final_evaluation):
            if evaluation is None:
                continue
            for miss in evaluation.near_misses:
                reason = "; ".join(value.message for value in miss.reasons)
                rows[(miss.candidate_id, reason)] = (
                    miss.action_kind.value.title(),
                    miss.display_name,
                    miss.route_text,
                    miss.expected_profit,
                    reason or "Required economics were incomplete.",
                )
        final_candidates = (
            result.final_evaluation.candidates if result.final_evaluation is not None else ()
        )
        for candidate in final_candidates:
            if candidate.candidate_id in selected_ids:
                continue
            profits = [
                candidate.economics.nonfocused_profit_per_craft
                if candidate.economics.nonfocused_eligible
                else None,
                candidate.economics.focused_profit_per_craft,
            ]
            expected_profit = max((value for value in profits if value is not None), default=None)
            reason = "; ".join(value.message for value in candidate.reasons)
            if not reason:
                reason = (
                    "Not selected by the shared silver, Focus, quantity, and profit-maximizing "
                    "allocation."
                )
            route = candidate.route
            route_text = (
                f"{route.buy_city} -> {route.sell_city}"
                if candidate.action_kind is ActionKind.ARBITRAGE
                else f"{route.material_city} -> {route.production_city} -> {route.sell_city}"
            )
            rows[(candidate.candidate_id, reason)] = (
                candidate.action_kind.value.title(),
                candidate.display_name,
                route_text,
                expected_profit,
                reason,
            )
        ordered = sorted(
            rows.values(),
            key=lambda value: (
                -(value[3] if value[3] is not None else -(10**30)),
                value[1].casefold(),
                value[2].casefold(),
                value[4],
            ),
        )
        visible = ordered[: self.MAX_EXCLUDED_ROWS]
        self.excluded_table.setSortingEnabled(False)
        self.excluded_table.setRowCount(len(visible))
        for row, values in enumerate(visible):
            display_values = (values[0], values[1], values[2], money(values[3]), values[4])
            for column, value in enumerate(display_values):
                self.excluded_table.setItem(row, column, QTableWidgetItem(value))
        self.excluded_table.setSortingEnabled(True)
        self.excluded_heading.setText(
            f"Showing {len(visible):,} of {len(ordered):,} retained excluded/near-miss routes, "
            "ordered by expected profit. Aggregate counts cover the full pipeline."
        )

    def _render_plan_explanation(self, snapshot: PlanSnapshot) -> None:
        metadata = dict(snapshot.metadata)
        rejection_counts: dict[str, int] = {}
        try:
            decoded = json.loads(metadata.get("rejection_counts", "{}"))
            if isinstance(decoded, Mapping):
                rejection_counts = {
                    str(key): int(value)
                    for key, value in decoded.items()
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            rejection_counts = {}
        explanation = build_plan_explanation(
            snapshot,
            rejection_counts=rejection_counts,
        )
        lines = ["PLAN REASONS"]
        if snapshot.reasons:
            lines.extend(
                f"- {reason.severity.value.upper()} · {reason.code.value}: {reason.message}"
                for reason in snapshot.reasons
            )
        else:
            lines.append("- No plan-level warning or blocker.")
        lines.extend(("", "WHY RESOURCES REMAIN"))
        lines.extend(f"- {value}" for value in explanation.unused_resources)
        if not explanation.unused_resources:
            lines.append("- No deployable resource remains unexplained.")
        lines.extend(("", "ASSUMPTIONS"))
        lines.extend(f"- {value}" for value in explanation.assumptions)
        lines.extend(("", "CANDIDATE REJECTIONS"))
        lines.extend(f"- {value}" for value in explanation.rejection_summary)
        if not explanation.rejection_summary:
            lines.append("- No rejection count was retained.")
        self.plan_explanation.setPlainText("\n".join(lines))

    def _render_actions(self, snapshot: PlanSnapshot, *, historical: bool) -> None:
        self.action_table.setSortingEnabled(False)
        self.action_table.setRowCount(len(snapshot.actions))
        for row, action in enumerate(snapshot.actions):
            is_arbitrage = action.action_kind is ActionKind.ARBITRAGE
            station = self._evidence_object(action, "station_fee")
            station_name = (
                "N/A"
                if is_arbitrage
                else str(station.get("station_type", "Unknown")).replace("_", " ").title()
            )
            station_fee = None if is_arbitrage else station.get("displayed_fee")
            actionability = self._actionability(action, snapshot)
            values: tuple[tuple[str, float | str | None], ...] = (
                (action.action_kind.value.title(), action.action_kind.value),
                (action.display_name, action.display_name),
                (f"{action.quantity:,}", action.quantity),
                (f"{action.focused_quantity:,}", action.focused_quantity),
                (f"{action.nonfocused_quantity:,}", action.nonfocused_quantity),
                (action.route.material_city, action.route.material_city),
                (
                    "N/A" if is_arbitrage else action.route.production_city,
                    "" if is_arbitrage else action.route.production_city,
                ),
                (action.route.sell_city, action.route.sell_city),
                (station_name, station_name),
                ("N/A" if is_arbitrage else money(station_fee), station_fee),
                (money(action.pre_revenue_cash_required), action.pre_revenue_cash_required),
                (money(action.effective_economic_cost), action.effective_economic_cost),
                (money(action.expected_revenue), action.expected_revenue),
                (money(action.expected_profit), action.expected_profit),
                (percent(action.roi), action.roi),
                (percent(action.margin), action.margin),
                (f"{action.focus_required:,}", action.focus_required),
                (money(action.silver_per_focus), action.silver_per_focus),
                (action.liquidity.value.title(), action.liquidity.value),
                (
                    age_text(action.oldest_market_observed_at),
                    action.oldest_market_observed_at.isoformat()
                    if action.oldest_market_observed_at
                    else None,
                ),
                (actionability, actionability),
            )
            for column, (display, sort_value) in enumerate(values):
                item = SortableItem(display, sort_value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.action_table.setItem(row, column, item)
        self.action_table.setSortingEnabled(True)
        self.action_detail.clear()
        self.action_detail.setProperty("historical", historical)
        if snapshot.actions:
            self.action_table.selectRow(0)

    @staticmethod
    def _actionability(action: PlanAction, snapshot: PlanSnapshot | None = None) -> str:
        if snapshot is not None and snapshot.plan_status is PlanStatus.NON_ACTIONABLE:
            return "Non-actionable"
        if any(reason.severity is PlanReasonSeverity.BLOCKING for reason in action.reasons):
            return "Non-actionable"
        if snapshot is not None and snapshot.plan_status is PlanStatus.ADVISORY:
            return "Advisory"
        if any(reason.severity is PlanReasonSeverity.WARNING for reason in action.reasons):
            return "Advisory"
        return "Decision-grade"

    def _show_selected_action(self) -> None:
        snapshot = self.displayed_snapshot
        selected = self.action_table.selectedItems()
        if snapshot is None or not selected:
            return
        item = self.action_table.item(selected[0].row(), 0)
        index = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(index, int) or not 0 <= index < len(snapshot.actions):
            return
        action = snapshot.actions[index]
        self.action_detail.setPlainText(self._action_detail_text(snapshot, action))

    def _action_detail_text(self, snapshot: PlanSnapshot, action: PlanAction) -> str:
        if action.action_kind is ActionKind.ARBITRAGE:
            return self._arbitrage_detail_text(snapshot, action)
        recipe = self._evidence_object(action, "recipe")
        accounting = self._evidence_object(action, "accounting")
        nonfocused_accounting = accounting.get("nonfocused_per_craft", {})
        focused_accounting = accounting.get("focused_per_craft", {})
        if not isinstance(nonfocused_accounting, Mapping):
            nonfocused_accounting = {}
        if not isinstance(focused_accounting, Mapping):
            focused_accounting = {}

        def mixed_total(key: str) -> float | None:
            nonfocused = nonfocused_accounting.get(key)
            focused = focused_accounting.get(key)
            if action.nonfocused_quantity and not isinstance(nonfocused, (int, float)):
                return None
            if action.focused_quantity and not isinstance(focused, (int, float)):
                return None
            return action.nonfocused_quantity * float(
                nonfocused or 0
            ) + action.focused_quantity * float(focused or 0)

        station = self._evidence_object(action, "station_fee")
        station_name = str(station.get("station_type", "Unknown")).replace("_", " ").title()
        action_label = action.action_kind.value.upper()
        production_verb = "REFINE" if action.action_kind is ActionKind.REFINE else "CRAFT"
        lines = [
            f"{action_label} — {action.display_name}",
            "",
            "BUY",
            action.route.material_city,
        ]
        materials = recipe.get("materials", [])
        if isinstance(materials, list):
            for material in materials:
                if isinstance(material, Mapping) and isinstance(
                    material.get("quantity"), (int, float)
                ):
                    lines.append(
                        f"- {self._quantity_text(float(material['quantity']) * action.quantity)} "
                        f"× {material.get('item_id', 'Unknown')}"
                    )
        if action.route.material_city.casefold() != action.route.production_city.casefold():
            lines.extend(
                (
                    "",
                    "MOVE",
                    f"{action.route.material_city} → {action.route.production_city}",
                )
            )
        lines.extend(
            (
                "",
                production_verb,
                f"{action.route.production_city} {station_name}",
                f"{action.quantity:,} batches · {action.focused_quantity:,} with Focus · "
                f"{action.nonfocused_quantity:,} without Focus",
                f"Expected Focus used: {action.focus_required:,}",
                "",
                "SELL",
                action.route.sell_city,
                action.sale_method.value.replace("_", " ").title(),
                f"Expected output: {action.output_units:,} × {action.display_name}",
                "",
                "EXPECTED",
                f"Pre-revenue silver: {money(action.pre_revenue_cash_required)}",
                f"Expected net profit: {money(action.expected_profit)}",
                f"ROI: {percent(action.roi)} · Liquidity: {action.liquidity.value.title()}",
                "",
                "EVIDENCE DETAIL",
            )
        )
        lines.extend(
            [
                f"{action.action_kind.value.upper()} — {action.display_name} ({action.item_id})",
                f"Actionability: {self._actionability(action, snapshot)}",
                f"Batches: {action.quantity:,} = {action.focused_quantity:,} focused + "
                f"{action.nonfocused_quantity:,} non-Focus; output units: {action.output_units:,}",
                f"Route: buy in {action.route.material_city} → "
                f"{action.action_kind.value} in {action.route.production_city} "
                f"→ sell in {action.route.sell_city}",
                f"Sale method: {action.sale_method.value.replace('_', ' ')}; quality: Normal",
                f"Pre-revenue cash: {money(action.pre_revenue_cash_required)}; economic cost: "
                f"{money(action.effective_economic_cost)}; expected revenue: "
                f"{money(action.expected_revenue)}; expected profit: "
                f"{money(action.expected_profit)}",
                f"ROI: {percent(action.roi)}; margin: {percent(action.margin)}; Focus: "
                f"{action.focus_required:,}; incremental Focus profit: "
                f"{money(action.incremental_focus_profit)}; incremental silver/Focus: "
                f"{money(action.silver_per_focus)}",
                f"Shared ceiling: {action.quantity_ceiling:,} production batches"
                + (
                    ""
                    if action.execution_ceiling_output_units is None
                    else f" and {action.execution_ceiling_output_units:,} "
                    "reported-volume output units"
                ),
                "",
                "CASH TIMING",
                "Gross materials, station cost, explicit transport, and sell-order setup cash are "
                "required before revenue. Transaction tax is deducted when sold. Expected returns "
                "affect economic cost/profit but never fund another V0.6 action.",
                "Gross material purchase cash: "
                f"{money(mixed_total('gross_material_purchase_cash'))}",
                f"Station cash: {money(mixed_total('station_cash'))}",
                f"Listing/setup cash: {money(mixed_total('listing_setup_cash'))}",
                f"Explicit transport cash: "
                f"{money(action.route.transport_cost_per_craft * action.quantity)}",
                f"Transaction tax deducted on sale: {money(mixed_total('transaction_tax'))}",
                f"Expected returned-material acquisition cost basis: "
                f"{money(mixed_total('returned_material_cost_basis_value'))}",
            ]
        )
        if action.reasons:
            lines.extend(("", "WARNINGS / REASONS"))
            lines.extend(
                f"- {reason.severity.value.upper()} · {reason.code.value}: {reason.message}"
                for reason in action.reasons
            )

        lines.extend(("", "RECIPE / MATERIALS"))
        lines.append(
            f"Static source: {recipe.get('source_version') or 'Unknown'}; output / batch: "
            f"{recipe.get('output_quantity', 'Unknown')}"
        )
        lines.append(
            f"Action type={recipe.get('action_kind', action.action_kind.value)}; production "
            f"group={recipe.get('production_group', 'Unknown')}; tier="
            f"{recipe.get('tier', 'Unknown')}; enchantment={recipe.get('enchantment', 0)}; "
            f"Item Value={recipe.get('item_value', 'Unknown')}; base Focus="
            f"{recipe.get('base_focus_cost', 'Unknown')}"
        )
        materials = recipe.get("materials", [])
        if isinstance(materials, list):
            for material in materials:
                if not isinstance(material, Mapping):
                    continue
                returnable = material.get("returnable")
                per_craft = material.get("quantity")
                gross_required = (
                    float(per_craft) * action.quantity
                    if isinstance(per_craft, (int, float))
                    else None
                )
                nonfocused_rate = nonfocused_accounting.get("return_rate")
                focused_rate = focused_accounting.get("return_rate")
                expected_returned = None
                if returnable is True and isinstance(per_craft, (int, float)):
                    if (
                        not action.nonfocused_quantity or isinstance(nonfocused_rate, (int, float))
                    ) and (not action.focused_quantity or isinstance(focused_rate, (int, float))):
                        expected_returned = float(per_craft) * (
                            action.nonfocused_quantity * float(nonfocused_rate or 0)
                            + action.focused_quantity * float(focused_rate or 0)
                        )
                lines.append(
                    f"- {material.get('item_id', 'Unknown')}: "
                    f"{self._quantity_text(gross_required)} gross required "
                    f"({self._quantity_text(per_craft)} / batch) from "
                    f"{action.route.material_city}; returnable="
                    f"{returnable if returnable is not None else 'Unknown'}; expected returned "
                    f"quantity={self._quantity_text(expected_returned)}"
                )

        prices = self._evidence_value(action, "prices", [])
        lines.extend(("", "CURRENT REQUIRED PRICE EVIDENCE (not history)"))
        if isinstance(prices, list):
            for price in prices:
                if not isinstance(price, Mapping):
                    continue
                lines.append(
                    f"- {price.get('role', 'price')} · {price.get('item_id', 'Unknown')} · "
                    f"{price.get('city', 'Unknown')} · {price.get('side', 'Unknown')}: "
                    f"{money(price.get('price'))}; observed "
                    f"{price.get('observed_at') or 'Unknown'}; "
                    f"source={price.get('provenance', 'Unknown')}; "
                    f"freshness={price.get('freshness', 'Unknown')}"
                )

        station = self._evidence_object(action, "station_fee")
        lines.extend(("", "STATION FEE"))
        lines.append(
            f"{station.get('station_type', 'Unknown')} in "
            f"{station.get('city', action.route.production_city)}: "
            f"displayed fee {money(station.get('displayed_fee'))}; observed "
            f"{station.get('observed_at', 'Unknown')}; "
            f"source={station.get('provenance', 'Unknown')}"
        )
        focus = self._evidence_object(action, "focus")
        lines.extend(("", "FOCUS INPUT"))
        lines.append(
            f"Eligible={focus.get('eligible', False)}; effective FCE="
            f"{focus.get('fce', 'Unknown')}; source={focus.get('source', 'Unknown')}; "
            f"mapping={focus.get('mapping_key', 'Unknown')}; mapping dataset="
            f"{focus.get('mapping_source_version', 'Unknown')}"
        )
        liquidity = self._evidence_object(action, "liquidity")
        ceiling = self._evidence_object(action, "quantity_ceiling")
        lines.extend(("", "REPORTED HISTORY / LIQUIDITY (not live order depth)"))
        if liquidity:
            lines.append(
                f"Liquidity={liquidity.get('level', action.liquidity.value)}; reported volume="
                f"{liquidity.get('reported_volume', 'Unknown')}; 24h/history weighted mean="
                f"{money(liquidity.get('weighted_mean_price'))}; last reported activity="
                f"{liquidity.get('last_activity_at') or 'Unknown'}"
            )
            for reason in liquidity.get("reasons", []):
                lines.append(f"- {reason}")
        else:
            lines.append("No complete history evidence was retained for this action.")
        if ceiling:
            lines.append(
                "Execution ceiling source="
                f"{ceiling.get('source', 'Unknown')}; exact reported 24h volume="
                f"{ceiling.get('reported_24h_volume', 'Unknown')}; applied share="
                f"{percent(ceiling.get('historical_volume_share'))}; "
                f"rationale={ceiling.get('explanation', 'Unknown')}"
            )
        transport = self._evidence_object(action, "transport")
        mechanics = self._evidence_object(action, "mechanics")
        nonfocused_bonus = mechanics.get("nonfocused_city_bonus", {})
        focused_bonus = mechanics.get("focused_city_bonus", {})
        if not isinstance(nonfocused_bonus, Mapping):
            nonfocused_bonus = {}
        if not isinstance(focused_bonus, Mapping):
            focused_bonus = {}
        lines.extend(
            (
                "",
                "TRANSPORT / MECHANICS",
                f"Transport policy={transport.get('policy', action.route.transport_policy.value)}; "
                f"explicit cost/batch={money(transport.get('cost_per_craft'))}",
                f"Ruleset={mechanics.get('ruleset_id', snapshot.mechanics_ruleset_id)}; "
                f"verification={mechanics.get('status', 'Unknown')}; city bonus dataset="
                f"{mechanics.get('city_bonus_dataset', 'Unknown')}",
                "Non-Focus city classification="
                f"{nonfocused_bonus.get('classification', 'Unknown')}; verified "
                f"{nonfocused_bonus.get('verified_on', 'Unknown')}; source="
                f"{nonfocused_bonus.get('source', 'Unknown')}",
                f"Non-Focus production bonus="
                f"{percent(nonfocused_bonus.get('total_production_bonus'))}; RRR="
                f"{percent(nonfocused_accounting.get('return_rate'))}",
                f"Focus production bonus={percent(focused_bonus.get('total_production_bonus'))}; "
                f"RRR={percent(focused_accounting.get('return_rate'))}",
                "Production-city returned-material market value (informational only): "
                f"{money(mixed_total('returned_material_craft_city_market_value'))}; "
                "acquisition cost basis is used once in expected profit.",
                "",
                "PLAN ASSUMPTIONS",
            )
        )
        lines.extend(f"- {assumption}" for assumption in snapshot.assumptions)
        return "\n".join(lines)

    def _arbitrage_detail_text(self, snapshot: PlanSnapshot, action: PlanAction) -> str:
        accounting = self._evidence_object(action, "arbitrage_accounting")
        marketplace = self._evidence_object(action, "marketplace")
        capacity = self._evidence_object(action, "capacity_evidence")
        transport = self._evidence_object(action, "transport")
        prices = self._evidence_value(action, "prices", [])
        ceilings = self._evidence_value(action, "capacity_ceilings", [])
        price_by_role = (
            {
                str(value.get("role")): value.get("price")
                for value in prices
                if isinstance(value, Mapping)
            }
            if isinstance(prices, list)
            else {}
        )
        source_capacity = capacity.get("source")
        destination_capacity = capacity.get("destination")
        source_liquidity = (
            source_capacity.get("level", "Unknown")
            if isinstance(source_capacity, Mapping)
            else "Unknown"
        )
        destination_liquidity = (
            destination_capacity.get("level", "Unknown")
            if isinstance(destination_capacity, Mapping)
            else "Unknown"
        )
        transport_per_unit = transport.get(
            "cost_per_action_unit",
            action.route.transport_cost_per_action_unit,
        )
        lines = [
            f"ARBITRAGE — {action.display_name} ({action.item_id})",
            f"Actionability: {self._actionability(action, snapshot)}",
            "",
            "BUY",
            f"{action.quantity:,} × {action.display_name} in {action.route.buy_city}",
            f"Expected unit purchase from minimum sell orders: "
            f"{money(price_by_role.get('arbitrage_source'))}",
            "",
            "MOVE",
            f"{action.route.buy_city} → {action.route.sell_city}",
            f"Transport policy={transport.get('policy', action.route.transport_policy.value)}; "
            "explicit cost/unit="
            f"{money(transport_per_unit)}",
            "",
            "SELL",
            f"{action.route.sell_city} via {action.sale_method.value.replace('_', ' ').title()}",
            f"Expected unit sale: {money(price_by_role.get('arbitrage_destination'))}",
            "",
            "EXPECTED / CASH TIMING",
            f"Purchase cash: {money(float(accounting.get('purchase_cash', 0)) * action.quantity)}",
            f"Sell-order setup cash: "
            f"{money(float(accounting.get('setup_cash', 0)) * action.quantity)}",
            f"Explicit transport cash: "
            f"{money(float(accounting.get('transport_cash', 0)) * action.quantity)}",
            f"Pre-revenue silver: {money(action.pre_revenue_cash_required)}",
            f"Transaction tax deducted only from sale proceeds: "
            f"{money(float(accounting.get('transaction_tax', 0)) * action.quantity)}",
            f"Gross destination value: {money(action.expected_revenue)}; net sale proceeds: "
            f"{money(float(accounting.get('net_sale_proceeds', 0)) * action.quantity)}",
            f"Economic cost: "
            f"{money(action.effective_economic_cost)}; net profit: {money(action.expected_profit)}",
            f"ROI: {percent(action.roi)}; margin: {percent(action.margin)}; liquidity: "
            f"{action.liquidity.value.title()}",
            "Focus: N/A · station fee: N/A · resource return rate: N/A · FCE: N/A",
            "",
            "CURRENT REQUIRED PRICE EVIDENCE (top-of-book snapshot, not order depth)",
        ]
        if isinstance(prices, list):
            for price in prices:
                if not isinstance(price, Mapping):
                    continue
                lines.append(
                    f"- {price.get('role', 'price')} · {price.get('city', 'Unknown')} · "
                    f"{price.get('side', 'Unknown')}: {money(price.get('price'))}; observed "
                    f"{price.get('observed_at') or 'Unknown'}; "
                    f"source={price.get('provenance', 'Unknown')}; "
                    f"freshness={price.get('freshness', 'Unknown')}"
                )
        lines.extend(
            (
                "",
                "MARKETPLACE MECHANICS",
                f"Ruleset={marketplace.get('ruleset_id', snapshot.mechanics_ruleset_id)}; "
                f"setup rate={percent(marketplace.get('setup_rate'))}; tax rate="
                f"{percent(marketplace.get('transaction_tax_rate'))}; status="
                f"{marketplace.get('marketplace_fee_status', 'Unknown')}",
                "",
                "SHARED SOURCE + DESTINATION CAPACITY",
                str(capacity.get("warning", "History is an execution proxy, not live depth.")),
                f"Source acquisition liquidity: {str(source_liquidity).title()}",
                f"Destination liquidation liquidity: {str(destination_liquidity).title()}",
            )
        )
        if isinstance(ceilings, list):
            for ceiling in ceilings:
                if not isinstance(ceiling, Mapping):
                    continue
                key = ceiling.get("key", ())
                city = key[2] if isinstance(key, list) and len(key) > 2 else "Unknown"
                market_units = ceiling.get("maximum_market_units")
                capacity_text = (
                    str(market_units)
                    if market_units is not None
                    else f"fallback {ceiling.get('maximum_action_units', 'Unknown')}"
                )
                lines.append(
                    f"- {ceiling.get('role', 'capacity')} in {city}: "
                    f"{capacity_text} "
                    f"units; source={ceiling.get('source', 'Unknown')}; "
                    f"rationale={ceiling.get('explanation', 'Unknown')}"
                )
        if action.reasons:
            lines.extend(("", "WARNINGS / REASONS"))
            lines.extend(
                f"- {reason.severity.value.upper()} · {reason.code.value}: {reason.message}"
                for reason in action.reasons
            )
        lines.extend(("", "PLAN ASSUMPTIONS"))
        lines.extend(f"- {assumption}" for assumption in snapshot.assumptions)
        return "\n".join(lines)

    @staticmethod
    def _quantity_text(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "Unknown"
        return f"{value:,.3f}".rstrip("0").rstrip(".")

    @staticmethod
    def _evidence_value(action: PlanAction, key: str, default: Any) -> Any:
        raw = dict(action.evidence).get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    @classmethod
    def _evidence_object(cls, action: PlanAction, key: str) -> dict[str, Any]:
        value = cls._evidence_value(action, key, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def refresh_recent_snapshots(self) -> None:
        self.recent_table.setSortingEnabled(False)
        self.recent_table.setRowCount(0)
        if self.snapshots is None:
            self.open_snapshot_button.setEnabled(False)
            self.refresh_snapshots_button.setEnabled(False)
            return
        try:
            summaries = self.snapshots.list_summaries()
        except Exception as error:
            self._set_status(f"Recent snapshots could not be loaded: {error}", "unknown")
            return
        self.open_snapshot_button.setEnabled(True)
        self.recent_table.setRowCount(len(summaries))
        for row, summary in enumerate(summaries):
            values: tuple[tuple[str, float | str | None], ...] = (
                (
                    summary.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                    summary.created_at.timestamp(),
                ),
                (summary.plan_status.value.replace("_", " ").title(), summary.plan_status.value),
                (summary.optimization_status.value.title(), summary.optimization_status.value),
                (summary.region.display_name, summary.region.value),
                (f"{summary.action_count:,}", summary.action_count),
                (money(summary.total_pre_revenue_cash), summary.total_pre_revenue_cash),
                (f"{summary.total_focus:,}", summary.total_focus),
                (money(summary.total_expected_profit), summary.total_expected_profit),
                (summary.snapshot_id, summary.snapshot_id),
            )
            for column, (display, sort_value) in enumerate(values):
                item = SortableItem(display, sort_value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, summary.snapshot_id)
                self.recent_table.setItem(row, column, item)
        self.recent_table.setSortingEnabled(True)

    def open_selected_snapshot(self) -> None:
        if self.snapshots is None:
            return
        selected = self.recent_table.selectedItems()
        if not selected:
            self._set_status("Select a recent snapshot first.", "aging")
            return
        first = self.recent_table.item(selected[0].row(), 0)
        snapshot_id = first.data(Qt.ItemDataRole.UserRole) if first is not None else None
        if not isinstance(snapshot_id, str):
            return
        try:
            snapshot = self.snapshots.load(snapshot_id)
        except Exception as error:
            self._set_status(f"Historical snapshot could not be opened: {error}", "unknown")
            return
        if snapshot is None:
            self._set_status("That historical snapshot no longer exists.", "unknown")
            self.refresh_recent_snapshots()
            return
        self._render_snapshot(snapshot, historical=True)
        self._set_status(
            "Historical snapshot opened. Its prices have NOT been refreshed; use Refresh & "
            "Replan to build a new immutable plan.",
            "aging",
        )

    def replan_displayed_snapshot(self) -> None:
        if self.displayed_snapshot is None or self._thread is not None:
            return
        self._apply_constraints(self.displayed_snapshot.constraints)
        self.preflight = None
        self.run_button.setEnabled(False)
        self.prepare_preflight()

    def export_displayed_json(self) -> None:
        self._choose_export("json")

    def export_displayed_csv(self) -> None:
        self._choose_export("csv")

    def _choose_export(self, kind: str) -> None:
        snapshot = self.displayed_snapshot
        if snapshot is None:
            return
        suffix = ".json" if kind == "json" else ".csv"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            f"Export plan {kind.upper()}",
            f"{snapshot.snapshot_id}{suffix}",
            "JSON (*.json)" if kind == "json" else "CSV (*.csv)",
        )
        if not selected:
            return
        path = Path(selected)
        if path.suffix.casefold() != suffix:
            path = path.with_suffix(suffix)
        try:
            written = (
                export_plan_json(snapshot, path)
                if kind == "json"
                else export_plan_csv(snapshot, path)
            )
        except Exception as error:
            self._set_status(f"Plan export failed: {error}", "unknown")
            return
        self._set_status(f"Exported immutable plan to {written}", "fresh")

    def _set_status(self, text: str, freshness: str) -> None:
        self.status.setText(text)
        self._repolish(self.status, freshness)

    @staticmethod
    def _repolish(widget: QWidget, freshness: str) -> None:
        widget.setProperty("freshness", freshness)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        thread = self._thread
        if thread is not None and thread.isRunning():
            self.cancel_plan()
            if not thread.wait(5_000):
                self._close_when_finished = True
                event.ignore()
                return
        super().closeEvent(event)
