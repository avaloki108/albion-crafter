from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from albion_crafter.core.actionability import ReasonCode
from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.crafting_profile import (
    CraftingSkillProfile,
    FocusEfficiencyResolution,
    focus_skill_mapping_for_recipe,
)
from albion_crafter.core.freshness import Freshness, FreshnessPolicy
from albion_crafter.core.models import CraftingContext, CraftResult, Recipe, SaleMethod
from albion_crafter.core.stations import (
    StationFeeObservation,
    StationType,
    station_type_for_item,
)
from albion_crafter.data.cities import CITIES
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import SettingsRepository
from albion_crafter.database.v3 import CraftingProfileRepository, StationFeeRepository
from albion_crafter.market.models import MarketSide, Region
from albion_crafter.market.pricing import PriceResolver, PricingSnapshot
from albion_crafter.market.recipe_refresh import (
    RecipePriceAvailabilityStatus,
    RecipePriceRefreshProgress,
    RecipePriceRefreshRequest,
    RecipePriceRefreshResult,
    RecipePriceRefreshService,
)

from .calculator_refresh_worker import CalculatorRefreshWorker
from .common import age_text, money, percent
from .settings_view import DEFAULT_SETTINGS

RefreshServiceFactory = Callable[[Region], RecipePriceRefreshService]


class CalculatorView(QWidget):
    """A cache-first calculator with an explicit, bounded network refresh action."""

    open_market_data_requested = Signal()
    open_settings_requested = Signal()
    station_fee_saved = Signal()
    prices_refreshed = Signal()

    def __init__(
        self,
        resolver: PriceResolver,
        settings: SettingsRepository,
        catalog: CatalogRepository,
        station_fees: StationFeeRepository | None = None,
        crafting_profiles: CraftingProfileRepository | None = None,
        *,
        refresh_service: RecipePriceRefreshService | None = None,
        refresh_service_factory: RefreshServiceFactory | None = None,
    ) -> None:
        super().__init__()
        if refresh_service is not None and refresh_service_factory is not None:
            raise ValueError("provide a refresh service or factory, not both")
        self.resolver = resolver
        self.settings = settings
        self.catalog = catalog
        self.station_fees = station_fees
        self.crafting_profiles = crafting_profiles
        self.calculator = CraftCalculator()
        self._refresh_service = refresh_service
        self._refresh_service_factory = refresh_service_factory
        self._region_refresh_services: dict[Region, RecipePriceRefreshService] = {}
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[CalculatorRefreshWorker] = set()
        self._closing = False
        self._loading_controls = False
        self._current_recipe: Recipe | None = None
        self._current_result: CraftResult | None = None
        self._current_snapshot: PricingSnapshot | None = None
        self.value_labels: dict[str, QLabel] = {}
        self.summary_labels: dict[str, QLabel] = {}
        self.summary_hints: dict[str, QLabel] = {}

        outer = QVBoxLayout(self)
        title = QLabel("Production Calculator")
        title.setObjectName("pageTitle")
        outer.addWidget(title)
        intro = QLabel(
            "Choose what to craft or refine. Totals update from saved data; prices are "
            "downloaded only when you press the refresh button."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        root = QVBoxLayout(body)

        self._build_scenario(root)

        self.clean_catalog_button = QPushButton("Open Market Data to install recipes")
        self.clean_catalog_button.setVisible(False)
        self.clean_catalog_button.clicked.connect(self.open_market_data_requested.emit)
        root.addWidget(self.clean_catalog_button)

        self.data_banner = QLabel("Choose a producible item to see totals.")
        self.data_banner.setWordWrap(True)
        self.data_banner.setObjectName("dataBanner")
        self._set_banner_state("unknown")
        root.addWidget(self.data_banner)

        self._build_station_fee_prompt(root)
        self._build_refresh_controls(root)
        self._build_summary(root)
        self._build_details(root)
        root.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self._search_catalog)
        self.item_search.textChanged.connect(lambda: self.search_timer.start())
        self.item_results.currentIndexChanged.connect(self.calculate)
        for signal in (
            self.quantity.valueChanged,
            self.quality.valueChanged,
            self.material_buy_city.currentIndexChanged,
            self.craft_city.currentIndexChanged,
            self.sell_city.currentIndexChanged,
            self.sale_method.currentIndexChanged,
            self.focus.toggled,
            self.premium.toggled,
        ):
            signal.connect(self._economic_control_changed)

        self.load_defaults()
        self._search_catalog()

    def _build_scenario(self, root: QVBoxLayout) -> None:
        inputs = QGroupBox("What are you producing?")
        grid = QGridLayout(inputs)
        self.item_search = QLineEdit()
        self.item_search.setPlaceholderText("Search by item name or canonical Albion ID…")
        self.item_results = QComboBox()
        self.item_results.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.item_results.setMinimumContentsLength(32)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 10_000)
        self.output_count_label = QLabel("Produces —")
        self.output_count_label.setObjectName("muted")
        self.materials_needed_label = QLabel("Materials to buy: —")
        self.materials_needed_label.setWordWrap(True)
        self.materials_needed_label.setObjectName("muted")

        self.material_buy_city = QComboBox()
        self.material_buy_city.addItems(CITIES)
        self.craft_city = QComboBox()
        self.craft_city.addItems(CITIES)
        self.sell_city = QComboBox()
        self.sell_city.addItems(CITIES)
        self.sale_method = QComboBox()
        self.sale_method.addItem(
            "Sell order · use current lowest offer", SaleMethod.SELL_ORDER.value
        )
        self.sale_method.addItem(
            "Instant sale · use current highest buy order", SaleMethod.INSTANT_SELL.value
        )
        self.focus = QCheckBox("Use Focus returns")
        self.premium = QCheckBox("Premium account")
        self.focus_profile_hint = QLabel()
        self.focus_profile_hint.setWordWrap(True)
        self.focus_profile_hint.setObjectName("muted")

        grid.addWidget(QLabel("Find item"), 0, 0)
        grid.addWidget(self.item_search, 0, 1, 1, 5)
        grid.addWidget(QLabel("Selected recipe"), 1, 0)
        grid.addWidget(self.item_results, 1, 1, 1, 3)
        grid.addWidget(QLabel("Number of batches"), 1, 4)
        grid.addWidget(self.quantity, 1, 5)
        grid.addWidget(self.output_count_label, 2, 1, 1, 5)
        grid.addWidget(self.materials_needed_label, 3, 1, 1, 5)
        grid.addWidget(QLabel("Buy materials in"), 4, 0)
        grid.addWidget(self.material_buy_city, 4, 1)
        grid.addWidget(QLabel("Produce in"), 4, 2)
        grid.addWidget(self.craft_city, 4, 3)
        grid.addWidget(QLabel("Sell in"), 4, 4)
        grid.addWidget(self.sell_city, 4, 5)
        grid.addWidget(QLabel("Sell using"), 5, 0)
        grid.addWidget(self.sale_method, 5, 1, 1, 3)
        assumptions = QHBoxLayout()
        assumptions.addWidget(self.premium)
        assumptions.addWidget(self.focus)
        assumptions.addStretch(1)
        grid.addLayout(assumptions, 5, 4, 1, 2)
        grid.addWidget(self.focus_profile_hint, 6, 1, 1, 5)
        root.addWidget(inputs)

    def _build_station_fee_prompt(self, root: QVBoxLayout) -> None:
        self.station_fee_prompt = QGroupBox("Station fee needed")
        layout = QVBoxLayout(self.station_fee_prompt)
        self.station_fee_prompt_text = QLabel()
        self.station_fee_prompt_text.setWordWrap(True)
        layout.addWidget(self.station_fee_prompt_text)
        entry = QHBoxLayout()
        entry.addWidget(QLabel("Displayed usage fee"))
        self.station_fee_input = QDoubleSpinBox()
        self.station_fee_input.setRange(0, 100_000)
        self.station_fee_input.setDecimals(2)
        self.station_fee_input.setSingleStep(50)
        self.station_fee_input.setToolTip(
            "Enter the exact number Albion displays. Enter 500 when the station shows 500."
        )
        entry.addWidget(self.station_fee_input)
        self.station_fee_save_button = QPushButton("Save fee && recalculate")
        self.station_fee_save_button.clicked.connect(self.save_station_fee)
        entry.addWidget(self.station_fee_save_button)
        entry.addStretch(1)
        layout.addLayout(entry)
        self.station_fee_save_status = QLabel()
        self.station_fee_save_status.setWordWrap(True)
        self.station_fee_save_status.setObjectName("muted")
        self.station_fee_save_status.setVisible(False)
        layout.addWidget(self.station_fee_save_status)
        self.station_fee_prompt.setVisible(False)
        root.addWidget(self.station_fee_prompt)

    def _build_refresh_controls(self, root: QVBoxLayout) -> None:
        self.whats_missing = QLabel("WHAT'S MISSING · choose a recipe first")
        self.whats_missing.setWordWrap(True)
        self.whats_missing.setObjectName("muted")
        root.addWidget(self.whats_missing)
        row = QHBoxLayout()
        # Escape the mnemonic marker so the ampersand is visible in the button text.
        self.refresh_button = QPushButton("FIX / REFRESH MISSING DATA")
        self.refresh_button.setEnabled(False)
        self.refresh_button.setToolTip(
            "Explicitly download only this recipe's material-buy and output-sale price keys."
        )
        self.refresh_button.clicked.connect(self.refresh_required_prices)
        row.addWidget(self.refresh_button)
        self.refresh_status = QLabel(
            "No network refresh has run. The totals below use saved prices."
        )
        self.refresh_status.setWordWrap(True)
        self.refresh_status.setObjectName("muted")
        row.addWidget(self.refresh_status, 1)
        root.addLayout(row)
        broad_note = QLabel(
            "This calculator refreshes only the selected recipe. For broad cache coverage, use "
            "Market Data → Refresh Royal Markets."
        )
        broad_note.setWordWrap(True)
        broad_note.setObjectName("muted")
        root.addWidget(broad_note)

    def _build_summary(self, root: QVBoxLayout) -> None:
        summary = QGroupBox("Your totals")
        grid = QGridLayout(summary)
        cards = (
            (
                "purchase",
                "Materials to purchase",
                "Total silver needed to buy all inputs",
            ),
            (
                "crafting_fee",
                "Station usage fee",
                "Silver charged by the production station",
            ),
            (
                "sale_proceeds",
                "Sale proceeds after fees",
                "After marketplace listing and transaction fees",
            ),
            (
                "final_profit",
                "Expected final profit",
                "Includes expected material returns",
            ),
        )
        for index, (key, title, hint) in enumerate(cards):
            card = QFrame()
            card.setObjectName("calculatorMetricCard")
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setStyleSheet(
                "#calculatorMetricCard { border: 1px solid #3b4250; border-radius: 6px; "
                "background: #1d212a; padding: 8px; }"
            )
            card_layout = QVBoxLayout(card)
            title_label = QLabel(title)
            title_label.setStyleSheet("background: transparent; font-weight: 600; color: #cbd1dc;")
            value = QLabel("—")
            value.setObjectName(f"{key}Value")
            value.setStyleSheet(
                "background: transparent; font-size: 23px; font-weight: 700; color: #e5e9f0;"
            )
            helper = QLabel(hint)
            helper.setWordWrap(True)
            helper.setObjectName("muted")
            helper.setStyleSheet("background: transparent; color: #929aaa;")
            card_layout.addWidget(title_label)
            card_layout.addWidget(value)
            card_layout.addWidget(helper)
            grid.addWidget(card, index // 2, index % 2)
            self.summary_labels[key] = value
            self.summary_hints[key] = helper

        self.cash_before_sale = QLabel("Cash needed before sale: —")
        self.cash_before_sale.setWordWrap(True)
        self.cash_before_sale.setObjectName("muted")
        self.reconciliation_note = QLabel(
            "Profit accounts for expected returned materials and marketplace fees."
        )
        self.reconciliation_note.setWordWrap(True)
        self.reconciliation_note.setObjectName("muted")
        grid.addWidget(self.cash_before_sale, 2, 0, 1, 2)
        grid.addWidget(self.reconciliation_note, 3, 0, 1, 2)
        root.addWidget(summary)

    def _build_details(self, root: QVBoxLayout) -> None:
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Show calculation details and data evidence")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setChecked(False)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.toggled.connect(self._toggle_details)
        root.addWidget(self.details_toggle)

        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        assumptions = QGroupBox("Advanced assumptions")
        assumptions_layout = QGridLayout(assumptions)
        self.quality = QSpinBox()
        self.quality.setRange(1, 5)
        self.quality.setToolTip(
            "Only Normal quality is decision-grade. Higher qualities are hypothetical."
        )
        self.quality_evidence = QLabel()
        self.quality_evidence.setWordWrap(True)
        self.quality_evidence.setObjectName("muted")
        assumptions_layout.addWidget(QLabel("Output quality (1–5)"), 0, 0)
        assumptions_layout.addWidget(self.quality, 0, 1)
        assumptions_layout.addWidget(self.quality_evidence, 0, 2, 1, 4)
        details_layout.addWidget(assumptions)

        breakdown = QGroupBox("Full calculation breakdown")
        breakdown_grid = QGridLayout(breakdown)
        fields = (
            ("upfront_material", "Gross material purchase cash"),
            ("station_cash", "Station cash"),
            ("listing_cash", "Sell-order setup cash"),
            ("pre_revenue", "Total cash needed before sale"),
            ("transaction_tax", "Transaction tax at sale"),
            ("material", "Raw material cost"),
            ("returned", "Expected returned material value"),
            ("effective", "Effective material cost"),
            ("station", "Station fee"),
            ("craft_total", "Total production cost"),
            ("economic_cost", "Effective economic cost"),
            ("gross", "Gross sale"),
            ("market_fees", "Marketplace fees"),
            ("net", "Net sale proceeds"),
            ("profit", "Expected profit"),
            ("roi", "ROI"),
            ("margin", "Margin"),
            ("break_even", "Break-even unit price"),
            ("focus_used", "Focus required"),
            ("focus_available", "Focus available"),
            ("focus_shortfall", "Focus shortfall"),
            ("focus_profit", "Additional profit from Focus"),
            ("silver_focus", "Silver / Focus"),
        )
        for index, (key, text) in enumerate(fields):
            row = index % 12
            column = 0 if index < 12 else 2
            breakdown_grid.addWidget(QLabel(text), row, column)
            label = QLabel("—")
            label.setAlignment(Qt.AlignmentFlag.AlignRight)
            breakdown_grid.addWidget(label, row, column + 1)
            self.value_labels[key] = label
        details_layout.addWidget(breakdown)

        evidence = QGroupBox("Recipe and price evidence")
        evidence_layout = QVBoxLayout(evidence)
        self.truth_detail = QLabel("No recipe selected")
        self.truth_detail.setWordWrap(True)
        self.truth_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        evidence_layout.addWidget(self.truth_detail)
        self.recipe_summary = QLabel("No recipe selected")
        self.recipe_summary.setWordWrap(True)
        self.recipe_summary.setObjectName("muted")
        evidence_layout.addWidget(self.recipe_summary)
        self.station_fee_evidence = QLabel("Station fee not resolved yet")
        self.station_fee_evidence.setWordWrap(True)
        self.station_fee_evidence.setObjectName("muted")
        evidence_layout.addWidget(self.station_fee_evidence)
        self.fce_evidence = QLabel("Recipe-specific FCE not resolved yet")
        self.fce_evidence.setWordWrap(True)
        self.fce_evidence.setObjectName("muted")
        evidence_layout.addWidget(self.fce_evidence)
        self.material_table = QTableWidget(0, 10)
        self.material_table.setHorizontalHeaderLabels(
            (
                "Price line",
                "Total needed for selected batches",
                "Returnable",
                "Unit price",
                "Resolved source",
                "Confidence",
                "Provenance",
                "Observed (UTC)",
                "Freshness",
                "7d volume/day",
            )
        )
        self.material_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        evidence_layout.addWidget(self.material_table)
        details_layout.addWidget(evidence)
        self.details.setVisible(False)
        root.addWidget(self.details)

    def load_defaults(self) -> None:
        self._loading_controls = True
        self.material_buy_city.setCurrentText(str(self._setting("default_material_buy_city")))
        self.craft_city.setCurrentText(str(self._setting("default_craft_city")))
        self.sell_city.setCurrentText(str(self._setting("default_sell_city")))
        self.focus.setChecked(bool(self._setting("focus_enabled")))
        self.premium.setChecked(bool(self._setting("premium")))
        self._loading_controls = False
        self._update_quality_evidence()
        self.calculate()

    def _setting(self, key: str):
        return self.settings.get(key, DEFAULT_SETTINGS[key])

    def _search_catalog(self) -> None:
        previous = self.item_results.currentData()
        matches = self.catalog.search_recipes(self.item_search.text(), limit=75)
        self.item_results.blockSignals(True)
        self.item_results.clear()
        for item in matches:
            enchantment = f" .{item.enchantment}" if item.enchantment else ""
            self.item_results.addItem(
                f"{item.display_name}{enchantment} · {item.item_id}", item.item_id
            )
        if previous:
            index = self.item_results.findData(previous)
            if index >= 0:
                self.item_results.setCurrentIndex(index)
        self.item_results.blockSignals(False)
        self.calculate()

    def refresh_catalog(self) -> None:
        self._search_catalog()

    def _economic_control_changed(self, *_args: object) -> None:
        if self._loading_controls:
            return
        self._update_quality_evidence()
        self.calculate()

    def calculate(self, *_args: object) -> None:
        if self._loading_controls:
            return
        item_id = self.item_results.currentData()
        if not item_id:
            self._show_no_recipe()
            return
        recipe = self.catalog.get_recipe(str(item_id))
        if recipe is None:
            self._show_unavailable_recipe()
            return
        self._current_recipe = recipe
        self.clean_catalog_button.setVisible(False)
        self.refresh_button.setEnabled(not self._workers)
        self._update_recipe_quantities(recipe)

        region = Region(str(self._setting("region")))
        market_policy = FreshnessPolicy(timedelta(hours=int(self._setting("max_market_age_hours"))))
        sale_method = SaleMethod(str(self.sale_method.currentData()))
        output_side = self._output_side(sale_method)
        snapshot = self.resolver.resolve(
            recipe,
            buy_city=self.material_buy_city.currentText(),
            sell_city=self.sell_city.currentText(),
            region=region,
            quality=self.quality.value(),
            freshness_policy=market_policy,
            material_side=MarketSide.SELL_ORDER,
            output_side=output_side,
        )

        station_type = station_type_for_item(recipe.output)
        station_observation = self._station_observation(region, station_type)
        station_policy = FreshnessPolicy(
            timedelta(hours=int(self._setting("max_station_fee_age_hours")))
        )
        profile = self._crafting_profile()
        fce_resolution = profile.resolve(focus_skill_mapping_for_recipe(recipe))
        self.station_fee_evidence.setText(
            self._station_evidence_text(region, station_type, station_observation)
        )
        self.fce_evidence.setText(self._fce_evidence_text(fce_resolution))
        self._update_focus_hint(fce_resolution)
        self._update_station_fee_prompt(
            region,
            station_type,
            station_observation,
            station_policy,
        )

        context = CraftingContext(
            craft_city=self.craft_city.currentText(),
            sell_city=self.sell_city.currentText(),
            crafts=self.quantity.value(),
            output_quality=self.quality.value(),
            use_focus=self.focus.isChecked(),
            premium=self.premium.isChecked(),
            sale_method=sale_method,
            profile=profile,
            material_buy_city=self.material_buy_city.currentText(),
            station_fee_observation=station_observation,
            station_fee_freshness_policy=station_policy,
        )
        result = self.calculator.calculate(
            recipe,
            snapshot.material_prices,
            snapshot.output_price,
            context,
            data_quality=snapshot.actionability,
        )
        self._current_result = result
        self._current_snapshot = snapshot
        self._show_result(result)
        self._show_truth(
            recipe,
            snapshot,
            result,
            region=region,
            station_type=station_type,
            station_observation=station_observation,
            fce_resolution=fce_resolution,
        )

    def _show_no_recipe(self) -> None:
        self._current_recipe = None
        self._current_result = None
        self._current_snapshot = None
        metadata = self.catalog.import_metadata()
        clean_catalog = metadata is None
        self.data_banner.setText(
            "No production recipes are installed yet. Install Static Game Data to choose an item."
            if clean_catalog
            else (
                "No producible item matches this search. Try a broader name or canonical Albion ID."
            )
        )
        self._set_banner_state("unknown")
        self.clean_catalog_button.setVisible(clean_catalog)
        self.refresh_button.setEnabled(False)
        self.station_fee_prompt.setVisible(False)
        self.output_count_label.setText("Produces —")
        self.materials_needed_label.setText("Materials to buy: —")
        self.focus_profile_hint.setText("Focus assumptions appear after you choose a recipe.")
        self.whats_missing.setText("WHAT'S MISSING · choose a supported production recipe")
        self.recipe_summary.setText("No recipe selected")
        self.truth_detail.setText("No recipe selected")
        self.station_fee_evidence.setText("Station fee: unknown — no recipe selected")
        self.fce_evidence.setText("Effective FCE: unknown — no recipe selected")
        self.material_table.setRowCount(0)
        self._clear_result()

    def _show_unavailable_recipe(self) -> None:
        self._current_recipe = None
        self._current_result = None
        self._current_snapshot = None
        self.data_banner.setText(
            "The selected recipe is no longer present. Refresh the catalog or choose another item."
        )
        self._set_banner_state("unknown")
        self.clean_catalog_button.setVisible(False)
        self.refresh_button.setEnabled(False)
        self.station_fee_prompt.setVisible(False)
        self.whats_missing.setText("WHAT'S MISSING · the selected recipe is unavailable")
        self.output_count_label.setText("Produces —")
        self.materials_needed_label.setText("Materials to buy: —")
        self.recipe_summary.setText("No recipe selected")
        self.truth_detail.setText("Selected recipe unavailable")
        self.material_table.setRowCount(0)
        self._clear_result()

    def _update_recipe_quantities(self, recipe: Recipe) -> None:
        crafts = self.quantity.value()
        outputs = crafts * recipe.output_quantity
        craft_word = "batch" if crafts == 1 else "batches"
        output_word = "item" if outputs == 1 else "items"
        verb = "produces" if crafts == 1 else "produce"
        self.output_count_label.setText(
            f"{crafts:,} {craft_word} {verb} {outputs:,} {output_word}."
        )
        material_lines = ", ".join(
            f"{requirement.quantity * crafts:g} {requirement.item_id}"
            for requirement in recipe.materials
        )
        self.materials_needed_label.setText(
            f"Gross materials to buy: {material_lines}. Expected returns do not reduce this "
            "purchase quantity."
        )

    def _crafting_profile(self) -> CraftingSkillProfile:
        stored = self.crafting_profiles.load() if self.crafting_profiles is not None else None
        if stored is not None:
            return stored
        return CraftingSkillProfile(available_focus=float(self._setting("available_focus")))

    def _station_observation(
        self, region: Region, station_type: StationType | None
    ) -> StationFeeObservation | None:
        if self.station_fees is None or station_type is None:
            return None
        return self.station_fees.get(region, self.craft_city.currentText(), station_type)

    def _update_station_fee_prompt(
        self,
        region: Region,
        station_type: StationType | None,
        observation: StationFeeObservation | None,
        policy: FreshnessPolicy,
    ) -> None:
        self.station_fee_save_status.clear()
        self.station_fee_save_status.setVisible(False)
        if station_type is None:
            self.station_fee_prompt.setVisible(True)
            self.station_fee_prompt.setTitle("Production station not mapped")
            self.station_fee_prompt_text.setText(
                "This recipe does not have a verified production-station mapping, so a station "
                "fee cannot be keyed safely. Final profit remains unavailable."
            )
            self.station_fee_input.setVisible(False)
            self.station_fee_save_button.setVisible(False)
            return
        if self.station_fees is None:
            self.station_fee_prompt.setVisible(True)
            self.station_fee_prompt.setTitle("Station-fee storage unavailable")
            self.station_fee_prompt_text.setText(
                "No station-fee repository is connected. Final profit remains unavailable; no "
                "zero fee has been assumed."
            )
            self.station_fee_input.setVisible(False)
            self.station_fee_save_button.setVisible(False)
            return

        freshness = policy.classify(observation.observed_at) if observation is not None else None
        needs_entry = observation is None or freshness in {
            Freshness.STALE,
            Freshness.FUTURE,
            Freshness.UNKNOWN,
        }
        self.station_fee_prompt.setVisible(needs_entry)
        if not needs_entry:
            return
        self.station_fee_input.setVisible(True)
        self.station_fee_save_button.setVisible(True)
        context = (
            f"{region.display_name} · {self.craft_city.currentText()} · {station_type.display_name}"
        )
        if observation is None:
            self.station_fee_prompt.setTitle("Station fee needed")
            self.station_fee_prompt_text.setText(
                f"No fee is saved for {context}. Enter the exact number Albion displays; "
                "the calculator will not assume 0."
            )
            self.station_fee_input.setValue(0)
        else:
            self.station_fee_prompt.setTitle("Station fee needs a current observation")
            self.station_fee_prompt_text.setText(
                f"The saved fee for {context} is {freshness.value.lower()}. Re-enter the exact "
                "number currently shown by Albion to make this evidence current."
            )
            self.station_fee_input.setValue(observation.displayed_fee)

    def save_station_fee(self) -> None:
        recipe = self._current_recipe
        station_type = station_type_for_item(recipe.output) if recipe is not None else None
        if recipe is None or station_type is None or self.station_fees is None:
            self.station_fee_save_status.setText(
                "A selected recipe, verified station mapping, and fee storage are required."
            )
            self.station_fee_save_status.setVisible(True)
            return
        region = Region(str(self._setting("region")))
        displayed_fee = self.station_fee_input.value()
        observation = StationFeeObservation(
            region.value,
            self.craft_city.currentText(),
            station_type,
            displayed_fee,
            datetime.now(UTC),
        )
        try:
            self.station_fees.set(observation)
        except Exception as exc:
            self.station_fee_save_status.setText("Station fee was not saved: " + str(exc))
            self.station_fee_save_status.setVisible(True)
            return
        self.station_fee_save_status.setText(
            f"Saved displayed fee {displayed_fee:g} for {self.craft_city.currentText()} "
            f"{station_type.display_name}."
        )
        self.station_fee_save_status.setVisible(True)
        self.station_fee_saved.emit()
        self.calculate()

    def _station_evidence_text(
        self,
        region: Region,
        station_type: StationType | None,
        observation: StationFeeObservation | None,
    ) -> str:
        if station_type is None:
            return "Unknown station mapping — no fee has been assumed."
        if observation is None:
            repository_note = (
                "station-fee storage is not connected"
                if self.station_fees is None
                else "no matching observation is stored"
            )
            return (
                f"{station_type.display_name} · {region.display_name} · "
                f"{self.craft_city.currentText()} · fee unknown ({repository_note})."
            )
        return (
            f"{station_type.display_name} · {observation.region} · {observation.city} · "
            f"displayed fee {observation.displayed_fee:g} · "
            f"observed {observation.observed_at.isoformat()} · {observation.provenance.value}."
        )

    def _fce_evidence_text(self, resolution: FocusEfficiencyResolution) -> str:
        if resolution.is_known:
            mapping_version = (
                f" · mapping {resolution.mapping.source_version}"
                if resolution.mapping is not None
                else ""
            )
            return (
                f"Effective FCE {resolution.focus_cost_efficiency:,.0f} · "
                f"{resolution.source.value} · {resolution.provenance.value}{mapping_version}."
            )
        missing = ", ".join(resolution.missing_skill_keys) or "mapping unavailable"
        repository_note = (
            " Profile storage is not connected." if self.crafting_profiles is None else ""
        )
        return f"Effective FCE unknown · missing {missing}.{repository_note}"

    def _update_focus_hint(self, resolution: FocusEfficiencyResolution) -> None:
        if not self.focus.isChecked():
            self.focus_profile_hint.setText(
                "Focus is off · Destiny Board levels and recipe-specific FCE are not used."
            )
        elif resolution.is_known:
            self.focus_profile_hint.setText(
                f"Focus is on · using recipe-specific effective FCE "
                f"{resolution.focus_cost_efficiency:,.0f}."
            )
        else:
            self.focus_profile_hint.setText(
                "Focus is on, but this recipe's Focus profile is incomplete. Configure its "
                "Destiny Board levels in Settings; the result remains an estimate."
            )

    def _update_quality_evidence(self) -> None:
        if self.quality.value() == 1:
            self.quality_evidence.setText("Normal quality · decision-grade mode.")
        else:
            self.quality_evidence.setText(
                "Hypothetical only · equipment-quality probability and expected value are not "
                "modeled, so this result cannot be actionable."
            )

    def _show_truth(
        self,
        recipe: Recipe,
        snapshot: PricingSnapshot,
        result: CraftResult,
        *,
        region: Region,
        station_type: StationType | None,
        station_observation: StationFeeObservation | None,
        fce_resolution: FocusEfficiencyResolution,
    ) -> None:
        issue_count = len(result.actionability.blocking_reasons)
        oldest = age_text(snapshot.oldest_timestamp)
        estimate_count = snapshot.historical_estimate_count
        timestamp_advisories = {
            ReasonCode.STALE_PRICE,
            ReasonCode.FUTURE_TIMESTAMP,
            ReasonCode.UNKNOWN_TIMESTAMP,
        }
        has_timestamp_advisory = any(
            reason.code in timestamp_advisories for reason in result.actionability.warnings
        )
        if result.actionability.is_actionable and estimate_count:
            self.data_banner.setText(
                f"Estimated profitability · {snapshot.live_price_count} live price(s), "
                f"{estimate_count} historical SELL estimate(s). Oldest evidence: {oldest}. "
                "Open details for confidence and volume."
            )
            self._set_banner_state("aging")
            self.whats_missing.setText(
                "WHAT'S MISSING · no required value is missing; historical estimates are "
                "clearly labeled advisory evidence"
            )
        elif result.actionability.is_actionable and has_timestamp_advisory:
            self.data_banner.setText(
                "Latest available profitability · all required prices are filled. One or more "
                f"current orders have an old or unusual timestamp; they remain usable. Oldest "
                f"evidence: {oldest}."
            )
            self._set_banner_state("aging")
            self.whats_missing.setText(
                "WHAT'S MISSING · nothing; market age is informational and does not block the "
                "calculation"
            )
        elif result.actionability.is_actionable:
            self.data_banner.setText(
                f"Ready to use · all required prices use the latest available AODP evidence. "
                f"Oldest required price: {oldest}."
            )
            self._set_banner_state("fresh")
            self.whats_missing.setText("WHAT'S MISSING · nothing required for this calculation")
        else:
            missing = len(result.missing_price_item_ids)
            reason_codes = {reason.code for reason in result.actionability.reasons}
            if missing:
                message = (
                    f"Price data needed · {missing} required item price"
                    f"{'s are' if missing != 1 else ' is'} missing."
                )
            elif ReasonCode.UNKNOWN_ITEM_VALUE in reason_codes:
                message = (
                    "Unsupported static recipe · the pinned upstream data does not provide "
                    "enough verified Item Value to calculate this station cost."
                )
            elif station_observation is None:
                message = "Station fee needed · add the exact displayed fee to finish profit."
            else:
                message = (
                    f"Estimate only · {issue_count} data or modeling issue"
                    f"{'s need' if issue_count != 1 else ' needs'} attention."
                )
            self.data_banner.setText(
                message + f" Oldest required price: {oldest}. Open details for evidence."
            )
            self._set_banner_state("stale")
            missing_lines: list[str] = []
            if missing:
                missing_lines.append(f"{missing:,} automatic market price(s)")
            if station_observation is None:
                missing_lines.append("the current displayed station fee")
            if ReasonCode.UNKNOWN_ITEM_VALUE in reason_codes:
                missing_lines.append(
                    f"verified static Item Value for {recipe.output.item_id} (not user-enterable)"
                )
            self.whats_missing.setText(
                "WHAT'S MISSING · "
                + (", ".join(missing_lines) if missing_lines else "review the remaining evidence")
            )

        metadata = self.catalog.import_metadata()
        source_version = recipe.source_version or "unknown"
        import_text = (
            f" · imported {metadata.imported_at.isoformat()}" if metadata is not None else ""
        )
        reasons = "\n".join(f"• {reason.message}" for reason in result.actionability.reasons)
        station_text = self._station_evidence_text(region, station_type, station_observation)
        fce_text = self._fce_evidence_text(fce_resolution)
        self.truth_detail.setText(
            f"{result.actionability.status.value} · ruleset {result.ruleset_id} · "
            f"static source {source_version}{import_text}\n"
            f"Oldest relevant price: {oldest} · freshness: {snapshot.freshness.value} · "
            f"{snapshot.live_price_count} live / {estimate_count} historical estimate(s)\n"
            f"Station: {station_text}\nFCE: {fce_text}" + (f"\n{reasons}" if reasons else "")
        )
        self.recipe_summary.setText(
            f"{recipe.output.display_name} · {recipe.output.item_id} · "
            f"{recipe.output_quantity} output/batch · buy materials in "
            f"{self.material_buy_city.currentText()} · produce in "
            f"{self.craft_city.currentText()} · "
            f"sell in {self.sell_city.currentText()} · recipe provenance "
            f"{recipe.provenance.value}"
        )
        self._show_price_table(recipe, snapshot)

    def _show_price_table(self, recipe: Recipe, snapshot: PricingSnapshot) -> None:
        material_lines = [line for line in snapshot.resolved_prices if line.role == "material"]
        self.material_table.setRowCount(len(recipe.materials) + 1)
        for row, requirement in enumerate(recipe.materials):
            line = next(
                (price for price in material_lines if price.item_id == requirement.item_id), None
            )
            values = (
                requirement.item_id,
                f"{requirement.quantity * self.quantity.value():g}",
                (
                    "Unknown"
                    if requirement.returnable is None
                    else "Yes"
                    if requirement.returnable
                    else "No"
                ),
                money(line.price if line else None),
                line.source.value if line else "MISSING",
                line.confidence.value if line else "MISSING",
                line.provenance.value if line else "unknown",
                (
                    line.observation_timestamp.isoformat()
                    if line and line.observation_timestamp
                    else "Unknown"
                ),
                line.freshness.value if line else "Unknown",
                (
                    f"{line.historical_avg_daily_volume_7d:,.1f}"
                    if line and line.historical_avg_daily_volume_7d is not None
                    else "—"
                ),
            )
            for column, value in enumerate(values):
                self.material_table.setItem(row, column, QTableWidgetItem(value))
        output = next((price for price in snapshot.resolved_prices if price.role == "output"), None)
        output_values = (
            f"OUTPUT: {recipe.output.item_id}",
            str(recipe.output_quantity * self.quantity.value()),
            "N/A",
            money(output.price if output else None),
            output.source.value if output else "MISSING",
            output.confidence.value if output else "MISSING",
            output.provenance.value if output else "unknown",
            (
                output.observation_timestamp.isoformat()
                if output and output.observation_timestamp
                else "Unknown"
            ),
            output.freshness.value if output else "Unknown",
            (
                f"{output.historical_avg_daily_volume_7d:,.1f}"
                if output and output.historical_avg_daily_volume_7d is not None
                else "—"
            ),
        )
        for column, value in enumerate(output_values):
            self.material_table.setItem(len(recipe.materials), column, QTableWidgetItem(value))
        self.material_table.resizeColumnsToContents()

    def _show_result(self, result: CraftResult) -> None:
        values = {
            "upfront_material": money(result.gross_material_purchase_cash),
            "station_cash": money(result.station_cash),
            "listing_cash": money(result.listing_setup_cash),
            "pre_revenue": money(result.total_pre_revenue_cash_required),
            "transaction_tax": money(result.transaction_tax),
            "material": money(result.raw_material_cost),
            "returned": money(result.expected_returned_material_value),
            "effective": money(result.effective_material_cost),
            "station": money(result.station_fee),
            "craft_total": money(result.total_craft_cost),
            "economic_cost": money(result.effective_economic_cost),
            "gross": money(result.gross_sale_value),
            "market_fees": money(result.market_fees),
            "net": money(result.net_sale_value),
            "profit": money(result.profit),
            "roi": percent(result.roi),
            "margin": percent(result.margin),
            "break_even": money(result.break_even_price),
            "focus_used": money(result.focus_used),
            "focus_available": money(result.focus_available),
            "focus_shortfall": money(result.focus_shortfall),
            "focus_profit": money(result.incremental_focus_profit),
            "silver_focus": money(result.silver_per_focus),
        }
        for key, display in values.items():
            self.value_labels[key].setText(display)

        reason_codes = {reason.code for reason in result.actionability.reasons}
        self.summary_labels["purchase"].setText(
            self._summary_money(result.gross_material_purchase_cash, "Needs material prices")
        )
        self.summary_labels["crafting_fee"].setText(
            self._summary_money(
                result.station_cash,
                "Unsupported static Item Value"
                if ReasonCode.UNKNOWN_ITEM_VALUE in reason_codes
                else "Needs station fee",
            )
        )
        self.summary_labels["sale_proceeds"].setText(
            self._summary_money(result.net_sale_value, "Needs sale price")
        )
        if result.profit is not None:
            profit_text = self._summary_money(result.profit, "Needs calculation data")
        elif ReasonCode.UNKNOWN_ITEM_VALUE in reason_codes:
            profit_text = "Unsupported static recipe"
        elif ReasonCode.UNKNOWN_STATION_FEE in reason_codes:
            profit_text = "Add station fee to finish"
        elif result.gross_material_purchase_cash is None:
            profit_text = "Needs material prices"
        elif result.net_sale_value is None:
            profit_text = "Needs sale price"
        else:
            profit_text = "Needs calculation data"
        self.summary_labels["final_profit"].setText(profit_text)
        if result.profit is not None and ReasonCode.HISTORICAL_PRICE_ESTIMATE in reason_codes:
            profit_hint = "ESTIMATED — uses clearly labeled AODP historical SELL evidence"
        elif result.profit is not None and result.actionability.is_actionable:
            profit_hint = "Includes expected material returns"
        elif result.profit is not None:
            profit_hint = "Estimate only — review data status"
        elif ReasonCode.UNKNOWN_ITEM_VALUE in reason_codes:
            profit_hint = (
                "Pinned upstream data does not provide enough verified Item Value; this is not "
                "manual setup"
            )
        elif ReasonCode.UNKNOWN_STATION_FEE in reason_codes:
            profit_hint = "Enter the exact displayed station fee above"
        else:
            profit_hint = "Requires complete material and sale price data"
        self.summary_hints["final_profit"].setText(profit_hint)
        self.cash_before_sale.setText(
            "Cash needed before sale: "
            + (
                f"{money(result.total_pre_revenue_cash_required)} silver"
                if result.total_pre_revenue_cash_required is not None
                else "Needs remaining cost data"
            )
        )
        if result.expected_returned_material_value is not None:
            self.reconciliation_note.setText(
                f"Expected final profit includes {money(result.expected_returned_material_value)} "
                "silver of expected returned materials and deducts marketplace fees."
            )
        else:
            self.reconciliation_note.setText(
                "Profit accounts for expected returned materials and marketplace fees."
            )

        profit = result.profit
        if profit is None or not result.actionability.is_actionable:
            profit_color = "#ffb454"
        else:
            profit_color = "#54d68c" if profit >= 0 else "#ff6b6b"
        profit_style = (
            f"background: transparent; font-size: 23px; font-weight: 700; color: {profit_color};"
        )
        self.summary_labels["final_profit"].setStyleSheet(profit_style)
        self.value_labels["profit"].setStyleSheet(f"color: {profit_color}; font-weight: 700;")

    @staticmethod
    def _summary_money(value: float | None, missing_text: str) -> str:
        return f"{money(value)} silver" if value is not None else missing_text

    def _clear_result(self) -> None:
        for label in self.value_labels.values():
            label.setText("—")
        for label in self.summary_labels.values():
            label.setText("—")
        self.summary_labels["final_profit"].setStyleSheet(
            "background: transparent; font-size: 23px; font-weight: 700; color: #e5e9f0;"
        )
        self.value_labels["profit"].setStyleSheet("")
        self.cash_before_sale.setText("Cash needed before sale: —")
        self.reconciliation_note.setText(
            "Profit accounts for expected returned materials and marketplace fees."
        )

    def refresh_required_prices(self) -> None:
        recipe = self._current_recipe
        if recipe is None or self._workers:
            return
        region = Region(str(self._setting("region")))
        sale_method = SaleMethod(str(self.sale_method.currentData()))
        request = RecipePriceRefreshRequest(
            recipe,
            region,
            self.material_buy_city.currentText(),
            self.sell_city.currentText(),
            self.quality.value(),
            material_side=MarketSide.SELL_ORDER,
            output_side=self._output_side(sale_method),
            maximum_price_age=timedelta(hours=int(self._setting("max_market_age_hours"))),
        )
        try:
            service = self._service_for_region(region)
        except Exception as exc:
            self.refresh_status.setText("Price refresh could not start: " + str(exc))
            return
        worker = CalculatorRefreshWorker(service, request)
        self._workers.add(worker)
        self.search_timer.stop()
        self._set_refresh_running(True)
        required = len(recipe.materials) + 1
        self.refresh_status.setText(
            f"Refreshing {required:,} required price lines in sparse background requests…"
        )
        worker.signals.progress.connect(self._refresh_progress)
        worker.signals.finished.connect(
            lambda result, current=worker: self._refresh_finished(current, result)
        )
        worker.signals.error.connect(
            lambda message, current=worker: self._refresh_failed(current, message)
        )
        self.thread_pool.start(worker)

    def _service_for_region(self, region: Region) -> RecipePriceRefreshService:
        if self._refresh_service is not None:
            return self._refresh_service
        service = self._region_refresh_services.get(region)
        if service is None:
            service = (
                self._refresh_service_factory(region)
                if self._refresh_service_factory is not None
                else RecipePriceRefreshService(
                    self.resolver.repository,
                    history_repository=self.resolver.history,
                )
            )
            self._region_refresh_services[region] = service
        return service

    def _refresh_progress(self, progress: RecipePriceRefreshProgress) -> None:
        if self._closing:
            return
        self.refresh_status.setText(
            f"Refreshing required prices… {progress.batches_completed:,}/"
            f"{progress.batches_planned:,} request batches complete; "
            f"{progress.records_loaded:,} rows loaded."
        )

    def _refresh_finished(
        self,
        worker: CalculatorRefreshWorker,
        result: RecipePriceRefreshResult,
    ) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._set_refresh_running(False)
        self.calculate()
        updated = sum(
            value.status is RecipePriceAvailabilityStatus.UPDATED for value in result.availability
        )
        retained = sum(
            value.status is RecipePriceAvailabilityStatus.RETAINED for value in result.availability
        )
        available = result.selected_sides_available
        requested = result.requirements_requested
        if not self._refresh_request_matches_current(worker.request):
            text = (
                f"Refresh finished for the previous selection: {available:,}/{requested:,} "
                "required prices are available. Successful rows were saved, but the visible "
                "scenario changed while the request was running. Current totals were "
                "recalculated from cache; refresh again for this selection."
            )
        elif result.cancelled:
            text = (
                f"Refresh cancelled: {available:,}/{requested:,} required prices are available. "
                "Successful rows already saved were retained; totals were recalculated."
            )
        elif result.is_complete and getattr(result, "historical_estimates_available", 0):
            text = (
                f"Refresh complete with estimates: {available:,}/{requested:,} required prices "
                f"available; {result.historical_estimates_available:,} resolved from AODP daily "
                "SELL history. Totals recalculated as ESTIMATED."
            )
        elif result.is_complete:
            text = (
                f"Refresh complete: {available:,}/{requested:,} required prices available; "
                f"{updated:,} updated and {retained:,} retained. Totals recalculated."
            )
        elif result.is_partial:
            if result.batches_failed == 0 and not result.record_failures:
                unavailable = self._unavailable_refresh_requirements(result)
                text = (
                    f"AODP check succeeded: {available:,}/{requested:,} required prices are "
                    f"available; {result.selected_sides_missing:,} still unavailable because "
                    "no usable order was reported"
                    + (f" for {unavailable}" if unavailable else "")
                    + ". Refreshing again cannot create a market order; change the selected "
                    "city/item or enter a current observed price in Market Data."
                )
            else:
                text = (
                    f"Refresh partial: {available:,}/{requested:,} required prices available; "
                    f"{result.selected_sides_missing:,} still missing; "
                    f"{result.batches_failed:,} request batches failed. Successful rows were "
                    "saved and prior cache rows were preserved."
                )
        else:
            text = (
                f"Refresh did not complete: {available:,}/{requested:,} required prices are "
                f"available; {result.batches_failed:,} request batches failed. Existing cache "
                "was preserved and totals were recalculated from it."
            )
        if result.record_failures:
            text += f" {result.record_failures:,} malformed response rows were skipped."
        self.refresh_status.setText(text)
        if result.batches_succeeded:
            self.prices_refreshed.emit()

    @staticmethod
    def _unavailable_refresh_requirements(result: RecipePriceRefreshResult) -> str:
        values: list[str] = []
        for availability in result.availability:
            if availability.status is not RecipePriceAvailabilityStatus.MISSING:
                continue
            requirement = getattr(availability, "requirement", None)
            if requirement is None:
                continue
            values.append(
                f"{requirement.item_id} {requirement.side.value.replace('_', ' ')} "
                f"in {requirement.city}"
            )
        return ", ".join(values)

    def _refresh_request_matches_current(self, request: RecipePriceRefreshRequest) -> bool:
        recipe = self._current_recipe
        if recipe is None:
            return False
        sale_method = SaleMethod(str(self.sale_method.currentData()))
        return (
            recipe == request.recipe
            and Region(str(self._setting("region"))) is request.region
            and self.material_buy_city.currentText().casefold() == request.material_city.casefold()
            and self.sell_city.currentText().casefold() == request.sell_city.casefold()
            and self.quality.value() == request.output_quality
            and request.material_side is MarketSide.SELL_ORDER
            and self._output_side(sale_method) is request.output_side
        )

    def _refresh_failed(self, worker: CalculatorRefreshWorker, message: str) -> None:
        self._workers.discard(worker)
        if self._closing:
            return
        self._set_refresh_running(False)
        self.calculate()
        self.refresh_status.setText(
            "Live price refresh stopped unexpectedly. Successful earlier batches may already "
            "be saved; all other saved prices were preserved. Totals were recalculated from "
            "the current cache. Error: " + message
        )

    def _set_refresh_running(self, running: bool) -> None:
        for control in (
            self.item_search,
            self.item_results,
            self.quantity,
            self.material_buy_city,
            self.craft_city,
            self.sell_city,
            self.sale_method,
            self.focus,
            self.premium,
            self.quality,
            self.station_fee_input,
            self.station_fee_save_button,
        ):
            control.setEnabled(not running)
        self.refresh_button.setEnabled(not running and self._current_recipe is not None)

    def shutdown(self) -> None:
        """Cancel workers and detach callbacks before this view or its window is destroyed."""

        if self._closing:
            return
        self._closing = True
        for worker in tuple(self._workers):
            worker.cancel()
            for signal in (
                worker.signals.progress,
                worker.signals.finished,
                worker.signals.error,
            ):
                try:
                    signal.disconnect()
                except RuntimeError:
                    pass
        self._workers.clear()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)

    @staticmethod
    def _output_side(sale_method: SaleMethod) -> MarketSide:
        return (
            MarketSide.SELL_ORDER if sale_method is SaleMethod.SELL_ORDER else MarketSide.BUY_ORDER
        )

    def _toggle_details(self, checked: bool) -> None:
        self.details.setVisible(checked)
        self.details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.details_toggle.setText(
            "Hide calculation details and data evidence"
            if checked
            else "Show calculation details and data evidence"
        )

    def _set_banner_state(self, state: str) -> None:
        self.data_banner.setProperty("freshness", state)
        self.data_banner.style().unpolish(self.data_banner)
        self.data_banner.style().polish(self.data_banner)
