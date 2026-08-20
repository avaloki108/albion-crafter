from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
)
from albion_crafter.market.aodp import SAFE_AODP_URL_LENGTH, AODPClient, plan_price_requests
from albion_crafter.market.coverage import MarketCoverageService
from albion_crafter.market.models import MarketPrice, MarketSide, Region, UserPriceOverride
from albion_crafter.market.sync import (
    DEFAULT_ROYAL_SYNC_CITIES,
    MarketSyncStateRepository,
    RoyalMarketSyncService,
    RoyalMarketUniverseService,
)

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _database(tmp_path, name: str = "market-sync.db") -> Database:
    database = Database(tmp_path / name)
    database.initialize()
    return database


def _catalog(repository: CatalogRepository, *, version: str = "one", extra=False) -> None:
    items = [
        Item("T4_MAIN_SWORD", "Adept's Broadsword", 4, crafting_category="sword"),
        Item("T4_METALBAR", "Adept's Metal Bar", 4, crafting_category="ore"),
        Item("T3_ORE", "Journeyman's Ore", 3),
        Item("T4_BAG@1", "Adept's Bag .1", 4, 1, crafting_category="bag"),
        Item("UNSUPPORTED_OUTPUT", "Unsupported", 4, crafting_category="mystery"),
        Item("NOISE", "Non-production noise", None),
    ]
    if extra:
        items.append(Item("T5_MAIN_SWORD", "Expert's Broadsword", 5, crafting_category="sword"))
    item_values = {
        "T4_MAIN_SWORD": 100,
        "T4_METALBAR": 20,
        "T3_ORE": 5,
        "T4_BAG@1": 80,
        "UNSUPPORTED_OUTPUT": 1,
        "NOISE": None,
        "T5_MAIN_SWORD": 200,
    }
    recipes = [
        Recipe(
            items[0],
            1,
            (MaterialRequirement("T4_METALBAR", 16, True),),
            item_value=100,
            provenance=Provenance.STATIC_GAME_DATA,
            source_version=version,
        ),
        Recipe(
            items[1],
            1,
            (MaterialRequirement("T3_ORE", 2, True),),
            item_value=20,
            provenance=Provenance.STATIC_GAME_DATA,
            source_version=version,
        ),
        Recipe(
            items[3],
            1,
            (MaterialRequirement("T4_METALBAR", 8, True),),
            item_value=80,
            provenance=Provenance.STATIC_GAME_DATA,
            source_version=version,
        ),
        Recipe(
            items[4],
            1,
            (MaterialRequirement("T3_ORE", 1, True),),
            item_value=1,
            provenance=Provenance.STATIC_GAME_DATA,
            source_version=version,
        ),
    ]
    if extra:
        recipes.append(
            Recipe(
                items[-1],
                1,
                (MaterialRequirement("T4_METALBAR", 32, True),),
                item_value=200,
                provenance=Provenance.STATIC_GAME_DATA,
                source_version=version,
            )
        )
    repository.replace_all(
        [
            CatalogItem(
                item,
                item_values[item.item_id],
                any(recipe.output.item_id == item.item_id for recipe in recipes),
                Provenance.STATIC_GAME_DATA,
                version,
            )
            for item in items
        ],
        recipes,
        CatalogImport(
            "test",
            "https://example.invalid/static.json",
            version,
            None,
            NOW,
            len(items),
            len(recipes),
        ),
    )


def _url_scope(url: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    parsed = urlparse(url)
    item_path = parsed.path.partition("/prices/")[2].removesuffix(".json")
    items = tuple(unquote(value) for value in item_path.split(","))
    cities = tuple(parse_qs(parsed.query)["locations"][0].split(","))
    return items, cities


def _payload(url: str, *, observed_at: datetime = NOW - timedelta(minutes=30)) -> bytes:
    item_ids, cities = _url_scope(url)
    return json.dumps(
        [
            {
                "item_id": item_id,
                "city": city,
                "quality": 1,
                "sell_price_min": 100,
                "sell_price_min_date": observed_at.isoformat(),
                "buy_price_max": 90,
                "buy_price_max_date": observed_at.isoformat(),
            }
            for item_id in item_ids
            for city in cities
        ]
    ).encode()


def test_market_universe_includes_supported_outputs_and_inputs_only(tmp_path) -> None:
    database = _database(tmp_path)
    catalog = CatalogRepository(database)
    _catalog(catalog)
    service = RoyalMarketUniverseService(catalog)

    universe = service.derive()

    assert universe.item_ids == ("T3_ORE", "T4_BAG@1", "T4_MAIN_SWORD", "T4_METALBAR")
    assert universe.supported_output_items == 3
    assert universe.required_ingredient_items == 2
    assert "UNSUPPORTED_OUTPUT" not in universe.item_ids
    assert "NOISE" not in universe.item_ids
    metal = next(item for item in universe.items if item.item.item_id == "T4_METALBAR")
    assert metal.reasons == (
        "Arbitrage output",
        "Crafting ingredient",
        "Refining output",
    )

    _catalog(catalog, version="two", extra=True)
    assert "T5_MAIN_SWORD" in service.derive().item_ids


def test_request_plan_is_complete_deterministic_and_bounded_for_five_cities() -> None:
    item_ids = tuple(f"T8_LONG_ITEM_{index:03d}@3_" + "X" * 90 for index in range(75))
    first = plan_price_requests(item_ids, cities=DEFAULT_ROYAL_SYNC_CITIES)
    second = plan_price_requests(item_ids, cities=DEFAULT_ROYAL_SYNC_CITIES)

    assert first == second
    assert first.cities == DEFAULT_ROYAL_SYNC_CITIES
    assert all(batch.item_ids for batch in first.batches)
    assert tuple(item for batch in first.batches for item in batch.item_ids) == item_ids
    assert all(batch.url_bytes <= SAFE_AODP_URL_LENGTH for batch in first.batches)
    assert all("locations=Bridgewatch%2CFort+Sterling" in batch.url for batch in first.batches)


def test_full_sync_persists_every_successful_batch_and_deterministic_statistics(tmp_path) -> None:
    database = _database(tmp_path)
    catalog = CatalogRepository(database)
    _catalog(catalog)
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    calls: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return _payload(url)

    def client_factory(
        region: Region,
        *,
        batch_size: int,
        max_url_length: int,
        max_batches: int,
    ) -> AODPClient:
        return AODPClient(
            region,
            batch_size=batch_size,
            max_url_length=max_url_length,
            max_batches=max_batches,
            transport=transport,
            wall_clock=lambda: NOW,
        )

    progress = []
    clocks = iter((10.0, 12.5))
    service = RoyalMarketSyncService(
        RoyalMarketUniverseService(catalog),
        prices,
        client_factory=client_factory,
        batch_size=2,
        clock=lambda: next(clocks),
        wall_clock=lambda: NOW,
    )
    result = service.synchronize(
        Region.AMERICAS,
        ("Bridgewatch", "Martlock"),
        on_progress=progress.append,
    )

    assert result.status == "complete"
    assert result.planned_batches == result.completed_batches == result.successful_batches == 2
    assert result.rows_returned == 8
    assert result.useful_sides_received == result.sides_updated == 16
    assert result.missing_sides == 0
    assert result.observations_le_2h == 16
    assert result.http_attempts == 2
    assert result.retry_count == 0
    assert result.elapsed_seconds == 2.5
    assert len(calls) == 2
    assert prices.count(Region.AMERICAS) == 8
    persisted = prices.get("T4_MAIN_SWORD", "Bridgewatch", 1, Region.AMERICAS)
    assert persisted is not None
    assert persisted.sell_price_timestamp == NOW - timedelta(minutes=30)
    assert persisted.fetched_at == NOW
    assert [value.completed_batches for value in progress] == [1, 2]


def test_full_sync_keeps_success_before_later_failure_and_cancellation(tmp_path) -> None:
    database = _database(tmp_path)
    catalog = CatalogRepository(database)
    _catalog(catalog)
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    calls = 0

    def failing_transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise URLError("offline")
        return _payload(url)

    def failing_factory(
        region: Region,
        *,
        batch_size: int,
        max_url_length: int,
        max_batches: int,
    ) -> AODPClient:
        return AODPClient(
            region,
            batch_size=batch_size,
            max_url_length=max_url_length,
            max_batches=max_batches,
            max_retries=0,
            transport=failing_transport,
            wall_clock=lambda: NOW,
        )

    result = RoyalMarketSyncService(
        RoyalMarketUniverseService(catalog),
        prices,
        client_factory=failing_factory,
        batch_size=2,
        wall_clock=lambda: NOW,
    ).synchronize(Region.AMERICAS, ("Bridgewatch",))
    assert result.status == "partial"
    assert result.successful_batches == 1
    assert result.failed_batches == 1
    assert prices.count(Region.AMERICAS) == 2

    cancel_database = _database(tmp_path, "cancel.db")
    cancel_catalog = CatalogRepository(cancel_database)
    _catalog(cancel_catalog)
    cancel_prices = MarketPriceRepository(cancel_database, wall_clock=lambda: NOW)
    cancel_calls = 0

    def cancel_transport(url: str, _timeout: float) -> bytes:
        nonlocal cancel_calls
        cancel_calls += 1
        return _payload(url)

    def cancel_factory(
        region: Region,
        *,
        batch_size: int,
        max_url_length: int,
        max_batches: int,
    ) -> AODPClient:
        return AODPClient(
            region,
            batch_size=batch_size,
            max_url_length=max_url_length,
            max_batches=max_batches,
            transport=cancel_transport,
            wall_clock=lambda: NOW,
        )

    cancelled = RoyalMarketSyncService(
        RoyalMarketUniverseService(cancel_catalog),
        cancel_prices,
        client_factory=cancel_factory,
        batch_size=2,
        wall_clock=lambda: NOW,
    ).synchronize(
        Region.AMERICAS,
        ("Bridgewatch",),
        is_cancelled=lambda: cancel_calls >= 1,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.completed_batches == 1
    assert cancelled.cancelled_batches == 1
    assert cancel_prices.count(Region.AMERICAS) == 2


def test_full_sync_reports_retry_malformed_empty_and_missing_sides(tmp_path) -> None:
    database = _database(tmp_path)
    catalog = CatalogRepository(database)
    _catalog(catalog)
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    calls = 0
    delays: list[float] = []
    headers = Message()
    headers["Retry-After"] = "0"

    def transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, 429, "rate limited", headers, None)
        item_ids, cities = _url_scope(url)
        if "T4_MAIN_SWORD" not in item_ids:
            return b"[]"
        return json.dumps(
            [
                {
                    "item_id": "T4_MAIN_SWORD",
                    "city": cities[0],
                    "quality": 1,
                    "sell_price_min": 123,
                    "sell_price_min_date": (NOW - timedelta(hours=3)).isoformat(),
                    "buy_price_max": 0,
                    "buy_price_max_date": "0001-01-01T00:00:00",
                },
                {"item_id": None, "city": cities[0], "quality": 1},
            ]
        ).encode()

    def factory(
        region: Region,
        *,
        batch_size: int,
        max_url_length: int,
        max_batches: int,
    ) -> AODPClient:
        return AODPClient(
            region,
            batch_size=batch_size,
            max_url_length=max_url_length,
            max_batches=max_batches,
            max_retries=1,
            retry_backoff_seconds=0,
            sleeper=delays.append,
            transport=transport,
            wall_clock=lambda: NOW,
        )

    result = RoyalMarketSyncService(
        RoyalMarketUniverseService(catalog),
        prices,
        client_factory=factory,
        batch_size=2,
        wall_clock=lambda: NOW,
    ).synchronize(Region.AMERICAS, ("Bridgewatch",))

    assert result.status == "partial"
    assert result.successful_batches == 2
    assert result.http_attempts == 3
    assert result.retry_count == 1
    assert len(result.record_failures) == 1
    assert result.rows_returned == 1
    assert result.useful_sides_received == 1
    assert result.missing_sides == 7
    assert result.rows_with_no_usable_side == 3
    assert result.observations_le_4h == 1
    assert delays == []


def test_market_coverage_uses_real_side_timestamps_and_missing_rows(tmp_path) -> None:
    database = _database(tmp_path)
    repository = MarketPriceRepository(database, wall_clock=lambda: NOW)
    item_ids = ("THIRTY_MIN", "THREE_H", "EIGHT_H", "TWENTY_SIX_H", "MISSING")
    ages = (
        timedelta(minutes=30),
        timedelta(hours=3),
        timedelta(hours=8),
        timedelta(hours=26),
        None,
    )
    repository.upsert_many(
        MarketPrice(
            item_id,
            "Bridgewatch",
            1,
            Region.AMERICAS,
            None if age is None else 100,
            None if age is None else NOW - age,
            None,
            None,
            NOW,
        )
        for item_id, age in zip(item_ids, ages, strict=True)
    )

    summary = MarketCoverageService(repository).summary(
        Region.AMERICAS,
        ("Bridgewatch",),
        item_ids,
        NOW,
    )

    assert summary.expected_rows == summary.market_rows == 5
    assert summary.observed_within_2h == 1
    assert summary.observed_within_4h == 2
    assert summary.observed_within_24h == 3
    assert summary.observed_older_than_24h == 1
    assert summary.no_usable_price == 1
    assert summary.observations_le_2h == 1
    assert summary.observations_le_4h == 2
    assert summary.observations_le_24h == 3
    assert summary.observations_older_24h == 1
    assert summary.missing_sides == 6


def test_sync_metadata_and_city_preferences_persist_without_touching_overrides(tmp_path) -> None:
    database = _database(tmp_path)
    catalog = CatalogRepository(database)
    _catalog(catalog)
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    overrides = PriceOverrideRepository(database)
    overrides.set(
        UserPriceOverride(
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
            Region.AMERICAS,
            MarketSide.SELL_ORDER,
            999,
            NOW,
        )
    )

    def factory(
        region: Region,
        *,
        batch_size: int,
        max_url_length: int,
        max_batches: int,
    ) -> AODPClient:
        return AODPClient(
            region,
            batch_size=batch_size,
            max_url_length=max_url_length,
            max_batches=max_batches,
            transport=lambda url, _timeout: _payload(url),
            wall_clock=lambda: NOW,
        )

    result = RoyalMarketSyncService(
        RoyalMarketUniverseService(catalog),
        prices,
        client_factory=factory,
        wall_clock=lambda: NOW,
    ).synchronize(Region.AMERICAS, ("Martlock", "Caerleon"))
    state = MarketSyncStateRepository(SettingsRepository(database))
    state.save_cities(("Martlock", "Caerleon"))
    state.save_result(result)

    assert state.cities() == ("Martlock", "Caerleon")
    stored = state.last_result()
    assert stored is not None
    assert stored.item_count == 4
    assert stored.status == "complete"
    saved_override = overrides.get(
        "T4_MAIN_SWORD",
        "Bridgewatch",
        1,
        Region.AMERICAS,
        MarketSide.SELL_ORDER,
    )
    assert saved_override is not None
    assert saved_override.price == 999
