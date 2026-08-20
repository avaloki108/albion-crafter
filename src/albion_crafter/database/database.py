from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from albion_crafter.core.freshness import DEFAULT_CLOCK_SKEW_TOLERANCE
from albion_crafter.core.provenance import Provenance
from albion_crafter.market.models import MarketPrice, MarketSide, Region, UserPriceOverride

LATEST_SCHEMA_VERSION = 4


class SchemaVersionError(RuntimeError):
    """Raised when an on-disk database cannot be safely opened by this build."""


def default_data_directory() -> Path:
    override = os.environ.get("ALBION_CRAFTER_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Albion Crafter"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "albion-crafter"


def default_database_path() -> Path:
    return default_data_directory() / "albion-crafter.db"


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _open_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection and deterministically close it."""
        connection = self._open_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # Readable alias retained for repositories introduced during the V0.2 migration.
    connection = connect

    def initialize(self) -> None:
        # Probe an existing database read-only before _open_connection enables WAL,
        # which is a persistent setting. Future schemas must be rejected untouched.
        probed_version = self._probe_existing_schema_version()
        if probed_version is not None and probed_version > LATEST_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {probed_version} is newer than supported version "
                f"{LATEST_SCHEMA_VERSION}."
            )

        connection = self._open_connection()
        try:
            with connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > LATEST_SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"Database schema version {version} is newer than supported version "
                        f"{LATEST_SCHEMA_VERSION}."
                    )

                # Do not use executescript here: it can introduce implicit transaction
                # boundaries. Every migration, including its user_version update, must
                # commit or roll back together.
                connection.execute("BEGIN IMMEDIATE")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > LATEST_SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"Database schema version {version} is newer than supported version "
                        f"{LATEST_SCHEMA_VERSION}."
                    )

                if version in (0, 1):
                    self._migrate_legacy_market_table(connection)
                    self._create_v2_schema(connection)
                    connection.execute("PRAGMA user_version = 2")
                    version = 2

                if version == 2:
                    self._validate_v2_schema(connection)
                    self._migrate_v2_to_v3(connection)
                    connection.execute("PRAGMA user_version = 3")
                    version = 3

                if version == 3:
                    # Validate the complete preserved schema before adding V0.4 state. This
                    # prevents a damaged V3 database from being relabelled as healthy merely
                    # because the new plan table can be created successfully.
                    self._validate_v3_schema(connection)
                    self._migrate_v3_to_v4(connection)
                    connection.execute("PRAGMA user_version = 4")
                    version = 4

                if version != LATEST_SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"No migration path exists for schema version {version}."
                    )
                self._validate_v4_schema(connection)
        finally:
            connection.close()

    def _probe_existing_schema_version(self) -> int | None:
        if not self.path.exists():
            return None
        uri = self.path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    @staticmethod
    def _create_v2_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS market_prices (
                    region TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    city TEXT NOT NULL,
                    quality INTEGER NOT NULL CHECK (quality BETWEEN 1 AND 5),
                    sell_price INTEGER,
                    sell_price_timestamp TEXT,
                    buy_price INTEGER,
                    buy_price_timestamp TEXT,
                    fetched_at TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (region, item_id, city, quality)
                )""",
            """CREATE INDEX IF NOT EXISTS market_prices_lookup
                    ON market_prices (item_id, city, quality, region)""",
            """CREATE TABLE IF NOT EXISTS price_overrides (
                    region TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    city TEXT NOT NULL,
                    quality INTEGER NOT NULL CHECK (quality BETWEEN 1 AND 5),
                    side TEXT NOT NULL,
                    price INTEGER NOT NULL CHECK (price > 0),
                    entered_at TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (region, item_id, city, quality, side)
                )""",
            """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                )""",
            """CREATE TABLE IF NOT EXISTS catalog_items (
                    item_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    tier INTEGER,
                    enchantment INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    crafting_category TEXT NOT NULL,
                    max_quality INTEGER,
                    item_value REAL,
                    craftable INTEGER NOT NULL,
                    provenance TEXT NOT NULL,
                    source_version TEXT NOT NULL
                )""",
            """CREATE INDEX IF NOT EXISTS catalog_items_search
                    ON catalog_items(display_name, tier, enchantment)""",
            """CREATE TABLE IF NOT EXISTS catalog_recipes (
                    output_item_id TEXT PRIMARY KEY REFERENCES catalog_items(item_id)
                        ON DELETE CASCADE,
                    output_quantity INTEGER NOT NULL,
                    base_focus_cost REAL,
                    recipe_ambiguous INTEGER NOT NULL,
                    provenance TEXT NOT NULL,
                    source_version TEXT NOT NULL
                )""",
            """CREATE TABLE IF NOT EXISTS catalog_materials (
                    output_item_id TEXT NOT NULL REFERENCES catalog_recipes(output_item_id)
                        ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    returnable INTEGER,
                    PRIMARY KEY (output_item_id, position)
                )""",
            """CREATE TABLE IF NOT EXISTS catalog_imports (
                    source_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    source_timestamp TEXT,
                    imported_at TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    recipe_count INTEGER NOT NULL
                )""",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE station_fees (
                    region TEXT NOT NULL,
                    city TEXT NOT NULL,
                    station_type TEXT NOT NULL,
                    displayed_fee REAL NOT NULL CHECK (displayed_fee >= 0),
                    observed_at TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (region, city, station_type)
                )""",
            """CREATE TABLE crafting_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    available_focus REAL NOT NULL DEFAULT 0 CHECK (available_focus >= 0),
                    complete_groups_json TEXT NOT NULL DEFAULT '[]',
                    assume_zero_for_unspecified INTEGER NOT NULL DEFAULT 0 CHECK (
                        assume_zero_for_unspecified IN (0, 1)
                    ),
                    updated_at TEXT NOT NULL,
                    provenance TEXT NOT NULL
                )""",
            """CREATE TABLE crafting_skill_levels (
                    profile_id TEXT NOT NULL REFERENCES crafting_profiles(profile_id)
                        ON DELETE CASCADE,
                    skill_key TEXT NOT NULL,
                    crafting_group TEXT NOT NULL,
                    level INTEGER CHECK (level BETWEEN 0 AND 100),
                    mutual_fce_per_level REAL NOT NULL CHECK (mutual_fce_per_level >= 0),
                    updated_at TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (profile_id, skill_key)
                )""",
            """CREATE TABLE focus_efficiency_overrides (
                    profile_id TEXT NOT NULL REFERENCES crafting_profiles(profile_id)
                        ON DELETE CASCADE,
                    mapping_key TEXT NOT NULL,
                    focus_cost_efficiency REAL NOT NULL CHECK (focus_cost_efficiency >= 0),
                    entered_at TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (profile_id, mapping_key)
                )""",
            """CREATE TABLE market_history_intervals (
                    region TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    city TEXT NOT NULL,
                    quality INTEGER NOT NULL CHECK (quality BETWEEN 1 AND 5),
                    time_scale_hours INTEGER NOT NULL CHECK (time_scale_hours IN (1, 6, 24)),
                    observed_at TEXT NOT NULL,
                    item_count INTEGER NOT NULL CHECK (item_count >= 0),
                    average_price REAL,
                    minimum_price REAL,
                    maximum_price REAL,
                    fetched_at TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    CHECK (average_price IS NULL OR average_price >= 0),
                    CHECK (minimum_price IS NULL OR minimum_price >= 0),
                    CHECK (maximum_price IS NULL OR maximum_price >= 0),
                    PRIMARY KEY (
                        region, item_id, city, quality, time_scale_hours, observed_at
                    )
                )""",
            """CREATE TABLE market_history_coverage (
                    region TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    city TEXT NOT NULL,
                    quality INTEGER NOT NULL CHECK (quality BETWEEN 1 AND 5),
                    time_scale_hours INTEGER NOT NULL CHECK (time_scale_hours IN (1, 6, 24)),
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_count INTEGER NOT NULL CHECK (record_count >= 0),
                    error_message TEXT,
                    PRIMARY KEY (region, item_id, city, quality, time_scale_hours)
                )""",
            """CREATE TABLE catalog_import_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    source_timestamp TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    raw_sha256 TEXT,
                    formatted_sha256 TEXT,
                    previous_item_count INTEGER NOT NULL CHECK (previous_item_count >= 0),
                    previous_recipe_count INTEGER NOT NULL CHECK (previous_recipe_count >= 0),
                    item_count INTEGER NOT NULL CHECK (item_count >= 0),
                    recipe_count INTEGER NOT NULL CHECK (recipe_count >= 0),
                    ingredient_count INTEGER NOT NULL CHECK (ingredient_count >= 0),
                    unknown_returnability_count INTEGER NOT NULL CHECK (
                        unknown_returnability_count >= 0
                    ),
                    skipped_malformed_count INTEGER NOT NULL CHECK (skipped_malformed_count >= 0),
                    validation_status TEXT NOT NULL,
                    validation_messages_json TEXT NOT NULL,
                    forced INTEGER NOT NULL CHECK (forced IN (0, 1)),
                    activated INTEGER NOT NULL CHECK (activated IN (0, 1))
                )""",
            """CREATE INDEX catalog_items_scan
                    ON catalog_items(craftable, tier, enchantment, category,
                                     crafting_category, item_id)""",
            """CREATE INDEX catalog_items_category_scan
                    ON catalog_items(craftable, category, tier, enchantment, item_id)""",
            """CREATE INDEX catalog_items_crafting_scan
                    ON catalog_items(
                        craftable, crafting_category, tier, enchantment, item_id
                    )""",
            """CREATE INDEX catalog_materials_item
                    ON catalog_materials(item_id, output_item_id)""",
            """CREATE INDEX catalog_import_runs_source_version
                    ON catalog_import_runs(source_id, source_version, finished_at DESC)""",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Add immutable, self-contained Find Me Money plan snapshots."""

        statements = (
            """CREATE TABLE plan_snapshots (
                    snapshot_id TEXT PRIMARY KEY NOT NULL,
                    snapshot_format_version INTEGER NOT NULL CHECK (
                        snapshot_format_version > 0
                    ),
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    region TEXT NOT NULL,
                    plan_status TEXT NOT NULL,
                    optimization_status TEXT NOT NULL,
                    catalog_source_version TEXT NOT NULL,
                    mechanics_ruleset_id TEXT NOT NULL,
                    action_count INTEGER NOT NULL CHECK (action_count >= 0),
                    total_pre_revenue_cash INTEGER NOT NULL CHECK (
                        total_pre_revenue_cash >= 0
                    ),
                    total_focus INTEGER NOT NULL CHECK (total_focus >= 0),
                    total_expected_profit INTEGER NOT NULL,
                    silver_remaining INTEGER NOT NULL CHECK (silver_remaining >= 0),
                    focus_remaining INTEGER NOT NULL CHECK (focus_remaining >= 0),
                    oldest_market_observed_at TEXT,
                    oldest_station_observed_at TEXT,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64)
                )""",
            """CREATE INDEX plan_snapshots_recent
                    ON plan_snapshots(created_at DESC, snapshot_id DESC)""",
        )
        for statement in statements:
            connection.execute(statement)

    @classmethod
    def _validate_v2_schema(cls, connection: sqlite3.Connection) -> None:
        columns = cls._v2_required_columns()
        cls._validate_required_tables(connection, set(columns), version=2)
        cls._validate_required_columns(connection, columns, version=2)
        cls._validate_schema_definitions(connection, set(columns), version=2)

    @classmethod
    def _validate_v3_schema(cls, connection: sqlite3.Connection) -> None:
        columns = cls._v3_required_columns()
        cls._validate_required_tables(connection, set(columns), version=3)
        cls._validate_required_columns(connection, columns, version=3)
        cls._validate_schema_definitions(connection, set(columns), version=3)

    @classmethod
    def _validate_v4_schema(cls, connection: sqlite3.Connection) -> None:
        columns = {
            **cls._v3_required_columns(),
            "plan_snapshots": {
                "snapshot_id",
                "snapshot_format_version",
                "created_at",
                "completed_at",
                "region",
                "plan_status",
                "optimization_status",
                "catalog_source_version",
                "mechanics_ruleset_id",
                "action_count",
                "total_pre_revenue_cash",
                "total_focus",
                "total_expected_profit",
                "silver_remaining",
                "focus_remaining",
                "oldest_market_observed_at",
                "oldest_station_observed_at",
                "payload_json",
                "payload_sha256",
            },
        }
        cls._validate_required_tables(connection, set(columns), version=4)
        cls._validate_required_columns(connection, columns, version=4)
        cls._validate_schema_definitions(connection, set(columns), version=4)

    @staticmethod
    def _v2_required_columns() -> dict[str, set[str]]:
        return {
            "market_prices": {
                "region",
                "item_id",
                "city",
                "quality",
                "sell_price",
                "sell_price_timestamp",
                "buy_price",
                "buy_price_timestamp",
                "fetched_at",
                "provenance",
            },
            "price_overrides": {
                "region",
                "item_id",
                "city",
                "quality",
                "side",
                "price",
                "entered_at",
                "provenance",
            },
            "settings": {"key", "value_json"},
            "catalog_items": {
                "item_id",
                "display_name",
                "tier",
                "enchantment",
                "category",
                "subcategory",
                "crafting_category",
                "max_quality",
                "item_value",
                "craftable",
                "provenance",
                "source_version",
            },
            "catalog_recipes": {
                "output_item_id",
                "output_quantity",
                "base_focus_cost",
                "recipe_ambiguous",
                "provenance",
                "source_version",
            },
            "catalog_materials": {
                "output_item_id",
                "position",
                "item_id",
                "quantity",
                "returnable",
            },
            "catalog_imports": {
                "source_id",
                "source_url",
                "source_version",
                "source_timestamp",
                "imported_at",
                "item_count",
                "recipe_count",
            },
        }

    @classmethod
    def _v3_required_columns(cls) -> dict[str, set[str]]:
        return {
            **cls._v2_required_columns(),
            "station_fees": {
                "region",
                "city",
                "station_type",
                "displayed_fee",
                "observed_at",
                "provenance",
            },
            "crafting_profiles": {
                "profile_id",
                "name",
                "available_focus",
                "complete_groups_json",
                "assume_zero_for_unspecified",
                "updated_at",
                "provenance",
            },
            "crafting_skill_levels": {
                "profile_id",
                "skill_key",
                "crafting_group",
                "level",
                "mutual_fce_per_level",
                "updated_at",
                "provenance",
            },
            "focus_efficiency_overrides": {
                "profile_id",
                "mapping_key",
                "focus_cost_efficiency",
                "entered_at",
                "provenance",
            },
            "market_history_intervals": {
                "region",
                "item_id",
                "city",
                "quality",
                "time_scale_hours",
                "observed_at",
                "item_count",
                "average_price",
                "minimum_price",
                "maximum_price",
                "fetched_at",
                "provenance",
            },
            "market_history_coverage": {
                "region",
                "item_id",
                "city",
                "quality",
                "time_scale_hours",
                "window_start",
                "window_end",
                "fetched_at",
                "status",
                "record_count",
                "error_message",
            },
            "catalog_import_runs": {
                "run_id",
                "source_id",
                "source_url",
                "source_version",
                "source_timestamp",
                "started_at",
                "finished_at",
                "raw_sha256",
                "formatted_sha256",
                "previous_item_count",
                "previous_recipe_count",
                "item_count",
                "recipe_count",
                "ingredient_count",
                "unknown_returnability_count",
                "skipped_malformed_count",
                "validation_status",
                "validation_messages_json",
                "forced",
                "activated",
            },
        }

    @staticmethod
    def _validate_required_tables(
        connection: sqlite3.Connection,
        required: set[str],
        *,
        version: int,
    ) -> None:
        available = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(required - available)
        if missing:
            raise SchemaVersionError(
                f"Schema version {version} is missing required tables: {', '.join(missing)}"
            )

    @staticmethod
    def _validate_required_columns(
        connection: sqlite3.Connection,
        required: dict[str, set[str]],
        *,
        version: int,
    ) -> None:
        failures: list[str] = []
        for table, expected in required.items():
            available = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = sorted(expected - available)
            if missing:
                failures.append(f"{table}({', '.join(missing)})")
        if failures:
            raise SchemaVersionError(
                f"Schema version {version} has incompatible columns: " + "; ".join(failures)
            )

    @classmethod
    def _validate_schema_definitions(
        cls,
        connection: sqlite3.Connection,
        required_tables: set[str],
        *,
        version: int,
    ) -> None:
        """Compare canonical table/index DDL, including keys, checks, FKs, and order.

        Column-name checks alone are unsafe for SQLite: a table can expose every
        expected name while lacking the primary key needed by an ``ON CONFLICT``
        repository write, or while silently losing a CHECK/FK. A temporary canonical
        schema keeps this validation tied directly to the migration definitions.
        """

        reference = sqlite3.connect(":memory:")
        try:
            reference.execute("PRAGMA foreign_keys = ON")
            cls._create_v2_schema(reference)
            if version >= 3:
                cls._migrate_v2_to_v3(reference)
            if version >= 4:
                cls._migrate_v3_to_v4(reference)

            expected_tables = {
                str(row[0]): str(row[1])
                for row in reference.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
                )
                if str(row[0]) in required_tables
            }
            expected_indexes = {
                str(row[0]): str(row[1])
                for row in reference.execute(
                    """SELECT name, sql FROM sqlite_master
                       WHERE type='index' AND sql IS NOT NULL"""
                )
            }
        finally:
            reference.close()

        failures: list[str] = []
        for name, expected_sql in expected_tables.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            if row is None or cls._normalize_schema_sql(row[0]) != cls._normalize_schema_sql(
                expected_sql
            ):
                failures.append(f"table {name}")
        for name, expected_sql in expected_indexes.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            if row is None or cls._normalize_schema_sql(row[0]) != cls._normalize_schema_sql(
                expected_sql
            ):
                failures.append(f"index {name}")
        if failures:
            raise SchemaVersionError(
                f"Schema version {version} has incompatible definitions: " + ", ".join(failures)
            )

    @staticmethod
    def _normalize_schema_sql(value: object) -> str:
        return "".join(str(value).split()).casefold()

    @staticmethod
    def _migrate_legacy_market_table(connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_prices'"
        ).fetchone()
        if not exists:
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(market_prices)")}
        if "provenance" in columns:
            return
        connection.execute("ALTER TABLE market_prices RENAME TO market_prices_v1")
        connection.execute(
            """CREATE TABLE market_prices (
                region TEXT NOT NULL,
                item_id TEXT NOT NULL,
                city TEXT NOT NULL,
                quality INTEGER NOT NULL CHECK (quality BETWEEN 1 AND 5),
                sell_price INTEGER,
                sell_price_timestamp TEXT,
                buy_price INTEGER,
                buy_price_timestamp TEXT,
                fetched_at TEXT NOT NULL,
                provenance TEXT NOT NULL,
                PRIMARY KEY (region, item_id, city, quality)
            )"""
        )
        connection.execute(
            """INSERT INTO market_prices (
                region, item_id, city, quality, sell_price, sell_price_timestamp,
                buy_price, buy_price_timestamp, fetched_at, provenance
            )
            SELECT region, item_id, city, quality, sell_price, sell_price_timestamp,
                   buy_price, buy_price_timestamp, fetched_at, 'aodp_cached'
            FROM market_prices_v1
            WHERE source = 'aodp'"""
        )
        connection.execute("DROP TABLE market_prices_v1")


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class MarketPriceRepository:
    def __init__(
        self,
        database: Database,
        *,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))

    def upsert_many(self, records: Iterable[MarketPrice]) -> None:
        records = list(records)
        invalid = [
            record.provenance
            for record in records
            if not record.provenance.is_production_market_data
        ]
        if invalid:
            raise ValueError(
                "Production market cache accepts only AODP observations; rejected "
                + ", ".join(sorted({value.value for value in invalid}))
            )
        if not records:
            return
        now = self._wall_clock()
        if now.tzinfo is None:
            raise ValueError("market repository wall clock must be timezone-aware")
        future_boundary = now.astimezone(UTC) + DEFAULT_CLOCK_SKEW_TOLERANCE
        rows = [
            (
                record.region.value,
                record.item_id,
                record.city,
                record.quality,
                record.sell_price,
                _serialize_datetime(record.sell_price_timestamp),
                record.buy_price,
                _serialize_datetime(record.buy_price_timestamp),
                _serialize_datetime(record.fetched_at),
                Provenance.AODP_CACHED.value,
                _serialize_datetime(future_boundary),
                _serialize_datetime(future_boundary),
                _serialize_datetime(future_boundary),
                _serialize_datetime(future_boundary),
            )
            for record in records
        ]
        with self.database.connection() as connection:
            connection.executemany(
                """
                INSERT INTO market_prices (
                    region, item_id, city, quality, sell_price, sell_price_timestamp,
                    buy_price, buy_price_timestamp, fetched_at, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region, item_id, city, quality) DO UPDATE SET
                    sell_price = CASE
                        WHEN excluded.sell_price IS NULL THEN market_prices.sell_price
                        WHEN market_prices.sell_price_timestamp IS NULL
                          OR market_prices.sell_price_timestamp > ?
                          OR excluded.sell_price_timestamp >= market_prices.sell_price_timestamp
                        THEN excluded.sell_price ELSE market_prices.sell_price END,
                    sell_price_timestamp = CASE
                        WHEN excluded.sell_price IS NULL THEN market_prices.sell_price_timestamp
                        WHEN market_prices.sell_price_timestamp IS NULL
                          OR market_prices.sell_price_timestamp > ?
                          OR excluded.sell_price_timestamp >= market_prices.sell_price_timestamp
                        THEN excluded.sell_price_timestamp
                        ELSE market_prices.sell_price_timestamp END,
                    buy_price = CASE
                        WHEN excluded.buy_price IS NULL THEN market_prices.buy_price
                        WHEN market_prices.buy_price_timestamp IS NULL
                          OR market_prices.buy_price_timestamp > ?
                          OR excluded.buy_price_timestamp >= market_prices.buy_price_timestamp
                        THEN excluded.buy_price ELSE market_prices.buy_price END,
                    buy_price_timestamp = CASE
                        WHEN excluded.buy_price IS NULL THEN market_prices.buy_price_timestamp
                        WHEN market_prices.buy_price_timestamp IS NULL
                          OR market_prices.buy_price_timestamp > ?
                          OR excluded.buy_price_timestamp >= market_prices.buy_price_timestamp
                        THEN excluded.buy_price_timestamp
                        ELSE market_prices.buy_price_timestamp END,
                    fetched_at = MAX(market_prices.fetched_at, excluded.fetched_at),
                    provenance = 'aodp_cached'
                """,
                rows,
            )

    def get(
        self,
        item_id: str,
        city: str,
        quality: int,
        region: Region,
    ) -> MarketPrice | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM market_prices
                WHERE region = ? AND item_id = ? AND city = ? AND quality = ?
                """,
                (region.value, item_id, city, quality),
            ).fetchone()
        return self._to_model(row) if row else None

    def list_all(self, region: Region | None = None) -> list[MarketPrice]:
        with self.database.connection() as connection:
            if region is None:
                rows = connection.execute(
                    "SELECT * FROM market_prices ORDER BY item_id, city, quality"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM market_prices WHERE region = ? ORDER BY item_id, city, quality",
                    (region.value,),
                ).fetchall()
        return [self._to_model(row) for row in rows]

    def list_for_display(self, region: Region, *, limit: int = 1_000) -> list[MarketPrice]:
        """Load a useful bounded slice for the GUI rather than hydrating the full cache."""

        if limit < 1:
            return []
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM market_prices
                WHERE region = ?
                ORDER BY
                    (sell_price IS NULL AND buy_price IS NULL),
                    fetched_at DESC,
                    item_id,
                    city,
                    quality
                LIMIT ?
                """,
                (region.value, limit),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def list_for_scan(
        self,
        region: Region,
        *,
        cities: Iterable[str] = (),
        qualities: Iterable[int] = (),
        item_ids: Iterable[str] | None = None,
    ) -> list[MarketPrice]:
        """Bulk-load a scan working set in a bounded number of queries.

        Item IDs are chunked conservatively for SQLite builds with a low host-parameter
        limit. City and quality filters are applied inside every chunk.
        """
        city_values = tuple(dict.fromkeys(cities))
        quality_values = tuple(dict.fromkeys(qualities))
        requested_ids = None if item_ids is None else tuple(dict.fromkeys(item_ids))
        if requested_ids == ():
            return []

        fixed_parameters = 1 + len(city_values) + len(quality_values)
        chunk_size = max(1, 900 - fixed_parameters)
        id_chunks: tuple[tuple[str, ...] | None, ...]
        if requested_ids is None:
            id_chunks = (None,)
        else:
            id_chunks = tuple(
                requested_ids[offset : offset + chunk_size]
                for offset in range(0, len(requested_ids), chunk_size)
            )

        rows: list[sqlite3.Row] = []
        with self.database.connection() as connection:
            for id_chunk in id_chunks:
                clauses = ["region = ?"]
                values: list[object] = [region.value]
                if city_values:
                    clauses.append(f"city IN ({','.join('?' for _ in city_values)})")
                    values.extend(city_values)
                if quality_values:
                    clauses.append(f"quality IN ({','.join('?' for _ in quality_values)})")
                    values.extend(quality_values)
                if id_chunk is not None:
                    clauses.append(f"item_id IN ({','.join('?' for _ in id_chunk)})")
                    values.extend(id_chunk)
                rows.extend(
                    connection.execute(
                        f"SELECT * FROM market_prices WHERE {' AND '.join(clauses)}",  # noqa: S608
                        values,
                    ).fetchall()
                )
        return [self._to_model(row) for row in rows]

    def count(self, region: Region | None = None) -> int:
        with self.database.connection() as connection:
            if region is None:
                row = connection.execute("SELECT COUNT(*) FROM market_prices").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM market_prices WHERE region = ?",
                    (region.value,),
                ).fetchone()
        return int(row[0])

    def list_tracked_item_ids(self, region: Region) -> tuple[str, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT item_id FROM market_prices WHERE region = ? ORDER BY item_id",
                (region.value,),
            ).fetchall()
        return tuple(str(row["item_id"]) for row in rows)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> MarketPrice:
        fetched_at = _parse_datetime(row["fetched_at"])
        assert fetched_at is not None
        provenance = Provenance(row["provenance"])
        if provenance is not Provenance.AODP_CACHED:
            raise ValueError(f"Unexpected production market provenance: {provenance.value}")
        return MarketPrice(
            item_id=row["item_id"],
            city=row["city"],
            quality=row["quality"],
            region=Region(row["region"]),
            sell_price=row["sell_price"],
            sell_price_timestamp=_parse_datetime(row["sell_price_timestamp"]),
            buy_price=row["buy_price"],
            buy_price_timestamp=_parse_datetime(row["buy_price_timestamp"]),
            fetched_at=fetched_at,
            provenance=provenance,
        )


class PriceOverrideRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set(self, override: UserPriceOverride) -> None:
        if override.provenance is not Provenance.USER_OVERRIDE:
            raise ValueError("price overrides must have USER_OVERRIDE provenance")
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO price_overrides (
                    region, item_id, city, quality, side, price, entered_at, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region, item_id, city, quality, side) DO UPDATE SET
                    price = excluded.price, entered_at = excluded.entered_at,
                    provenance = excluded.provenance
                """,
                (
                    override.region.value,
                    override.item_id,
                    override.city,
                    override.quality,
                    override.side.value,
                    override.price,
                    _serialize_datetime(override.entered_at),
                    override.provenance.value,
                ),
            )

    def get(
        self,
        item_id: str,
        city: str,
        quality: int,
        region: Region,
        side: MarketSide,
    ) -> UserPriceOverride | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM price_overrides
                WHERE region=? AND item_id=? AND city=? AND quality=? AND side=?
                """,
                (region.value, item_id, city, quality, side.value),
            ).fetchone()
        return self._to_model(row) if row else None

    def remove(
        self,
        item_id: str,
        city: str,
        quality: int,
        region: Region,
        side: MarketSide,
    ) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM price_overrides
                WHERE region=? AND item_id=? AND city=? AND quality=? AND side=?
                """,
                (region.value, item_id, city, quality, side.value),
            )
            return cursor.rowcount > 0

    def list_all(self, region: Region | None = None) -> list[UserPriceOverride]:
        with self.database.connection() as connection:
            if region is None:
                rows = connection.execute("SELECT * FROM price_overrides").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM price_overrides WHERE region=?", (region.value,)
                ).fetchall()
        return [self._to_model(row) for row in rows]

    def list_for_scan(
        self,
        region: Region,
        *,
        cities: Iterable[str] = (),
        qualities: Iterable[int] = (),
        item_ids: Iterable[str] | None = None,
    ) -> list[UserPriceOverride]:
        city_values = tuple(dict.fromkeys(cities))
        quality_values = tuple(dict.fromkeys(qualities))
        requested_ids = None if item_ids is None else tuple(dict.fromkeys(item_ids))
        if requested_ids == ():
            return []
        fixed_parameters = 1 + len(city_values) + len(quality_values)
        chunk_size = max(1, 900 - fixed_parameters)
        id_chunks: tuple[tuple[str, ...] | None, ...] = (
            (None,)
            if requested_ids is None
            else tuple(
                requested_ids[offset : offset + chunk_size]
                for offset in range(0, len(requested_ids), chunk_size)
            )
        )
        rows: list[sqlite3.Row] = []
        with self.database.connection() as connection:
            for id_chunk in id_chunks:
                clauses = ["region = ?"]
                values: list[object] = [region.value]
                if city_values:
                    clauses.append(f"city IN ({','.join('?' for _ in city_values)})")
                    values.extend(city_values)
                if quality_values:
                    clauses.append(f"quality IN ({','.join('?' for _ in quality_values)})")
                    values.extend(quality_values)
                if id_chunk is not None:
                    clauses.append(f"item_id IN ({','.join('?' for _ in id_chunk)})")
                    values.extend(id_chunk)
                rows.extend(
                    connection.execute(
                        f"SELECT * FROM price_overrides WHERE {' AND '.join(clauses)}",  # noqa: S608
                        values,
                    ).fetchall()
                )
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> UserPriceOverride:
        entered_at = _parse_datetime(row["entered_at"])
        assert entered_at is not None
        provenance = Provenance(row["provenance"])
        if provenance is not Provenance.USER_OVERRIDE:
            raise ValueError(f"Unexpected price override provenance: {provenance.value}")
        return UserPriceOverride(
            item_id=row["item_id"],
            city=row["city"],
            quality=row["quality"],
            region=Region(row["region"]),
            side=MarketSide(row["side"]),
            price=row["price"],
            entered_at=entered_at,
            provenance=provenance,
        )


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str, default: Any = None) -> Any:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def set(self, key: str, value: Any) -> None:
        self.set_many({key: value})

    def get_many(self, keys: Iterable[str]) -> dict[str, Any]:
        key_values = tuple(dict.fromkeys(keys))
        if not key_values:
            return {}
        placeholders = ",".join("?" for _ in key_values)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT key, value_json FROM settings WHERE key IN ({placeholders})",  # noqa: S608
                key_values,
            ).fetchall()
        return {str(row["key"]): json.loads(row["value_json"]) for row in rows}

    def set_many(self, values: dict[str, Any]) -> None:
        rows = [(key, json.dumps(value)) for key, value in values.items()]
        with self.database.connection() as connection:
            connection.executemany(
                """
                INSERT INTO settings (key, value_json) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                rows,
            )
