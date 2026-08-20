"""SQLite repositories used by the application."""

from .catalog import CatalogRepository
from .database import (
    LATEST_SCHEMA_VERSION,
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SchemaVersionError,
    SettingsRepository,
    default_data_directory,
    default_database_path,
)
from .v3 import (
    DEFAULT_HISTORY_RETENTION,
    CraftingProfileRepository,
    HistoryCoverage,
    MarketHistoryRepository,
    StationFeeRepository,
)
from .v4 import (
    DEFAULT_PLAN_RETENTION,
    FIND_MONEY_PREFERENCES_KEY,
    LEGACY_FIND_MONEY_PREFERENCES_KEY,
    V2_FIND_MONEY_PREFERENCES_KEY,
    FindMoneyPreferencesError,
    FindMoneyPreferencesRepository,
    PlanSnapshotAlreadyExists,
    PlanSnapshotError,
    PlanSnapshotIntegrityError,
    PlanSnapshotRepository,
    PlanSnapshotSummary,
    UnsupportedPlanSnapshotVersion,
)

__all__ = [
    "CatalogRepository",
    "CraftingProfileRepository",
    "DEFAULT_HISTORY_RETENTION",
    "DEFAULT_PLAN_RETENTION",
    "Database",
    "FIND_MONEY_PREFERENCES_KEY",
    "LEGACY_FIND_MONEY_PREFERENCES_KEY",
    "V2_FIND_MONEY_PREFERENCES_KEY",
    "FindMoneyPreferencesError",
    "FindMoneyPreferencesRepository",
    "HistoryCoverage",
    "LATEST_SCHEMA_VERSION",
    "MarketHistoryRepository",
    "MarketPriceRepository",
    "PriceOverrideRepository",
    "PlanSnapshotAlreadyExists",
    "PlanSnapshotError",
    "PlanSnapshotIntegrityError",
    "PlanSnapshotRepository",
    "PlanSnapshotSummary",
    "SchemaVersionError",
    "SettingsRepository",
    "StationFeeRepository",
    "UnsupportedPlanSnapshotVersion",
    "default_data_directory",
    "default_database_path",
]
