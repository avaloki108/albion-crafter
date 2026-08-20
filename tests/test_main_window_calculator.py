from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
)
from albion_crafter.market.models import Region
from albion_crafter.market.recipe_refresh import RecipePriceRefreshService
from albion_crafter.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window_opens_simple_calculator_and_preserves_scenario_on_evidence_edits(
    qt_app,
    tmp_path,
) -> None:
    database = Database(tmp_path / "main-window.db")
    database.initialize()
    market = MarketPriceRepository(database)
    window = MainWindow(
        market,
        PriceOverrideRepository(database),
        CatalogRepository(database),
        SettingsRepository(database),
    )

    assert window.navigation.currentRow() == 0
    assert window.pages.currentWidget() is window.find_money
    window.navigation.setCurrentRow(1)
    assert window.pages.currentWidget() is window.calculator
    assert isinstance(window.calculator._refresh_service, RecipePriceRefreshService)

    window.calculator.material_buy_city.setCurrentText("Martlock")
    window.calculator.craft_city.setCurrentText("Lymhurst")
    window.calculator.sell_city.setCurrentText("Thetford")
    window.calculator.quantity.setValue(7)
    window.calculator.focus.setChecked(True)
    window.calculator.premium.setChecked(False)

    window.settings.station_city.setCurrentText("Fort Sterling")
    window.settings.station_displayed_fee.setValue(777)
    window.settings._save_station_fee()
    window.settings.crafting_profile_changed.emit()

    assert window.calculator.material_buy_city.currentText() == "Martlock"
    assert window.calculator.craft_city.currentText() == "Lymhurst"
    assert window.calculator.sell_city.currentText() == "Thetford"
    assert window.calculator.quantity.value() == 7
    assert window.calculator.focus.isChecked()
    assert not window.calculator.premium.isChecked()

    assert window.settings.station_fees is not None
    window.settings.station_fees.set(
        StationFeeObservation(
            Region.AMERICAS.value,
            "Martlock",
            StationType.WARRIORS_FORGE,
            888,
            datetime.now(UTC),
        )
    )
    before = window.settings.station_fee_table.rowCount()
    window.calculator.station_fee_saved.emit()
    assert window.settings.station_fee_table.rowCount() == before + 1

    shutdown_calls = []
    original_shutdown = window.calculator.shutdown

    def record_shutdown() -> None:
        shutdown_calls.append(True)
        original_shutdown()

    window.calculator.shutdown = record_shutdown  # type: ignore[method-assign]
    window.close()
    assert shutdown_calls == [True]
