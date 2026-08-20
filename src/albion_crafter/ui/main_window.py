from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from albion_crafter.core.models import ActionKind
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
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
from albion_crafter.market.history import AODPHistoryClient
from albion_crafter.market.history_cache import CachedOutputHistoryService
from albion_crafter.market.models import Region
from albion_crafter.market.pricing import PriceResolver
from albion_crafter.market.recipe_refresh import RecipePriceRefreshService
from albion_crafter.opportunity.service import OpportunityScannerService
from albion_crafter.planning.current_refresh import CurrentMarketRefreshExecutor
from albion_crafter.planning.models import FindMoneyConstraints, TransportPolicy
from albion_crafter.planning.preflight import FindMoneyPreflightPlanner
from albion_crafter.planning.service import FindMoneyService

from .calculator_view import CalculatorView
from .craft_scanner import CraftScannerView
from .find_money import FindMoneyView, ServiceFactory
from .market_data import MarketDataView
from .settings_view import SettingsView


def create_find_money_service(
    region: Region,
    market_repository: MarketPriceRepository,
    override_repository: PriceOverrideRepository,
    catalog_repository: CatalogRepository,
    station_fee_repository: StationFeeRepository,
    crafting_profile_repository: CraftingProfileRepository,
    history_repository: MarketHistoryRepository,
    snapshot_repository: PlanSnapshotRepository,
) -> FindMoneyService:
    """Compose the production planner without performing any network request."""

    preflight = FindMoneyPreflightPlanner(
        catalog_repository,
        market_repository,
        override_repository,
        station_fee_repository,
        crafting_profile_repository,
        history_repository,
    )
    return FindMoneyService(
        preflight,
        market_repository,
        override_repository,
        crafting_profile_repository,
        history_repository,
        snapshots=snapshot_repository,
        current_refresh=CurrentMarketRefreshExecutor(market_repository),
        history_refresh=CachedOutputHistoryService(
            AODPHistoryClient(region),
            history_repository,
        ),
    )


class MainWindow(QMainWindow):
    def __init__(
        self,
        market_repository: MarketPriceRepository,
        override_repository: PriceOverrideRepository,
        catalog_repository: CatalogRepository,
        settings_repository: SettingsRepository,
        station_fee_repository: StationFeeRepository | None = None,
        crafting_profile_repository: CraftingProfileRepository | None = None,
        history_repository: MarketHistoryRepository | None = None,
        find_money_service: FindMoneyService | None = None,
        snapshot_repository: PlanSnapshotRepository | None = None,
        preferences_repository: FindMoneyPreferencesRepository | None = None,
        find_money_service_factory: ServiceFactory | None = None,
        *,
        auto_refresh_market_on_startup: bool = False,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Albion Crafter")
        self.resize(1380, 860)
        resolver = PriceResolver(market_repository, override_repository)
        station_fee_repository = station_fee_repository or StationFeeRepository(
            market_repository.database
        )
        crafting_profile_repository = crafting_profile_repository or CraftingProfileRepository(
            market_repository.database
        )
        history_repository = history_repository or MarketHistoryRepository(
            market_repository.database
        )
        snapshot_repository = snapshot_repository or PlanSnapshotRepository(
            market_repository.database
        )
        preferences_repository = preferences_repository or FindMoneyPreferencesRepository(
            settings_repository
        )
        default_constraints = FindMoneyConstraints(
            available_silver=1_000_000,
            available_focus=int(settings_repository.get("available_focus", 10_000)),
            region=Region(str(settings_repository.get("region", Region.AMERICAS.value))),
            premium=bool(settings_repository.get("premium", True)),
            material_cities=(
                str(settings_repository.get("default_material_buy_city", "Bridgewatch")),
            ),
            craft_cities=(str(settings_repository.get("default_craft_city", "Bridgewatch")),),
            sell_cities=(str(settings_repository.get("default_sell_city", "Bridgewatch")),),
            use_focus=bool(settings_repository.get("focus_enabled", False)),
            max_market_age=timedelta(hours=int(settings_repository.get("max_market_age_hours", 4))),
            max_station_fee_age=timedelta(
                hours=int(settings_repository.get("max_station_fee_age_hours", 24))
            ),
            action_kinds=frozenset(ActionKind),
            transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        )
        if find_money_service_factory is None and find_money_service is None:
            services: dict[Region, FindMoneyService] = {}

            def service_for_region(region: Region) -> FindMoneyService:
                service = services.get(region)
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
                    services[region] = service
                return service

            find_money_service_factory = service_for_region
            find_money_service = service_for_region(default_constraints.region)
        elif find_money_service is None:
            assert find_money_service_factory is not None
            find_money_service = find_money_service_factory(default_constraints.region)
        scanner_service = OpportunityScannerService(
            catalog_repository,
            market_repository,
            override_repository,
            station_fee_repository,
            crafting_profile_repository,
            history_repository,
        )

        container = QWidget()
        root = QHBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        side_layout = QVBoxLayout(sidebar)
        brand = QLabel("ALBION\nCRAFTER")
        brand.setObjectName("brand")
        side_layout.addWidget(brand)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        for text in (
            "Find Me Money",
            "Production Calculator",
            "Craft Scanner",
            "Market Data",
            "Settings",
        ):
            self.navigation.addItem(QListWidgetItem(text))
        side_layout.addWidget(self.navigation)
        version = QLabel("V0.6.1\nSimple Mode")
        version.setObjectName("muted")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side_layout.addWidget(version)

        self.pages = QStackedWidget()
        self.scanner = CraftScannerView(
            scanner_service,
            settings_repository,
            catalog_repository,
        )
        self.find_money = FindMoneyView(
            find_money_service,
            snapshot_repository,
            preferences_repository,
            service_factory=find_money_service_factory,
            default_constraints=default_constraints,
        )
        self.calculator = CalculatorView(
            resolver,
            settings_repository,
            catalog_repository,
            station_fee_repository,
            crafting_profile_repository,
            refresh_service=RecipePriceRefreshService(market_repository),
        )
        self.market = MarketDataView(
            market_repository,
            override_repository,
            catalog_repository,
            settings_repository,
            history_repository,
            auto_refresh_on_startup=auto_refresh_market_on_startup,
        )
        self.settings = SettingsView(
            settings_repository,
            station_fee_repository,
            crafting_profile_repository,
            catalog_repository,
        )
        for page in (
            self.find_money,
            self.calculator,
            self.scanner,
            self.market,
            self.settings,
        ):
            self.pages.addWidget(page)
        root.addWidget(sidebar)
        root.addWidget(self.pages, 1)
        self.setCentralWidget(container)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.market.data_changed.connect(self._refresh_calculations)
        self.market.catalog_changed.connect(self._catalog_changed)
        self.settings.settings_saved.connect(self._settings_saved)
        self.settings.station_fees_changed.connect(self._crafting_evidence_changed)
        self.settings.crafting_profile_changed.connect(self._crafting_evidence_changed)
        self.calculator.open_market_data_requested.connect(lambda: self.navigation.setCurrentRow(3))
        self.calculator.open_settings_requested.connect(lambda: self.navigation.setCurrentRow(4))
        self.calculator.prices_refreshed.connect(self._calculator_prices_refreshed)
        self.calculator.station_fee_saved.connect(self._calculator_station_fee_saved)
        self.find_money.focus_setup_requested.connect(lambda: self.navigation.setCurrentRow(4))
        self.find_money.evidence_saved.connect(self._find_money_evidence_saved)
        self.statusBar().showMessage("Enter your bankroll and press FIND ME MONEY")

    def _find_money_evidence_saved(self) -> None:
        self.market.reload()
        self.settings.refresh_station_fees()
        self.scanner.refresh()
        self.statusBar().showMessage("Find Me Money evidence saved", 5000)

    def _refresh_calculations(self) -> None:
        self.scanner.refresh()
        self.calculator.calculate()

    def _settings_saved(self) -> None:
        self.market.reload()
        self.calculator.load_defaults()
        self._refresh_calculations()
        self.statusBar().showMessage("Settings saved", 3000)

    def _catalog_changed(self) -> None:
        self.calculator.refresh_catalog()
        self.scanner.refresh()
        self.statusBar().showMessage("Static game-data catalog updated", 5000)

    def _calculator_prices_refreshed(self) -> None:
        self.market.reload()
        self.scanner.refresh()
        self.statusBar().showMessage("Selected recipe prices refreshed", 5000)

    def _calculator_station_fee_saved(self) -> None:
        # Keep the Calculator's current cities, quantity, and assumptions intact.
        # A generic Settings reload would replace them with global defaults.
        self.settings.refresh_station_fees()
        self.scanner.refresh()
        self.statusBar().showMessage("Calculator station fee saved", 5000)

    def _crafting_evidence_changed(self) -> None:
        # Fee/profile edits change evidence, not the user's current scenario.
        self.calculator.calculate()
        self.scanner.refresh()
        self.statusBar().showMessage("Crafting evidence updated", 5000)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        # Child widgets are normally hidden, not closed, when their top-level
        # window exits. Explicitly cancel network work and detach callbacks.
        self.market.shutdown()
        self.calculator.shutdown()
        super().closeEvent(event)
