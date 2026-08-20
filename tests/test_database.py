import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from albion_crafter.core.provenance import Provenance
from albion_crafter.data.sample_data import sample_market_prices
from albion_crafter.database.database import Database, MarketPriceRepository, SettingsRepository
from albion_crafter.market.models import MarketPrice, Region


def test_database_connection_context_closes_connection(tmp_path) -> None:
    database = Database(tmp_path / "closed.db")
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_sqlite_market_cache_and_settings_round_trip(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    database.initialize()
    prices = MarketPriceRepository(database)
    observed = datetime(2026, 1, 1, 12, tzinfo=UTC)
    prices.upsert_many(
        [
            MarketPrice(
                item_id="T4_TEST",
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=100,
                sell_price_timestamp=observed,
                buy_price=None,
                buy_price_timestamp=None,
                fetched_at=observed,
            )
        ]
    )
    loaded = prices.get("T4_TEST", "Bridgewatch", 1, Region.AMERICAS)
    assert loaded is not None
    assert loaded.sell_price == 100
    assert loaded.buy_price is None
    assert loaded.sell_price_timestamp == observed

    settings = SettingsRepository(database)
    settings.set_many({"premium": True, "max_age": 4})
    assert settings.get("premium") is True
    assert settings.get("max_age") == 4


def _price(
    *,
    sell: int | None,
    sell_at: datetime | None,
    buy: int | None,
    buy_at: datetime | None,
    fetched_at: datetime,
) -> MarketPrice:
    return MarketPrice(
        item_id="ITEM",
        city="Bridgewatch",
        quality=1,
        region=Region.AMERICAS,
        sell_price=sell,
        sell_price_timestamp=sell_at,
        buy_price=buy,
        buy_price_timestamp=buy_at,
        fetched_at=fetched_at,
    )


def test_cache_merges_buy_and_sell_sides_by_their_own_observation_times(tmp_path) -> None:
    database = Database(tmp_path / "merge.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    t1 = datetime(2026, 1, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, 2, tzinfo=UTC)
    t3 = datetime(2026, 1, 1, 3, tzinfo=UTC)
    t4 = datetime(2026, 1, 1, 4, tzinfo=UTC)
    repository.upsert_many([_price(sell=100, sell_at=t2, buy=90, buy_at=t2, fetched_at=t2)])

    # Newer sell wins while an older buy cannot destroy the useful buy side.
    repository.upsert_many([_price(sell=110, sell_at=t3, buy=80, buy_at=t1, fetched_at=t3)])
    loaded = repository.get("ITEM", "Bridgewatch", 1, Region.AMERICAS)
    assert loaded is not None
    assert (loaded.sell_price, loaded.sell_price_timestamp) == (110, t3)
    assert (loaded.buy_price, loaded.buy_price_timestamp) == (90, t2)

    # Missing sell preserves it; newer buy is merged independently.
    repository.upsert_many([_price(sell=None, sell_at=None, buy=95, buy_at=t3, fetched_at=t4)])
    loaded = repository.get("ITEM", "Bridgewatch", 1, Region.AMERICAS)
    assert loaded is not None
    assert (loaded.sell_price, loaded.sell_price_timestamp) == (110, t3)
    assert (loaded.buy_price, loaded.buy_price_timestamp) == (95, t3)
    assert loaded.fetched_at == t4

    # The inverse case is also side-specific: older sell loses, newer buy wins.
    repository.upsert_many([_price(sell=105, sell_at=t2, buy=100, buy_at=t4, fetched_at=t4)])
    loaded = repository.get("ITEM", "Bridgewatch", 1, Region.AMERICAS)
    assert loaded is not None
    assert (loaded.sell_price, loaded.sell_price_timestamp) == (110, t3)
    assert (loaded.buy_price, loaded.buy_price_timestamp) == (100, t4)

    # An entirely older observation does not become current merely because it was fetched later.
    repository.upsert_many([_price(sell=1, sell_at=t1, buy=None, buy_at=None, fetched_at=t4)])
    loaded = repository.get("ITEM", "Bridgewatch", 1, Region.AMERICAS)
    assert loaded is not None
    assert (loaded.sell_price, loaded.sell_price_timestamp) == (110, t3)
    assert loaded.fetched_at == t4


def test_complete_cache_miss_preserves_missing_sides(tmp_path) -> None:
    database = Database(tmp_path / "missing.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repository.upsert_many([_price(sell=None, sell_at=None, buy=90, buy_at=now, fetched_at=now)])
    loaded = repository.get("ITEM", "Bridgewatch", 1, Region.AMERICAS)
    assert loaded is not None
    assert loaded.sell_price is None
    assert loaded.sell_price_timestamp is None
    assert loaded.buy_price == 90


def test_production_cache_rejects_demo_and_test_fixture_prices(tmp_path) -> None:
    database = Database(tmp_path / "production.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    with pytest.raises(ValueError, match="AODP observations"):
        repository.upsert_many(sample_market_prices())
    assert repository.count() == 0


def test_market_display_query_is_region_scoped_bounded_and_prioritizes_prices(tmp_path) -> None:
    database = Database(tmp_path / "market-display.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 2, tzinfo=UTC)
    repository.upsert_many(
        [
            MarketPrice(
                "PRICED",
                "Bridgewatch",
                1,
                Region.AMERICAS,
                100,
                older,
                None,
                None,
                older,
            ),
            MarketPrice(
                "EMPTY",
                "Bridgewatch",
                1,
                Region.AMERICAS,
                None,
                None,
                None,
                None,
                newer,
            ),
            MarketPrice(
                "EUROPE",
                "Bridgewatch",
                1,
                Region.EUROPE,
                200,
                newer,
                None,
                None,
                newer,
            ),
        ]
    )

    assert repository.count(Region.AMERICAS) == 2
    assert repository.count(Region.EUROPE) == 1
    assert [row.item_id for row in repository.list_for_display(Region.AMERICAS, limit=1)] == [
        "PRICED"
    ]
    assert repository.list_for_display(Region.AMERICAS, limit=0) == []


def test_legacy_migration_drops_samples_and_keeps_only_aodp(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE market_prices (
                region TEXT, item_id TEXT, city TEXT, quality INTEGER,
                sell_price INTEGER, sell_price_timestamp TEXT,
                buy_price INTEGER, buy_price_timestamp TEXT,
                fetched_at TEXT, source TEXT,
                PRIMARY KEY(region, item_id, city, quality)
            );
            INSERT INTO market_prices VALUES
              ('americas', 'SAMPLE', 'Bridgewatch', 1, 100, NULL, 90, NULL,
               '2026-01-01T00:00:00+00:00', 'sample-estimate'),
              ('americas', 'LIVE', 'Bridgewatch', 1, 200,
               '2026-01-01T00:00:00+00:00', 190,
               '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'aodp');
            """
        )
        connection.commit()

    database = Database(path)
    database.initialize()
    repository = MarketPriceRepository(database)
    assert repository.get("SAMPLE", "Bridgewatch", 1, Region.AMERICAS) is None
    live = repository.get("LIVE", "Bridgewatch", 1, Region.AMERICAS)
    assert live is not None
    assert live.provenance is Provenance.AODP_CACHED
