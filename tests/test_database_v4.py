from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.models import ActionKind, SaleMethod
from albion_crafter.database.database import LATEST_SCHEMA_VERSION, Database, SchemaVersionError
from albion_crafter.database.v4 import (
    FIND_MONEY_PREFERENCES_KEY,
    FindMoneyPreferencesError,
    FindMoneyPreferencesRepository,
    PlanSnapshotAlreadyExists,
    PlanSnapshotIntegrityError,
    PlanSnapshotRepository,
    UnsupportedPlanSnapshotVersion,
)
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning.models import (
    ArbitrageScope,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationDiagnostics,
    OptimizationStatus,
    PlanAction,
    PlanDataHealth,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanSnapshot,
    PlanStatus,
    RefreshStatistics,
    TransportPolicy,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _create_v2(path) -> None:
    database = Database(path)
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        database._create_v2_schema(connection)
        connection.execute("PRAGMA user_version = 2")


def _create_v3(path) -> None:
    _create_v2(path)
    database = Database(path)
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        database._migrate_v2_to_v3(connection)
        connection.execute("PRAGMA user_version = 3")


def _insert_v2_rows(path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO settings VALUES ('premium', 'true')")
        connection.execute(
            """INSERT INTO market_prices VALUES (
                'americas', 'T4_MAIN_SWORD', 'Bridgewatch', 1,
                1000, '2026-08-18T11:00:00+00:00',
                900, '2026-08-18T10:00:00+00:00',
                '2026-08-18T11:01:00+00:00', 'aodp_cached'
            )"""
        )
        connection.execute(
            """INSERT INTO price_overrides VALUES (
                'americas', 'T4_MAIN_SWORD', 'Bridgewatch', 1, 'sell_order',
                1100, '2026-08-18T11:30:00+00:00', 'user_override'
            )"""
        )
        connection.execute(
            """INSERT INTO catalog_items VALUES (
                'T4_MAIN_SWORD', 'Adept Sword', 4, 0, 'weapon', '', 'sword',
                5, 32, 1, 'static_game_data', 'catalog-v3'
            )"""
        )
        connection.execute(
            """INSERT INTO catalog_recipes VALUES (
                'T4_MAIN_SWORD', 1, 100, 0, 'static_game_data', 'catalog-v3'
            )"""
        )
        connection.execute(
            "INSERT INTO catalog_materials VALUES ('T4_MAIN_SWORD', 0, 'T4_BAR', 16, 1)"
        )
        connection.execute(
            """INSERT INTO catalog_imports VALUES (
                'source', 'https://example.invalid', 'catalog-v3', NULL,
                '2026-08-18T11:00:00+00:00', 1, 1
            )"""
        )
        connection.commit()


def _insert_v3_rows(path) -> None:
    _insert_v2_rows(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """INSERT INTO station_fees VALUES (
                'americas', 'Bridgewatch', 'warrior_forge', 500,
                '2026-08-18T09:00:00+00:00', 'user_override'
            )"""
        )
        connection.execute(
            """INSERT INTO crafting_profiles VALUES (
                'default', 'Default', 10000, '["sword"]', 0,
                '2026-08-18T09:00:00+00:00', 'user_profile'
            )"""
        )
        connection.execute(
            """INSERT INTO crafting_skill_levels VALUES (
                'default', 'sword:main_sword', 'sword', 42, 30,
                '2026-08-18T09:00:00+00:00', 'user_profile'
            )"""
        )
        connection.execute(
            """INSERT INTO focus_efficiency_overrides VALUES (
                'default', 'sword/main_sword', 12345,
                '2026-08-18T09:00:00+00:00', 'user_override'
            )"""
        )
        connection.execute(
            """INSERT INTO market_history_intervals VALUES (
                'americas', 'T4_MAIN_SWORD', 'Bridgewatch', 1, 6,
                '2026-08-18T06:00:00+00:00', 12, 1000, 900, 1100,
                '2026-08-18T09:00:00+00:00', 'aodp_cached'
            )"""
        )
        connection.execute(
            """INSERT INTO market_history_coverage VALUES (
                'americas', 'T4_MAIN_SWORD', 'Bridgewatch', 1, 6,
                '2026-08-11T00:00:00+00:00', '2026-08-18T09:00:00+00:00',
                '2026-08-18T09:01:00+00:00', 'success', 1, NULL
            )"""
        )
        connection.execute(
            """INSERT INTO catalog_import_runs (
                source_id, source_url, source_version, source_timestamp,
                started_at, finished_at, raw_sha256, formatted_sha256,
                previous_item_count, previous_recipe_count, item_count, recipe_count,
                ingredient_count, unknown_returnability_count, skipped_malformed_count,
                validation_status, validation_messages_json, forced, activated
            ) VALUES (
                'source', 'https://example.invalid', 'catalog-v3', NULL,
                '2026-08-18T08:00:00+00:00', '2026-08-18T08:01:00+00:00',
                NULL, NULL, 0, 0, 1, 1, 1, 0, 0, 'passed_v3', '[]', 0, 1
            )"""
        )
        connection.commit()


def _table_digest(path, tables: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path)) as connection:
        for table in tables:
            digest.update(table.encode())
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):  # noqa: S608
                digest.update(repr(tuple(row)).encode())
                digest.update(b"\n")
    return digest.hexdigest()


def _snapshot(snapshot_id: str = "plan-1", *, created_at: datetime = NOW) -> PlanSnapshot:
    constraints = FindMoneyConstraints(
        available_silver=1_000_000,
        available_focus=10_000,
        silver_reserve=100_000,
        focus_reserve=1_000,
        material_cities=("Bridgewatch",),
        craft_cities=("Bridgewatch",),
        sell_cities=("Bridgewatch",),
    )
    route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Bridgewatch",
        TransportPolicy.LOCAL_ONLY,
    )
    action = PlanAction(
        candidate_id="candidate-1",
        item_id="T4_MAIN_SWORD",
        display_name="Adept Sword",
        route=route,
        quantity=2,
        focused_quantity=1,
        nonfocused_quantity=1,
        output_units=2,
        quality=1,
        sale_method=SaleMethod.SELL_ORDER,
        pre_revenue_cash_required=200_000,
        focus_required=800,
        expected_profit=30_000,
        liquidity=LiquidityLevel.MODERATE,
        execution_capacity_key=(
            Region.AMERICAS,
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
        ),
        quantity_ceiling=4,
        execution_ceiling_output_units=4,
        expected_revenue=260_000,
        effective_economic_cost=230_000,
        reasons=(
            PlanReason(
                PlanReasonCode.UNMODELED_TRANSPORT,
                "No transport is required.",
                PlanReasonSeverity.INFO,
            ),
        ),
        evidence=(("materials", '[{"item_id":"T4_BAR","quantity":32}]'),),
        oldest_market_observed_at=created_at - timedelta(hours=1),
        station_fee_observed_at=created_at - timedelta(hours=2),
    )
    return PlanSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        completed_at=created_at + timedelta(seconds=2),
        region=Region.AMERICAS,
        constraints=constraints,
        actions=(action,),
        total_pre_revenue_cash=200_000,
        total_focus=800,
        total_expected_profit=30_000,
        silver_remaining=700_000,
        focus_remaining=8_200,
        plan_status=PlanStatus.DECISION_GRADE,
        reasons=(),
        optimizer=OptimizationDiagnostics(
            "pareto-frontier-v1",
            OptimizationStatus.EXACT,
            candidate_count=1,
            group_count=1,
            quantity_decision_count=4,
            states_considered=8,
            states_pruned=3,
            state_limit=50_000,
            state_limit_reached=False,
            elapsed_seconds=0.01,
        ),
        catalog_source_version="catalog-v3",
        mechanics_ruleset_id="rules-v4",
        assumptions=("Normal quality only",),
        data_health=PlanDataHealth(
            market_observations_used=3,
            market_fresh=3,
            station_fees_used=1,
            station_fees_fresh=1,
            mechanics_status="verified",
        ),
        current_refresh=RefreshStatistics(3, 1, 1, 0, 3, 0.1),
        history_refresh=RefreshStatistics(1, 1, 1, 0, 4, 0.2),
        oldest_market_observed_at=action.oldest_market_observed_at,
        oldest_station_observed_at=action.station_fee_observed_at,
        metadata=(("test", "fixture"),),
    )


def test_fresh_database_initializes_directly_to_v4_with_snapshot_index(tmp_path) -> None:
    database = Database(tmp_path / "fresh.db")
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_snapshots'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='plan_snapshots_recent'"
        ).fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_list(plan_snapshots)").fetchall()
    assert foreign_keys == []


def test_v2_to_v4_preserves_every_existing_v2_table(tmp_path) -> None:
    path = tmp_path / "v2.db"
    _create_v2(path)
    _insert_v2_rows(path)
    tables = tuple(Database._v2_required_columns())
    before = _table_digest(path, tables)

    Database(path).initialize()

    assert _table_digest(path, tables) == before
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v3_to_v4_preserves_every_existing_v3_table_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "v3.db"
    _create_v3(path)
    _insert_v3_rows(path)
    tables = tuple(Database._v3_required_columns())
    before = _table_digest(path, tables)

    database = Database(path)
    database.initialize()
    database.initialize()

    assert _table_digest(path, tables) == before
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v3_migration_failure_rolls_back_ddl_and_version(tmp_path, monkeypatch) -> None:
    path = tmp_path / "rollback.db"
    _create_v3(path)

    def fail_after_ddl(connection) -> None:
        connection.execute("CREATE TABLE migration_v4_must_rollback(value TEXT)")
        raise RuntimeError("injected V4 migration failure")

    monkeypatch.setattr(Database, "_migrate_v3_to_v4", staticmethod(fail_after_ddl))
    with pytest.raises(RuntimeError, match="injected V4"):
        Database(path).initialize()

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='migration_v4_must_rollback'"
            ).fetchone()
            is None
        )


def test_damaged_preserved_v3_schema_is_rejected_before_v4_ddl(tmp_path) -> None:
    path = tmp_path / "damaged-v3.db"
    _create_v3(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE price_overrides")
        connection.commit()

    with pytest.raises(SchemaVersionError, match="version 3.*price_overrides"):
        Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='plan_snapshots'").fetchone()
            is None
        )


def test_v3_schema_with_all_column_names_but_missing_primary_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "damaged-primary-key.db"
    _create_v3(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO settings VALUES ('preserved', 'true')")
        connection.execute("ALTER TABLE settings RENAME TO settings_with_key")
        connection.execute("CREATE TABLE settings (key TEXT NOT NULL, value_json TEXT NOT NULL)")
        connection.execute("INSERT INTO settings SELECT * FROM settings_with_key")
        connection.execute("DROP TABLE settings_with_key")
        connection.commit()

    with pytest.raises(SchemaVersionError, match="incompatible definitions.*table settings"):
        Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT * FROM settings").fetchone() == (
            "preserved",
            "true",
        )


def test_v3_schema_with_missing_required_index_is_rejected(tmp_path) -> None:
    path = tmp_path / "damaged-v3-index.db"
    _create_v3(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP INDEX catalog_materials_item")
        connection.commit()

    with pytest.raises(
        SchemaVersionError,
        match="incompatible definitions.*index catalog_materials_item",
    ):
        Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='plan_snapshots'").fetchone()
            is None
        )


def test_existing_v4_schema_with_missing_snapshot_index_is_rejected(tmp_path) -> None:
    path = tmp_path / "damaged-v4-index.db"
    database = Database(path)
    database.initialize()
    with database.connection() as connection:
        connection.execute("DROP INDEX plan_snapshots_recent")

    with pytest.raises(
        SchemaVersionError,
        match="incompatible definitions.*index plan_snapshots_recent",
    ):
        database.initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_newer_schema_remains_rejected_without_mutation(tmp_path) -> None:
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 99")
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SchemaVersionError, match="newer"):
        Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_snapshot_round_trip_summary_immutability_and_remove(tmp_path) -> None:
    database = Database(tmp_path / "plans.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    snapshot = _snapshot()

    assert repository.save(snapshot) == snapshot
    assert repository.load(snapshot.snapshot_id) == snapshot
    assert repository.list_recent() == [snapshot]
    summary = repository.list_summaries()[0]
    assert summary.snapshot_id == snapshot.snapshot_id
    assert summary.action_count == 1
    assert summary.optimization_status is OptimizationStatus.EXACT
    with pytest.raises(PlanSnapshotAlreadyExists):
        repository.save(replace(snapshot, total_expected_profit=999_999))
    assert repository.remove(snapshot.snapshot_id)
    assert repository.load(snapshot.snapshot_id) is None
    assert not repository.remove(snapshot.snapshot_id)


def test_snapshot_retention_is_bounded_and_deterministic_for_timestamp_ties(tmp_path) -> None:
    database = Database(tmp_path / "retention.db")
    database.initialize()
    repository = PlanSnapshotRepository(database, retention_limit=3)
    for snapshot_id in ("plan-a", "plan-d", "plan-b", "plan-c"):
        repository.save(_snapshot(snapshot_id, created_at=NOW))

    assert repository.count() == 3
    assert [value.snapshot_id for value in repository.list_recent()] == [
        "plan-d",
        "plan-c",
        "plan-b",
    ]


def test_snapshot_default_retention_keeps_exactly_the_newest_twenty(tmp_path) -> None:
    database = Database(tmp_path / "default-retention.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    for number in range(24):
        repository.save(
            _snapshot(
                f"plan-{number:02d}",
                created_at=NOW + timedelta(seconds=number),
            )
        )

    assert repository.count() == 20
    assert [value.snapshot_id for value in repository.list_summaries()] == [
        f"plan-{number:02d}" for number in range(23, 3, -1)
    ]


def test_snapshot_load_rejects_hash_corruption_and_index_mismatch(tmp_path) -> None:
    database = Database(tmp_path / "corrupt.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    repository.save(_snapshot())
    with database.connection() as connection:
        connection.execute(
            "UPDATE plan_snapshots SET payload_json=payload_json || ' ' WHERE snapshot_id='plan-1'"
        )
    with pytest.raises(PlanSnapshotIntegrityError, match="SHA-256"):
        repository.load("plan-1")
    with pytest.raises(PlanSnapshotIntegrityError, match="SHA-256"):
        repository.list_summaries()

    database = Database(tmp_path / "metadata.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    repository.save(_snapshot())
    with database.connection() as connection:
        connection.execute(
            "UPDATE plan_snapshots SET total_expected_profit=123 WHERE snapshot_id='plan-1'"
        )
    with pytest.raises(PlanSnapshotIntegrityError, match="metadata"):
        repository.load("plan-1")


def test_snapshot_load_wraps_malformed_indexed_timestamps_as_integrity_errors(tmp_path) -> None:
    database = Database(tmp_path / "malformed-index.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    repository.save(_snapshot())
    with database.connection() as connection:
        connection.execute(
            "UPDATE plan_snapshots SET created_at='not-a-timestamp' WHERE snapshot_id='plan-1'"
        )

    with pytest.raises(PlanSnapshotIntegrityError, match="indexed metadata is invalid"):
        repository.load("plan-1")


def test_snapshot_load_rejects_rehashed_noncanonical_type_coercion(tmp_path) -> None:
    database = Database(tmp_path / "noncanonical.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    repository.save(_snapshot())
    with database.connection() as connection:
        row = connection.execute(
            "SELECT payload_json FROM plan_snapshots WHERE snapshot_id='plan-1'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["actions"][0]["quantity"] = "2"
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE plan_snapshots SET payload_json=?, payload_sha256=? WHERE snapshot_id='plan-1'",
            (serialized, hashlib.sha256(serialized.encode()).hexdigest()),
        )

    with pytest.raises(PlanSnapshotIntegrityError, match="canonical serialized form"):
        repository.load("plan-1")


def test_snapshot_load_rejects_future_payload_version_explicitly(tmp_path) -> None:
    database = Database(tmp_path / "future-plan.db")
    database.initialize()
    repository = PlanSnapshotRepository(database)
    repository.save(_snapshot())
    with database.connection() as connection:
        row = connection.execute(
            "SELECT payload_json FROM plan_snapshots WHERE snapshot_id='plan-1'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["snapshot_format_version"] = 99
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """UPDATE plan_snapshots
               SET snapshot_format_version=99, payload_json=?, payload_sha256=?
               WHERE snapshot_id='plan-1'""",
            (serialized, hashlib.sha256(serialized.encode()).hexdigest()),
        )
    with pytest.raises(UnsupportedPlanSnapshotVersion, match="version 99"):
        repository.load("plan-1")


def test_find_money_preferences_are_typed_namespaced_and_validated(tmp_path) -> None:
    database = Database(tmp_path / "preferences.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    constraints = _snapshot().constraints
    assert preferences.load() is None
    assert preferences.save(constraints) == constraints
    assert preferences.load() == constraints
    with database.connection() as connection:
        connection.execute("INSERT INTO settings VALUES ('unrelated', '42')")
        stored = connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (FIND_MONEY_PREFERENCES_KEY,),
        ).fetchone()[0]
        assert json.loads(stored)["format_version"] == 3
        assert (
            connection.execute("SELECT value_json FROM settings WHERE key='unrelated'").fetchone()[
                0
            ]
            == "42"
        )

        malformed = json.loads(stored)
        malformed["constraints"]["premium"] = "false"
        connection.execute(
            "UPDATE settings SET value_json=? WHERE key=?",
            (json.dumps(malformed), FIND_MONEY_PREFERENCES_KEY),
        )
    with pytest.raises(FindMoneyPreferencesError, match="premium.*boolean"):
        preferences.load()


def test_v3_preferences_round_trip_arbitrage_scope_and_cities(tmp_path) -> None:
    database = Database(tmp_path / "arbitrage-preferences.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    constraints = replace(
        _snapshot().constraints,
        action_kinds=frozenset({ActionKind.CRAFT, ActionKind.ARBITRAGE}),
        arbitrage_scope=ArbitrageScope.CRAFTED_OUTPUTS,
        arbitrage_source_cities=("Bridgewatch", "Lymhurst"),
        arbitrage_destination_cities=("Martlock", "Thetford"),
        transport_policy=TransportPolicy.EXPLICIT_COST,
        transport_cost_per_craft=42,
    )

    preferences.save(constraints)

    assert preferences.load() == constraints
    payload = preferences.settings.get(FIND_MONEY_PREFERENCES_KEY)
    assert payload["format_version"] == 3
    assert payload["constraints"]["action_kinds"] == ["arbitrage", "craft"]
    assert payload["constraints"]["arbitrage_scope"] == "crafted_outputs"
    assert payload["constraints"]["arbitrage_source_cities"] == [
        "Bridgewatch",
        "Lymhurst",
    ]
    assert payload["constraints"]["arbitrage_destination_cities"] == [
        "Martlock",
        "Thetford",
    ]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        ("available_silver", "1000000", "available_silver.*integer"),
        ("tiers", [4, "5"], "tiers.*only integers"),
        ("item_query", 42, "item_query.*string"),
        ("max_market_age_seconds", 1e308, "preferences are invalid"),
        ("unexpected_v04_field", 1, "unknown fields.*unexpected_v04_field"),
    ),
)
def test_find_money_preferences_reject_type_coercion_and_unknown_fields(
    tmp_path,
    key,
    value,
    message,
) -> None:
    database = Database(tmp_path / "malformed-preferences.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    constraints = _snapshot().constraints.to_dict()
    constraints[key] = value
    preferences.settings.set(
        FIND_MONEY_PREFERENCES_KEY,
        {"format_version": 3, "constraints": constraints},
    )

    with pytest.raises(FindMoneyPreferencesError, match=message):
        preferences.load()


def test_find_money_preferences_reject_future_format(tmp_path) -> None:
    database = Database(tmp_path / "future-preferences.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    preferences.settings.set(
        FIND_MONEY_PREFERENCES_KEY,
        {"format_version": 99, "constraints": _snapshot().constraints.to_dict()},
    )

    with pytest.raises(FindMoneyPreferencesError, match="unsupported.*version 99"):
        preferences.load()


def test_find_money_preferences_distinguish_missing_from_stored_null(tmp_path) -> None:
    database = Database(tmp_path / "null-preferences.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    default = _snapshot().constraints
    assert preferences.load(default) == default

    preferences.settings.set(FIND_MONEY_PREFERENCES_KEY, None)
    with pytest.raises(FindMoneyPreferencesError, match="must be a JSON object"):
        preferences.load(default)


def test_schema_version_constant_is_v4() -> None:
    assert LATEST_SCHEMA_VERSION == 4
