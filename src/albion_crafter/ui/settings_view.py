from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
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
    QVBoxLayout,
    QWidget,
)

from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillMapping,
    CraftingSkillProfile,
    ManualFocusEfficiencyOverride,
    focus_skill_mapping_for_recipe,
    refining_skill_mapping_for_item,
)
from albion_crafter.core.freshness import FreshnessPolicy
from albion_crafter.core.models import Item
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.data.cities import CITIES
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import SettingsRepository
from albion_crafter.database.v3 import CraftingProfileRepository, StationFeeRepository
from albion_crafter.market.models import Region

DEFAULT_SETTINGS: dict[str, object] = {
    "region": Region.AMERICAS.value,
    "premium": True,
    "default_craft_city": "Bridgewatch",
    "default_material_buy_city": "Bridgewatch",
    "default_sell_city": "Bridgewatch",
    "max_market_age_hours": 4,
    "max_station_fee_age_hours": 24,
    # A missing crafting profile makes a focused result non-actionable.  Keep
    # new installs on the complete non-Focus path until the user opts in.
    "focus_enabled": False,
    "available_focus": 10_000,
}


class SettingsView(QWidget):
    settings_saved = Signal()
    station_fees_changed = Signal()
    crafting_profile_changed = Signal()

    def __init__(
        self,
        repository: SettingsRepository,
        station_fees: StationFeeRepository | None = None,
        crafting_profiles: CraftingProfileRepository | None = None,
        catalog: CatalogRepository | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.station_fees = station_fees
        self.crafting_profiles = crafting_profiles
        self.catalog = catalog
        self._profile = CraftingSkillProfile()
        self._selected_mapping: CraftingSkillMapping | None = None

        root = QVBoxLayout(self)
        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)

        defaults = QGroupBox("Defaults")
        form = QFormLayout(defaults)
        self.region = QComboBox()
        for region in Region:
            self.region.addItem(region.display_name, region.value)
        self.premium = QCheckBox("Premium enabled")
        self.craft_city = QComboBox()
        self.craft_city.addItems(CITIES)
        self.material_buy_city = QComboBox()
        self.material_buy_city.addItems(CITIES)
        self.sell_city = QComboBox()
        self.sell_city.addItems(CITIES)
        self.max_age = QSpinBox()
        self.max_age.setRange(1, 168)
        self.max_age.setSuffix(" hours")
        self.max_station_fee_age = QSpinBox()
        self.max_station_fee_age.setRange(1, 720)
        self.max_station_fee_age.setSuffix(" hours")
        self.focus = QCheckBox("Use Focus by default")
        self.available_focus = QSpinBox()
        self.available_focus.setRange(0, 30_000)
        self.available_focus.setSingleStep(500)
        form.addRow("Albion server", self.region)
        form.addRow("Account", self.premium)
        form.addRow("Default material-buy city", self.material_buy_city)
        form.addRow("Default production city", self.craft_city)
        form.addRow("Default selling city", self.sell_city)
        form.addRow("Maximum market-data age", self.max_age)
        form.addRow("Maximum station-fee age", self.max_station_fee_age)
        form.addRow("Focus", self.focus)
        form.addRow("Available Focus", self.available_focus)
        layout.addWidget(defaults)

        self._build_station_fee_group(layout)
        self._build_refining_matrix_group(layout)
        self._build_profile_group(layout)

        note = QLabel(
            "Station fees are the exact numbers displayed by Albion and are keyed by server, "
            "city, and station. Focus Cost Efficiency is recipe-specific; missing "
            "Destiny Board levels are not silently treated as zero."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        save = QPushButton("Save defaults and profile options")
        save.clicked.connect(self.save)
        layout.addWidget(save)
        layout.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll)

        self.profile_search_timer = QTimer(self)
        self.profile_search_timer.setSingleShot(True)
        self.profile_search_timer.setInterval(250)
        self.profile_search_timer.timeout.connect(self._search_profile_catalog)
        self.profile_item_search.textChanged.connect(lambda: self.profile_search_timer.start())
        self.profile_item_results.currentIndexChanged.connect(self._profile_item_changed)
        self.region.currentIndexChanged.connect(self._refresh_station_fee_table)
        self.station_fee_table.itemSelectionChanged.connect(self._station_fee_selected)

        self.load()
        self._search_profile_catalog()

    def _build_station_fee_group(self, layout: QVBoxLayout) -> None:
        group = QGroupBox("Observed station fees")
        group_layout = QVBoxLayout(group)
        form = QFormLayout()
        self.station_city = QComboBox()
        self.station_city.addItems(CITIES)
        self.station_type = QComboBox()
        for station_type in StationType:
            self.station_type.addItem(station_type.display_name, station_type)
        self.station_displayed_fee = QDoubleSpinBox()
        self.station_displayed_fee.setRange(0, 100_000)
        self.station_displayed_fee.setDecimals(2)
        self.station_displayed_fee.setSingleStep(50)
        self.station_displayed_fee.setToolTip(
            "Enter the exact number displayed by Albion. For example, enter 500 when Albion "
            "shows 500."
        )
        form.addRow("City", self.station_city)
        form.addRow("Station", self.station_type)
        form.addRow("Displayed usage fee", self.station_displayed_fee)
        group_layout.addLayout(form)

        buttons = QHBoxLayout()
        self.save_station_fee_button = QPushButton("Save observed fee")
        self.save_station_fee_button.clicked.connect(self._save_station_fee)
        self.remove_station_fee_button = QPushButton("Remove selected fee")
        self.remove_station_fee_button.clicked.connect(self._remove_station_fee)
        buttons.addWidget(self.save_station_fee_button)
        buttons.addWidget(self.remove_station_fee_button)
        buttons.addStretch()
        group_layout.addLayout(buttons)

        self.station_fee_status = QLabel()
        self.station_fee_status.setWordWrap(True)
        self.station_fee_status.setObjectName("muted")
        group_layout.addWidget(self.station_fee_status)
        self.station_fee_table = QTableWidget(0, 7)
        self.station_fee_table.setHorizontalHeaderLabels(
            (
                "Server",
                "City",
                "Station",
                "Displayed fee",
                "Observed (UTC)",
                "Freshness",
                "Provenance",
            )
        )
        self.station_fee_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.station_fee_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.station_fee_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.station_fee_table.setMaximumHeight(180)
        group_layout.addWidget(self.station_fee_table)

        enabled = self.station_fees is not None
        self.save_station_fee_button.setEnabled(enabled)
        self.remove_station_fee_button.setEnabled(enabled)
        if not enabled:
            self.station_fee_status.setText(
                "Station-fee storage is not connected. Calculations will report station fees "
                "as unknown."
            )
        layout.addWidget(group)

    def _build_profile_group(self, layout: QVBoxLayout) -> None:
        group = QGroupBox("Item-specific crafting and refining Focus profile")
        group_layout = QVBoxLayout(group)
        form = QFormLayout()
        self.profile_item_search = QLineEdit()
        self.profile_item_search.setPlaceholderText(
            "Search craftable item or T4-T8 refined resource…"
        )
        self.profile_item_results = QComboBox()
        self.profile_item_results.setMinimumContentsLength(32)
        self.profile_mapping = QLabel("No item selected")
        self.profile_mapping.setWordWrap(True)
        self.mastery_level = QSpinBox()
        self.mastery_level.setRange(0, 100)
        self.mastery_level.setToolTip(
            "For verified mappings this editor uses the versioned mastery coefficient shown "
            "in Mapping evidence."
        )
        self.specialization_level = QSpinBox()
        self.specialization_level.setRange(0, 100)
        self.specialization_level.setToolTip(
            "For verified mappings this editor uses the versioned mutual and item-specific "
            "coefficients shown in Mapping evidence."
        )
        self.assume_zero = QCheckBox("Explicitly treat every unspecified level as zero")
        self.assume_zero.setToolTip(
            "Off by default. When off, an incomplete crafting group has unknown effective FCE."
        )
        self.manual_fce_enabled = QCheckBox(
            "Use manual effective-FCE override for this item family"
        )
        self.manual_fce = QDoubleSpinBox()
        self.manual_fce.setRange(0, 1_000_000)
        self.manual_fce.setDecimals(0)
        self.manual_fce.setSingleStep(1_000)
        self.manual_fce.setToolTip(
            "A user-supplied effective FCE for the selected item family. This is stored "
            "separately from Destiny Board levels and takes precedence."
        )
        self.manual_fce.setEnabled(False)
        self.complete_profile_group = QCheckBox(
            "Mark the selected skill group complete (omitted nodes are explicit zero)"
        )
        self.manual_fce_enabled.toggled.connect(self._manual_mode_changed)
        self.assume_zero.toggled.connect(self._update_profile_resolution)
        self.complete_profile_group.toggled.connect(self._update_profile_resolution)
        self.mastery_level.valueChanged.connect(self._update_profile_resolution)
        self.specialization_level.valueChanged.connect(self._update_profile_resolution)
        self.manual_fce.valueChanged.connect(self._update_profile_resolution)
        self.available_focus.valueChanged.connect(self._update_profile_resolution)
        form.addRow("Find item", self.profile_item_search)
        form.addRow("Matching producible item", self.profile_item_results)
        form.addRow("Mapping evidence", self.profile_mapping)
        form.addRow("Crafting mastery level (if applicable)", self.mastery_level)
        form.addRow("Item / refining-tier specialization", self.specialization_level)
        form.addRow("Incomplete groups", self.assume_zero)
        form.addRow("Selected group", self.complete_profile_group)
        form.addRow("Manual override", self.manual_fce_enabled)
        form.addRow("Manual effective FCE", self.manual_fce)
        group_layout.addLayout(form)

        buttons = QHBoxLayout()
        self.save_profile_item_button = QPushButton("Save selected item profile")
        self.save_profile_item_button.clicked.connect(self._save_profile_item)
        self.clear_profile_item_button = QPushButton("Clear selected item entry")
        self.clear_profile_item_button.setToolTip(
            "Removes this item's specialization and manual override. Shared group mastery stays."
        )
        self.clear_profile_item_button.clicked.connect(self._clear_profile_item)
        buttons.addWidget(self.save_profile_item_button)
        buttons.addWidget(self.clear_profile_item_button)
        buttons.addStretch()
        group_layout.addLayout(buttons)

        self.profile_resolution = QLabel()
        self.profile_resolution.setWordWrap(True)
        self.profile_resolution.setObjectName("muted")
        group_layout.addWidget(self.profile_resolution)
        self.profile_entries = QTableWidget(0, 4)
        self.profile_entries.setHorizontalHeaderLabels(
            ("Stored entry", "Production group", "Level / effective FCE", "Provenance")
        )
        self.profile_entries.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.profile_entries.setMaximumHeight(160)
        group_layout.addWidget(self.profile_entries)

        connected = self.crafting_profiles is not None
        searchable = self.catalog is not None
        self.profile_item_search.setEnabled(searchable)
        self.profile_item_results.setEnabled(searchable)
        self.save_profile_item_button.setEnabled(connected and searchable)
        self.clear_profile_item_button.setEnabled(connected and searchable)
        if not connected:
            self.profile_resolution.setText(
                "Production-profile storage is not connected. Focused calculations will report "
                "recipe-specific FCE as unknown."
            )
        elif not searchable:
            self.profile_resolution.setText(
                "The static catalog is not connected; no mapping can be edited."
            )
        layout.addWidget(group)

    def _build_refining_matrix_group(self, layout: QVBoxLayout) -> None:
        group = QGroupBox("Refining Destiny Board matrix")
        group_layout = QVBoxLayout(group)
        explanation = QLabel(
            "Enter T4-T8 levels for each refining family. Blank means unknown; 0 is an "
            "explicit zero. Mark a family complete only when omitted nodes should be treated "
            "as zero. Manual per-tier FCE overrides remain separate and take precedence."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        group_layout.addWidget(explanation)
        grid = QGridLayout()
        grid.addWidget(QLabel("Family"), 0, 0)
        for column, tier in enumerate(range(4, 9), start=1):
            grid.addWidget(QLabel(f"T{tier} level"), 0, column)
        grid.addWidget(QLabel("Complete"), 0, 6)
        grid.addWidget(QLabel("Effective FCE by tier"), 0, 7)
        self.refining_matrix_inputs: dict[tuple[str, int], QLineEdit] = {}
        self.refining_matrix_complete: dict[str, QCheckBox] = {}
        self.refining_matrix_resolution: dict[str, QLabel] = {}
        families = (
            ("ore", "Ore / Metal Bars"),
            ("wood", "Wood / Planks"),
            ("hide", "Hide / Leather"),
            ("fiber", "Fiber / Cloth"),
            ("rock", "Rock / Stone Blocks"),
        )
        for row, (family, display_name) in enumerate(families, start=1):
            grid.addWidget(QLabel(display_name), row, 0)
            for column, tier in enumerate(range(4, 9), start=1):
                editor = QLineEdit()
                editor.setValidator(QIntValidator(0, 100, editor))
                editor.setPlaceholderText("unknown")
                editor.setMaximumWidth(82)
                editor.textChanged.connect(self._update_refining_matrix_resolution)
                self.refining_matrix_inputs[(family, tier)] = editor
                grid.addWidget(editor, row, column)
            complete = QCheckBox()
            complete.setToolTip(
                "When checked, every blank node in this family is an explicit zero."
            )
            complete.toggled.connect(self._update_refining_matrix_resolution)
            self.refining_matrix_complete[family] = complete
            grid.addWidget(complete, row, 6, alignment=Qt.AlignmentFlag.AlignCenter)
            resolution = QLabel("Unknown")
            resolution.setWordWrap(True)
            self.refining_matrix_resolution[family] = resolution
            grid.addWidget(resolution, row, 7)
        grid.setColumnStretch(7, 1)
        group_layout.addLayout(grid)
        buttons = QHBoxLayout()
        self.save_refining_matrix_button = QPushButton("Save refining matrix")
        self.save_refining_matrix_button.clicked.connect(self._save_refining_matrix)
        buttons.addWidget(self.save_refining_matrix_button)
        buttons.addStretch(1)
        group_layout.addLayout(buttons)
        self.save_refining_matrix_button.setEnabled(self.crafting_profiles is not None)
        layout.addWidget(group)

    def value(self, key: str):
        return self.repository.get(key, DEFAULT_SETTINGS[key])

    def load(self) -> None:
        index = self.region.findData(self.value("region"))
        self.region.setCurrentIndex(max(index, 0))
        self.premium.setChecked(bool(self.value("premium")))
        self.material_buy_city.setCurrentText(str(self.value("default_material_buy_city")))
        self.craft_city.setCurrentText(str(self.value("default_craft_city")))
        self.sell_city.setCurrentText(str(self.value("default_sell_city")))
        self.max_age.setValue(int(self.value("max_market_age_hours")))
        self.max_station_fee_age.setValue(int(self.value("max_station_fee_age_hours")))
        self.focus.setChecked(bool(self.value("focus_enabled")))
        self.available_focus.setValue(int(self.value("available_focus")))
        self._load_profile()
        self._refresh_station_fee_table()

    def save(self) -> None:
        self.repository.set_many(
            {
                "region": self.region.currentData(),
                "premium": self.premium.isChecked(),
                "default_material_buy_city": self.material_buy_city.currentText(),
                "default_craft_city": self.craft_city.currentText(),
                "default_sell_city": self.sell_city.currentText(),
                "max_market_age_hours": self.max_age.value(),
                "max_station_fee_age_hours": self.max_station_fee_age.value(),
                "focus_enabled": self.focus.isChecked(),
                "available_focus": self.available_focus.value(),
            }
        )
        if self.crafting_profiles is not None:
            self._profile = self._profile_from_refining_matrix()
            self.crafting_profiles.save(self._profile)
            self._refresh_profile_entries()
            self._update_profile_resolution()
        self.settings_saved.emit()

    def _save_station_fee(self) -> None:
        if self.station_fees is None:
            return
        station_type = self._current_station_type()
        if station_type is None:
            return
        observation = StationFeeObservation(
            region=str(self.region.currentData()),
            city=self.station_city.currentText(),
            station_type=station_type,
            displayed_fee=self.station_displayed_fee.value(),
            observed_at=datetime.now(UTC),
        )
        self.station_fees.set(observation)
        self.station_fee_status.setText(
            f"Saved {observation.station_type.display_name} in {observation.city}: "
            f"displayed fee {observation.displayed_fee:g} at "
            f"{observation.observed_at.isoformat()} (user override)."
        )
        self._refresh_station_fee_table()
        self.station_fees_changed.emit()

    def _remove_station_fee(self) -> None:
        if self.station_fees is None:
            return
        station_type = self._current_station_type()
        if station_type is None:
            return
        removed = self.station_fees.remove(
            str(self.region.currentData()), self.station_city.currentText(), station_type
        )
        self.station_fee_status.setText(
            "Removed the selected station-fee observation."
            if removed
            else "No station-fee observation matched that server, city, and station."
        )
        self._refresh_station_fee_table()
        if removed:
            self.station_fees_changed.emit()

    def refresh_station_fees(self) -> None:
        """Reload persisted fee observations without changing default controls."""

        self._refresh_station_fee_table()

    def _refresh_station_fee_table(self) -> None:
        observations = (
            self.station_fees.list_all(str(self.region.currentData()))
            if self.station_fees is not None
            else []
        )
        self.station_fee_table.setRowCount(len(observations))
        policy = FreshnessPolicy(timedelta(hours=self.max_station_fee_age.value()))
        now = datetime.now(UTC)
        for row, observation in enumerate(observations):
            values = (
                observation.region,
                observation.city,
                observation.station_type.display_name,
                f"{observation.displayed_fee:g}",
                observation.observed_at.isoformat(),
                policy.classify(observation.observed_at, now=now).value,
                observation.provenance.value,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, observation.station_type.value)
                self.station_fee_table.setItem(row, column, item)
        self.station_fee_table.resizeColumnsToContents()

    def _station_fee_selected(self) -> None:
        row = self.station_fee_table.currentRow()
        if row < 0:
            return
        city = self.station_fee_table.item(row, 1)
        station_cell = self.station_fee_table.item(row, 0)
        fee = self.station_fee_table.item(row, 3)
        if city is None or station_cell is None or fee is None:
            return
        self.station_city.setCurrentText(city.text())
        station_value = station_cell.data(Qt.ItemDataRole.UserRole)
        index = self.station_type.findData(str(station_value))
        if index >= 0:
            self.station_type.setCurrentIndex(index)
        self.station_displayed_fee.setValue(float(fee.text()))

    def _load_profile(self) -> None:
        stored = self.crafting_profiles.load() if self.crafting_profiles is not None else None
        if stored is None:
            self._profile = CraftingSkillProfile(
                available_focus=float(self.available_focus.value())
            )
        else:
            self._profile = stored
            self.available_focus.setValue(round(stored.available_focus))
        self.assume_zero.setChecked(self._profile.assume_zero_for_unspecified)
        self._load_refining_matrix()
        self._refresh_profile_entries()

    def _load_refining_matrix(self) -> None:
        levels = {level.skill_key: level for level in self._profile.skill_levels}
        for (family, tier), editor in self.refining_matrix_inputs.items():
            level = levels.get(f"refining:{family}:t{tier}")
            editor.setText("" if level is None or level.level is None else str(level.level))
        for family, complete in self.refining_matrix_complete.items():
            complete.setChecked(f"refining:{family}" in self._profile.complete_groups)
        self._update_refining_matrix_resolution()

    def _profile_from_refining_matrix(self) -> CraftingSkillProfile:
        skills = [
            level
            for level in self._profile.skill_levels
            if not level.crafting_group.startswith("refining:")
        ]
        for (family, tier), editor in sorted(self.refining_matrix_inputs.items()):
            text = editor.text().strip()
            if not text:
                continue
            level = int(text)
            if not 0 <= level <= 100:
                raise ValueError("refining levels must be between 0 and 100")
            skills.append(
                CraftingSkillLevel(
                    f"refining:{family}:t{tier}",
                    f"refining:{family}",
                    level,
                    30,
                    Provenance.USER_PROFILE,
                )
            )
        complete_groups = {
            group for group in self._profile.complete_groups if not group.startswith("refining:")
        }
        complete_groups.update(
            f"refining:{family}"
            for family, checkbox in self.refining_matrix_complete.items()
            if checkbox.isChecked()
        )
        return CraftingSkillProfile(
            available_focus=float(self.available_focus.value()),
            skill_levels=tuple(skills),
            manual_fce_overrides=self._profile.manual_fce_overrides,
            complete_groups=frozenset(complete_groups),
            assume_zero_for_unspecified=self.assume_zero.isChecked(),
        )

    def _update_refining_matrix_resolution(self, *_args) -> None:
        if not hasattr(self, "assume_zero"):
            return
        preview = self._profile_from_refining_matrix()
        suffixes = {
            "ore": "METALBAR",
            "wood": "PLANKS",
            "hide": "LEATHER",
            "fiber": "CLOTH",
            "rock": "STONEBLOCK",
        }
        for family, label in self.refining_matrix_resolution.items():
            values: list[str] = []
            for tier in range(4, 9):
                item = Item(
                    f"T{tier}_{suffixes[family]}",
                    f"T{tier} {family}",
                    tier,
                    crafting_category=family,
                )
                resolution = preview.resolve(refining_skill_mapping_for_item(item))
                values.append(
                    f"T{tier} "
                    + (
                        f"{resolution.focus_cost_efficiency:,.0f} ({resolution.source.value})"
                        if resolution.is_known
                        else "Unknown"
                    )
                )
            label.setText(" · ".join(values))

    def _save_refining_matrix(self) -> None:
        if self.crafting_profiles is None:
            return
        self._profile = self._profile_from_refining_matrix()
        self.crafting_profiles.save(self._profile)
        self.repository.set_many({"available_focus": self.available_focus.value()})
        self._load_refining_matrix()
        self._refresh_profile_entries()
        self._profile_item_changed()
        self._update_refining_matrix_resolution()
        self.crafting_profile_changed.emit()

    def _search_profile_catalog(self) -> None:
        if self.catalog is None:
            return
        previous = self.profile_item_results.currentData()
        matches = self.catalog.search_recipes(self.profile_item_search.text(), limit=75)
        self.profile_item_results.blockSignals(True)
        self.profile_item_results.clear()
        for item in matches:
            enchantment = f" .{item.enchantment}" if item.enchantment else ""
            self.profile_item_results.addItem(
                f"{item.display_name}{enchantment} · {item.item_id}", item.item_id
            )
        if previous:
            index = self.profile_item_results.findData(previous)
            if index >= 0:
                self.profile_item_results.setCurrentIndex(index)
        self.profile_item_results.blockSignals(False)
        self._profile_item_changed()

    def _profile_item_changed(self) -> None:
        recipe = self._selected_profile_recipe()
        self._selected_mapping = (
            focus_skill_mapping_for_recipe(recipe) if recipe is not None else None
        )
        mapping = self._selected_mapping
        if mapping is None:
            self.profile_mapping.setText(
                "No stable production Focus mapping is available. No FCE is assumed."
            )
            self._set_profile_level_controls(False)
            self.manual_fce_enabled.setChecked(False)
            self.manual_fce_enabled.setEnabled(False)
            self.manual_fce.setValue(0)
            self._update_profile_resolution()
            return

        is_refining = mapping.crafting_group.startswith("refining:")
        self.complete_profile_group.setChecked(
            mapping.crafting_group in self._profile.complete_groups
        )

        if mapping.verified:
            self.profile_mapping.setText(
                f"Verified mapping · {mapping.crafting_group} · "
                f"{mapping.specialization_skill_key} · dataset {mapping.source_version}. "
                f"Mastery {mapping.mastery_fce_per_level:g} FCE/level; specialization "
                f"mutual {mapping.mutual_fce_per_level:g} FCE/level; "
                f"this item adds {mapping.unique_fce_per_level:g} unique FCE/level. Stored "
                f"siblings contribute their verified node coefficient. Verified "
                f"{mapping.verified_on}."
            )
        else:
            self.profile_mapping.setText(
                f"Mapping key {mapping.mapping_key} is stable, but its level coefficients are "
                f"not verified in {mapping.source_version}. Use a manual effective-FCE override."
            )
        self._set_profile_level_controls(mapping.verified)
        self.mastery_level.setEnabled(mapping.verified and not is_refining)
        self.mastery_level.setToolTip(
            "Refining uses the five tier nodes in the selected resource-family chain; "
            "there is no extra mastery row in this editor."
            if is_refining
            else "The verified crafting mastery contribution for this item family."
        )
        self.manual_fce_enabled.setEnabled(True)

        mastery_key = self._mastery_key(mapping)
        mastery = next(
            (value for value in self._profile.skill_levels if value.skill_key == mastery_key), None
        )
        specialization = next(
            (
                value
                for value in self._profile.skill_levels
                if value.skill_key == mapping.specialization_skill_key
            ),
            None,
        )
        override = next(
            (
                value
                for value in self._profile.manual_fce_overrides
                if value.mapping_key == mapping.mapping_key
            ),
            None,
        )
        self.mastery_level.setValue(
            0 if is_refining else (mastery.level or 0 if mastery is not None else 0)
        )
        self.specialization_level.setValue(
            specialization.level or 0 if specialization is not None else 0
        )
        self.manual_fce_enabled.setChecked(override is not None)
        self.manual_fce.setValue(override.focus_cost_efficiency if override is not None else 0)
        self._update_profile_resolution()

    def _set_profile_level_controls(self, enabled: bool) -> None:
        self.mastery_level.setEnabled(enabled)
        self.specialization_level.setEnabled(enabled)

    def _manual_mode_changed(self, enabled: bool) -> None:
        self.manual_fce.setEnabled(enabled)
        mapping = self._selected_mapping
        self._set_profile_level_controls(mapping is not None and mapping.verified and not enabled)
        if mapping is not None and mapping.crafting_group.startswith("refining:"):
            self.mastery_level.setEnabled(False)
        self._update_profile_resolution()

    def _save_profile_item(self) -> None:
        if self.crafting_profiles is None or self._selected_mapping is None:
            return
        self._profile = self._profile_from_editor(override_entered_at=datetime.now(UTC))
        self.crafting_profiles.save(self._profile)
        self.repository.set_many({"available_focus": self.available_focus.value()})
        self._load_refining_matrix()
        self._refresh_profile_entries()
        self._update_profile_resolution()
        self.crafting_profile_changed.emit()

    def _clear_profile_item(self) -> None:
        if self.crafting_profiles is None or self._selected_mapping is None:
            return
        mapping = self._selected_mapping
        self._profile = CraftingSkillProfile(
            available_focus=float(self.available_focus.value()),
            skill_levels=tuple(
                value
                for value in self._profile.skill_levels
                if value.skill_key != mapping.specialization_skill_key
            ),
            manual_fce_overrides=tuple(
                value
                for value in self._profile.manual_fce_overrides
                if value.mapping_key != mapping.mapping_key
            ),
            complete_groups=self._profile.complete_groups,
            assume_zero_for_unspecified=self.assume_zero.isChecked(),
        )
        self.crafting_profiles.save(self._profile)
        self._load_refining_matrix()
        self._profile_item_changed()
        self._refresh_profile_entries()
        self.crafting_profile_changed.emit()

    def _update_profile_resolution(self) -> None:
        if self.crafting_profiles is None:
            return
        preview = self._profile_from_editor()
        resolution = preview.resolve(self._selected_mapping)
        if resolution.is_known:
            self.profile_resolution.setText(
                "Editor preview (save to persist): effective FCE "
                f"{resolution.focus_cost_efficiency:,.0f} · "
                f"{resolution.source.value} · {resolution.provenance.value}. "
                f"Available Focus: {preview.available_focus:,.0f}."
            )
        else:
            missing = ", ".join(resolution.missing_skill_keys) or "mapping unavailable"
            self.profile_resolution.setText(
                f"Editor preview (save to persist): effective FCE unknown · missing {missing}. "
                "Focused results remain non-actionable until this evidence is complete or a "
                "manual override is saved."
            )

    def _profile_from_editor(
        self, *, override_entered_at: datetime | None = None
    ) -> CraftingSkillProfile:
        mapping = self._selected_mapping
        skills = list(self._profile.skill_levels)
        overrides = list(self._profile.manual_fce_overrides)
        if mapping is not None:
            existing_mastery = next(
                (value for value in skills if value.skill_key == self._mastery_key(mapping)),
                None,
            )
            existing_specialization = next(
                (value for value in skills if value.skill_key == mapping.specialization_skill_key),
                None,
            )
            is_refining = mapping.crafting_group.startswith("refining:")
            if mapping.verified and not self.manual_fce_enabled.isChecked():
                skills = [
                    value
                    for value in skills
                    if value.skill_key
                    not in {self._mastery_key(mapping), mapping.specialization_skill_key}
                ]
                edited = [
                    self._edited_skill_level(
                        existing_specialization,
                        skill_key=mapping.specialization_skill_key,
                        crafting_group=mapping.crafting_group,
                        level=self.specialization_level.value(),
                        mutual_fce_per_level=mapping.mutual_fce_per_level,
                    )
                ]
                if not is_refining:
                    edited.insert(
                        0,
                        self._edited_skill_level(
                            existing_mastery,
                            skill_key=self._mastery_key(mapping),
                            crafting_group=mapping.crafting_group,
                            level=self.mastery_level.value(),
                            mutual_fce_per_level=mapping.mastery_fce_per_level,
                        ),
                    )
                skills.extend(edited)
            overrides = [value for value in overrides if value.mapping_key != mapping.mapping_key]
            if self.manual_fce_enabled.isChecked():
                previous_override = next(
                    (
                        value
                        for value in self._profile.manual_fce_overrides
                        if value.mapping_key == mapping.mapping_key
                    ),
                    None,
                )
                overrides.append(
                    ManualFocusEfficiencyOverride(
                        mapping.mapping_key,
                        self.manual_fce.value(),
                        override_entered_at
                        or (
                            previous_override.entered_at
                            if previous_override is not None
                            else datetime.now(UTC)
                        ),
                    )
                )
        complete_groups = set(self._profile.complete_groups)
        if mapping is not None:
            if self.complete_profile_group.isChecked():
                complete_groups.add(mapping.crafting_group)
            else:
                complete_groups.discard(mapping.crafting_group)
        return CraftingSkillProfile(
            available_focus=float(self.available_focus.value()),
            skill_levels=tuple(skills),
            manual_fce_overrides=tuple(overrides),
            complete_groups=frozenset(complete_groups),
            assume_zero_for_unspecified=self.assume_zero.isChecked(),
        )

    @staticmethod
    def _edited_skill_level(
        existing: CraftingSkillLevel | None,
        *,
        skill_key: str,
        crafting_group: str,
        level: int,
        mutual_fce_per_level: float,
    ) -> CraftingSkillLevel:
        return CraftingSkillLevel(
            skill_key,
            crafting_group,
            level,
            (existing.mutual_fce_per_level if existing is not None else mutual_fce_per_level),
            existing.provenance if existing is not None else Provenance.USER_PROFILE,
        )

    def _refresh_profile_entries(self) -> None:
        rows: list[tuple[str, str, str, str]] = []
        rows.extend(
            (
                level.skill_key,
                level.crafting_group,
                "Unknown" if level.level is None else str(level.level),
                level.provenance.value,
            )
            for level in sorted(self._profile.skill_levels, key=lambda value: value.skill_key)
        )
        rows.extend(
            (
                f"Manual override · {override.mapping_key}",
                "item family",
                f"{override.focus_cost_efficiency:g}",
                f"{override.provenance.value} · {override.entered_at.isoformat()}",
            )
            for override in sorted(
                self._profile.manual_fce_overrides, key=lambda value: value.mapping_key
            )
        )
        self.profile_entries.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.profile_entries.setItem(row, column, QTableWidgetItem(value))
        self.profile_entries.resizeColumnsToContents()

    def _selected_profile_recipe(self):
        if self.catalog is None:
            return None
        item_id = self.profile_item_results.currentData()
        return self.catalog.get_recipe(str(item_id)) if item_id else None

    @staticmethod
    def _mastery_key(mapping: CraftingSkillMapping) -> str:
        return f"{mapping.crafting_group}:mastery"

    def _current_station_type(self) -> StationType | None:
        value = self.station_type.currentData()
        try:
            return StationType(str(value))
        except ValueError:
            return None
