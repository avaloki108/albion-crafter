import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillProfile,
    ManualFocusEfficiencyOverride,
)
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    LATEST_SCHEMA_VERSION,
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SchemaVersionError,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    HistoryCoverage,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.models import MarketPrice, MarketSide, Region, UserPriceOverride


def _create_v2_database(path) -> None:
    database = Database(path)
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        database._create_v2_schema(connection)
        connection.execute("PRAGMA user_version = 2")


def test_new_database_has_v3_tables_and_scan_indexes(tmp_path) -> None:
    database = Database(tmp_path / "new.db")
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {
        "station_fees",
        "crafting_profiles",
        "crafting_skill_levels",
        "focus_efficiency_overrides",
        "market_history_intervals",
        "market_history_coverage",
        "catalog_import_runs",
    } <= tables
    assert {
        "catalog_items_scan",
        "catalog_items_category_scan",
        "catalog_items_crafting_scan",
        "catalog_materials_item",
    } <= indexes


def test_v2_migration_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "v2.db"
    _create_v2_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO settings(key, value_json) VALUES ('premium', 'true')")
        connection.execute(
            """
            INSERT INTO market_prices VALUES (
                'americas', 'T4_TEST', 'Bridgewatch', 1,
                100, '2026-01-01T00:00:00+00:00', NULL, NULL,
                '2026-01-01T00:00:00+00:00', 'aodp_cached'
            )
            """
        )
        connection.commit()

    database = Database(path)
    database.initialize()
    assert MarketPriceRepository(database).count() == 1
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert (
            connection.execute("SELECT value_json FROM settings WHERE key='premium'").fetchone()[0]
            == "true"
        )


def test_newer_schema_is_rejected_without_downgrading(tmp_path) -> None:
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaVersionError, match="newer"):
        Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch) -> None:
    path = tmp_path / "rollback.db"
    _create_v2_database(path)

    def fail_after_ddl(connection) -> None:
        connection.execute("CREATE TABLE migration_must_rollback(value TEXT)")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(Database, "_migrate_v2_to_v3", staticmethod(fail_after_ddl))
    with pytest.raises(RuntimeError, match="injected"):
        Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='migration_must_rollback'"
            ).fetchone()
            is None
        )


def test_station_fee_and_crafting_profile_round_trip(tmp_path) -> None:
    database = Database(tmp_path / "profiles.db")
    database.initialize()
    now = datetime(2026, 8, 18, tzinfo=UTC)

    fees = StationFeeRepository(database)
    observation = StationFeeObservation("americas", "Thetford", StationType.WARRIOR_FORGE, 500, now)
    fees.set(observation)
    assert fees.get("americas", "Thetford", StationType.WARRIOR_FORGE) == observation
    assert fees.list_all(Region.AMERICAS) == [observation]
    assert fees.remove("americas", "Thetford", StationType.WARRIOR_FORGE)
    assert fees.get("americas", "Thetford", StationType.WARRIOR_FORGE) is None

    profile = CraftingSkillProfile(
        available_focus=12_345,
        skill_levels=(
            CraftingSkillLevel("axe:battleaxe", "axe", 82, 30),
            CraftingSkillLevel("axe:halberd", "axe", None, 30),
        ),
        manual_fce_overrides=(ManualFocusEfficiencyOverride("axe/battleaxe", 55_000, now),),
        complete_groups=frozenset({"axe"}),
        assume_zero_for_unspecified=True,
    )
    profiles = CraftingProfileRepository(database)
    profiles.save(profile)
    assert profiles.load() == profile


def test_station_fee_set_replaces_poisoned_future_row_but_not_valid_newer_row(tmp_path) -> None:
    database = Database(tmp_path / "future-station-fee.db")
    database.initialize()
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    fees = StationFeeRepository(database, wall_clock=lambda: now)
    key = ("americas", "Thetford", StationType.WARRIOR_FORGE)

    fees.set(StationFeeObservation(*key, 999, now + timedelta(days=30)))
    corrected = StationFeeObservation(*key, 500, now)
    fees.set(corrected)
    assert fees.get(*key) == corrected

    fees.set(StationFeeObservation(*key, 100, now - timedelta(hours=1)))
    assert fees.get(*key) == corrected


def test_history_cache_deduplicates_covers_and_prunes(tmp_path) -> None:
    database = Database(tmp_path / "history.db")
    database.initialize()
    repository = MarketHistoryRepository(database)
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    live = MarketHistoryInterval(
        item_id="T4_MAIN_SWORD",
        city="Bridgewatch",
        quality=1,
        region=Region.AMERICAS,
        observed_at=now - timedelta(hours=1),
        item_count=12,
        average_price=1_000,
        time_scale=HistoryTimeScale.HOURLY,
        fetched_at=now,
    )
    repository.upsert_many([live, live])
    loaded = repository.list_for_outputs(
        Region.AMERICAS,
        [live.item_id],
        [live.city],
        1,
        now - timedelta(days=1),
    )
    assert len(loaded) == 1
    assert loaded[0].provenance is Provenance.AODP_CACHED

    coverage = HistoryCoverage(
        Region.AMERICAS,
        live.item_id,
        live.city,
        1,
        HistoryTimeScale.HOURLY,
        now - timedelta(days=1),
        now,
        now,
        "success",
        1,
    )
    repository.set_coverage(coverage)
    assert repository.list_coverage(
        Region.AMERICAS,
        [live.item_id, "NEVER_FETCHED"],
        [live.city],
        1,
        HistoryTimeScale.HOURLY,
    ) == [coverage]
    assert repository.prune_before(now) == 1


def test_bulk_market_and_override_queries_are_scoped_and_chunked(tmp_path) -> None:
    database = Database(tmp_path / "bulk.db")
    database.initialize()
    observed = datetime(2026, 8, 18, tzinfo=UTC)
    prices = MarketPriceRepository(database)
    prices.upsert_many(
        [
            MarketPrice(
                item_id=f"ITEM_{index}",
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=100 + index,
                sell_price_timestamp=observed,
                buy_price=None,
                buy_price_timestamp=None,
                fetched_at=observed,
            )
            for index in range(905)
        ]
    )
    loaded = prices.list_for_scan(
        Region.AMERICAS,
        cities=["Bridgewatch"],
        qualities=[1],
        item_ids=[f"ITEM_{index}" for index in range(905)],
    )
    assert len(loaded) == 905

    overrides = PriceOverrideRepository(database)
    override = UserPriceOverride(
        "ITEM_904",
        "Bridgewatch",
        1,
        Region.AMERICAS,
        MarketSide.SELL_ORDER,
        999,
        observed,
    )
    overrides.set(override)
    assert overrides.list_for_scan(
        Region.AMERICAS,
        cities=["Bridgewatch"],
        qualities=[1],
        item_ids=["ITEM_904"],
    ) == [override]


def test_catalog_list_recipes_bulk_hydrates_in_two_queries(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "catalog-bulk.db")
    database.initialize()
    repository = CatalogRepository(database)
    material = Item("T4_BAR", "Steel Bar", 4, category="crafting")
    output = Item(
        "T4_MAIN_SWORD", "Adept's Broadsword", 4, category="weapons", crafting_category="sword"
    )
    recipe = Recipe(
        output,
        1,
        (MaterialRequirement(material.item_id, 2, True),),
        32,
        100,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="v",
    )
    repository.replace_all(
        [
            CatalogItem(material, 16, False, Provenance.STATIC_GAME_DATA, "v"),
            CatalogItem(output, 32, True, Provenance.STATIC_GAME_DATA, "v"),
        ],
        [recipe],
        CatalogImport("source", "https://example.invalid", "v", None, datetime.now(UTC), 2, 1),
    )
    statements: list[str] = []
    original_open = database._open_connection

    def open_traced():
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_open_connection", open_traced)
    assert repository.list_item_ids() == ("T4_BAR", "T4_MAIN_SWORD")
    assert repository.list_recipes(tier_min=4, tier_max=4, categories=("weapons",)) == [recipe]
    assert len([value for value in statements if value.lstrip().upper().startswith("WITH")]) == 2
