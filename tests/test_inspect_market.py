from datetime import UTC, datetime, timedelta

from albion_crafter.database.database import Database, MarketPriceRepository
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.inspect_market import inspect_market_items, main
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.models import MarketPrice, Region

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def test_market_diagnostic_exposes_raw_history_and_resolved_sides(tmp_path) -> None:
    database = Database(tmp_path / "market-diagnostic.db")
    database.initialize()
    MarketPriceRepository(database, wall_clock=lambda: NOW).upsert_many(
        (
            MarketPrice(
                item_id="T5_POTION_ACID",
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=None,
                sell_price_timestamp=None,
                buy_price=None,
                buy_price_timestamp=None,
                fetched_at=NOW,
            ),
        )
    )
    MarketHistoryRepository(database).upsert_many(
        MarketHistoryInterval(
            item_id="T5_POTION_ACID",
            city="Bridgewatch",
            quality=1,
            region=Region.AMERICAS,
            observed_at=NOW - timedelta(days=day),
            item_count=20,
            average_price=10_000,
            time_scale=HistoryTimeScale.DAILY,
            fetched_at=NOW,
        )
        for day in range(1, 8)
    )

    result = inspect_market_items(
        database,
        ("T5_POTION_ACID",),
        city="Bridgewatch",
        quality=1,
        region=Region.AMERICAS,
        as_of=NOW,
        maximum_price_age=timedelta(hours=4),
    )

    item = result["items"][0]
    assert item["current"]["sell"] is None
    assert item["history"]["days_used"] == 7
    assert item["history"]["avg_daily_volume_7d"] == 20
    assert item["resolved_sell"]["source"] == "HISTORICAL_ESTIMATE"
    assert item["resolved_sell"]["confidence"] == "HIGH"
    assert item["resolved_buy"]["source"] == "MISSING"


def test_market_diagnostic_cli_reads_cache_without_network(tmp_path, capsys) -> None:
    database_path = tmp_path / "empty-market-diagnostic.db"

    exit_code = main(
        [
            "T4_MILK",
            "--city",
            "Bridgewatch",
            "--database",
            str(database_path),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"item_id": "T4_MILK"' in output
    assert '"source": "MISSING"' in output
