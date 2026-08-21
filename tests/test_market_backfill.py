import json
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlparse

from albion_crafter.database.database import Database, MarketPriceRepository
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.market.backfill import MissingSellHistoryBackfillService
from albion_crafter.market.history import AODPHistoryClient, HistoryTimeScale
from albion_crafter.market.history_cache import CachedOutputHistoryService
from albion_crafter.market.models import FreshnessPolicy, MarketPrice, MarketSide, Region
from albion_crafter.market.pricing import resolve_price

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def test_backfill_batches_only_missing_current_sells_and_resolves_history(tmp_path) -> None:
    database = Database(tmp_path / "backfill.db")
    database.initialize()
    market = MarketPriceRepository(database, wall_clock=lambda: NOW)
    history = MarketHistoryRepository(database)
    market.upsert_many(
        (
            MarketPrice(
                "CURRENT_ITEM",
                "Bridgewatch",
                1,
                Region.AMERICAS,
                123,
                NOW - timedelta(days=10),
                None,
                None,
                NOW,
            ),
            MarketPrice(
                "MISSING_ITEM",
                "Bridgewatch",
                1,
                Region.AMERICAS,
                None,
                None,
                None,
                None,
                NOW,
            ),
        )
    )
    requested: list[tuple[str, ...]] = []

    def transport(url: str, _timeout: float) -> bytes:
        path = urlparse(url).path.partition("/history/")[2].removesuffix(".json")
        item_ids = tuple(unquote(value) for value in path.split(","))
        requested.append(item_ids)
        return json.dumps(
            [
                {
                    "item_id": item_id,
                    "location": "Bridgewatch",
                    "quality": 1,
                    "data": [
                        {
                            "item_count": 20,
                            "avg_price": 1_000 + day,
                            "timestamp": (NOW - timedelta(days=day)).isoformat(),
                        }
                        for day in range(1, 8)
                    ],
                }
                for item_id in item_ids
            ]
        ).encode()

    service = MissingSellHistoryBackfillService(
        market,
        history,
        client_factory=lambda region: AODPHistoryClient(
            region,
            transport=transport,
            wall_clock=lambda: NOW,
            retry_backoff_seconds=0,
        ),
        cache_service_factory=lambda client, repository: CachedOutputHistoryService(
            client,
            repository,
            wall_clock=lambda: NOW,
        ),
    )

    result = service.refresh_missing(
        Region.AMERICAS,
        ("CURRENT_ITEM", "MISSING_ITEM"),
        ("Bridgewatch",),
        as_of=NOW,
    )

    assert requested == [("MISSING_ITEM",)]
    assert result.requested_keys == (("MISSING_ITEM", "Bridgewatch", 1),)
    assert result.resolved_keys == (("MISSING_ITEM", "Bridgewatch", 1),)
    assert result.unresolved_keys == ()
    intervals = history.list_for_items(
        Region.AMERICAS,
        ("MISSING_ITEM",),
        ("Bridgewatch",),
        1,
        NOW - timedelta(days=30),
        time_scale=HistoryTimeScale.DAILY,
    )
    line = resolve_price(
        item_id="MISSING_ITEM",
        city="Bridgewatch",
        quality=1,
        side=MarketSide.SELL_ORDER,
        role="test",
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
        market_price=market.get("MISSING_ITEM", "Bridgewatch", 1, Region.AMERICAS),
        history=intervals,
    )
    assert line.price is not None
    assert line.source.value == "HISTORICAL_ESTIMATE"


def test_empty_history_is_unresolved_upstream_absence_not_an_error(tmp_path) -> None:
    database = Database(tmp_path / "empty-backfill.db")
    database.initialize()
    market = MarketPriceRepository(database, wall_clock=lambda: NOW)
    history = MarketHistoryRepository(database)
    service = MissingSellHistoryBackfillService(
        market,
        history,
        client_factory=lambda region: AODPHistoryClient(
            region,
            transport=lambda _url, _timeout: b"[]",
            wall_clock=lambda: NOW,
            retry_backoff_seconds=0,
        ),
        cache_service_factory=lambda client, repository: CachedOutputHistoryService(
            client,
            repository,
            wall_clock=lambda: NOW,
        ),
    )

    result = service.refresh_missing(
        Region.AMERICAS,
        ("NEVER_TRADED",),
        ("Bridgewatch",),
        as_of=NOW,
    )

    assert result.resolved_count == 0
    assert result.unresolved_keys == (("NEVER_TRADED", "Bridgewatch", 1),)
    assert not result.has_errors
