from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import Database, SettingsRepository
from albion_crafter.market.models import Region
from albion_crafter.opportunity.models import OpportunitySort, ScanConstraints, ScanSnapshot
from albion_crafter.ui.craft_scanner import CraftScannerView


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_zero_actionable_scan_explains_scope_and_offers_reason_inspection(
    qt_app,
    tmp_path,
) -> None:
    database = Database(tmp_path / "scanner-ui.db")
    database.initialize()
    view = CraftScannerView(
        object(),  # type: ignore[arg-type]
        SettingsRepository(database),
        CatalogRepository(database),
    )
    snapshot = ScanSnapshot(
        scan_time=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ruleset_id="scanner-ui-test",
        constraints=ScanConstraints(
            region=Region.AMERICAS,
            craft_cities=("Bridgewatch",),
            sell_cities=("Bridgewatch",),
            actionable_only=True,
            sort_by=OpportunitySort.PROFIT,
        ),
        recipes_considered=27,
        scenarios_evaluated=81,
        actionable_count=0,
        rejected_count=81,
        opportunities=(),
        database_load_operations=8,
        market_rows_loaded=12,
        override_rows_loaded=0,
        elapsed_seconds=0.1,
        rejection_class_counts=(
            ("market_data", 31),
            ("unprofitable", 28),
            ("unsupported_static", 22),
        ),
    )

    view._render(snapshot)

    assert not view.zero_results.isHidden()
    assert "0 actionable results" in view.zero_results.text()
    assert "81 scenarios checked" in view.zero_results.text()
    assert "31 missing or stale market prices" in view.zero_results.text()
    assert "28 unprofitable" in view.zero_results.text()
    assert "22 unsupported static data" in view.zero_results.text()
    assert "individual reasons" in view.zero_results.text()
    assert not view.show_nonactionable_button.isHidden()
    assert not view.market_sync_button.isHidden()
    sync_requests: list[bool] = []
    view.market_sync_requested.connect(lambda: sync_requests.append(True))
    view.market_sync_button.click()
    assert sync_requests == [True]

    rescans: list[bool] = []
    view.start_scan = lambda: rescans.append(True)  # type: ignore[method-assign]
    view.show_nonactionable_button.click()
    qt_app.processEvents()
    assert not view.actionable_only.isChecked()
    assert rescans == [True]
    view.close()
