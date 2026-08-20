from __future__ import annotations

import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
    default_database_path,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.database.v4 import (
    FindMoneyPreferencesRepository,
    PlanSnapshotRepository,
)
from albion_crafter.market.models import Region
from albion_crafter.planning.service import FindMoneyService
from albion_crafter.ui.main_window import MainWindow, create_find_money_service

DARK_STYLESHEET = """
QWidget { background: #171a21; color: #e5e9f0; font-size: 13px; }
QMainWindow, QStackedWidget { background: #171a21; }
#sidebar { background: #11141a; border-right: 1px solid #303541; }
#brand { color: #f0bd58; font-size: 22px; font-weight: 800; padding: 22px 16px; }
#navigation { background: transparent; border: none; outline: none; }
#navigation::item { padding: 13px 16px; margin: 2px 8px; border-radius: 5px; }
#navigation::item:selected { background: #343b49; color: #f0bd58; }
#pageTitle { font-size: 25px; font-weight: 700; color: #f4f6fa; margin-bottom: 8px; }
#muted { color: #929aaa; }
#sampleBanner, #dataBanner {
  background: #2a2519; color: #f0bd58; border: 1px solid #6c5427;
  border-radius: 5px; padding: 9px;
}
#dataBanner[freshness="fresh"] { background: #173025; color: #75d8a2; border-color: #285f43; }
#dataBanner[freshness="aging"] { background: #352b18; color: #ffcb6b; border-color: #745c28; }
#dataBanner[freshness="stale"], #dataBanner[freshness="unknown"] {
  background: #392020; color: #ff8585; border-color: #743636;
}
QGroupBox {
  border: 1px solid #343b49; border-radius: 6px; margin-top: 12px;
  padding: 14px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
  background: #222630; border: 1px solid #3b4250; border-radius: 4px;
  padding: 6px; min-height: 20px;
}
QPushButton {
  background: #c89432; color: #11141a; border: none; border-radius: 4px;
  padding: 8px 15px; font-weight: 700;
}
QPushButton:hover { background: #e0aa43; }
QPushButton:disabled { background: #555b66; color: #a0a4ab; }
QProgressBar {
  background: #222630; border: 1px solid #3b4250; border-radius: 4px;
  color: #e5e9f0; text-align: center; min-height: 20px;
}
QProgressBar::chunk { background: #c89432; border-radius: 3px; }
QTableWidget {
  background: #1d212a; alternate-background-color: #20252f;
  gridline-color: #303642; border: 1px solid #343b49;
}
QHeaderView::section {
  background: #292f3a; color: #d8dce5; border: 0;
  border-right: 1px solid #3b4250; padding: 7px; font-weight: 600;
}
QTableWidget::item:selected { background: #3c526c; }
QStatusBar { background: #11141a; color: #929aaa; }
"""


def create_application() -> tuple[QApplication, MainWindow]:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Albion Crafter")
    app.setOrganizationName("Albion Crafter")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    database = Database(default_database_path())
    database.initialize()
    market_repository = MarketPriceRepository(database)
    override_repository = PriceOverrideRepository(database)
    catalog_repository = CatalogRepository(database)
    settings_repository = SettingsRepository(database)
    station_fee_repository = StationFeeRepository(database)
    crafting_profile_repository = CraftingProfileRepository(database)
    history_repository = MarketHistoryRepository(database)
    snapshot_repository = PlanSnapshotRepository(database)
    preferences_repository = FindMoneyPreferencesRepository(settings_repository)
    find_money_services: dict[Region, FindMoneyService] = {}

    def find_money_service_factory(region: Region) -> FindMoneyService:
        service = find_money_services.get(region)
        if service is None:
            service = create_find_money_service(
                region,
                market_repository,
                override_repository,
                catalog_repository,
                station_fee_repository,
                crafting_profile_repository,
                history_repository,
                snapshot_repository,
            )
            find_money_services[region] = service
        return service

    selected_region = Region(str(settings_repository.get("region", Region.AMERICAS.value)))
    window = MainWindow(
        market_repository,
        override_repository,
        catalog_repository,
        settings_repository,
        station_fee_repository,
        crafting_profile_repository,
        history_repository,
        find_money_service_factory(selected_region),
        snapshot_repository,
        preferences_repository,
        find_money_service_factory,
    )
    return app, window


def main() -> int:
    app, window = create_application()
    window.show()
    if os.environ.get("ALBION_CRAFTER_SMOKE_TEST") == "1":
        QTimer.singleShot(250, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
