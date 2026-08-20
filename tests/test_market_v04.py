import json
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.market.aodp import AODPClient, plan_price_requests
from albion_crafter.market.cache import CachedMarketService
from albion_crafter.market.history import AODPHistoryClient, HistoryTimeScale
from albion_crafter.market.history_cache import CachedOutputHistoryService
from albion_crafter.market.models import (
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)
from albion_crafter.market.pricing import PriceResolver
from albion_crafter.opportunity.pricing import PricingIndex

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _recipe() -> Recipe:
    return Recipe(
        output=Item("OUTPUT", "Output", 4),
        output_quantity=1,
        materials=(MaterialRequirement("MATERIAL", 2),),
    )


def _market_rows() -> tuple[MarketPrice, ...]:
    return (
        MarketPrice(
            item_id="MATERIAL",
            city="Thetford",
            quality=1,
            region=Region.AMERICAS,
            sell_price=100,
            sell_price_timestamp=NOW - timedelta(minutes=10),
            buy_price=80,
            buy_price_timestamp=NOW - timedelta(hours=5),
            fetched_at=NOW,
        ),
        MarketPrice(
            item_id="OUTPUT",
            city="Bridgewatch",
            quality=2,
            region=Region.AMERICAS,
            sell_price=1_000,
            sell_price_timestamp=NOW - timedelta(hours=3),
            buy_price=900,
            buy_price_timestamp=NOW - timedelta(minutes=5),
            fetched_at=NOW,
        ),
    )


@pytest.mark.parametrize("output_side", [MarketSide.SELL_ORDER, MarketSide.BUY_ORDER])
def test_scalar_and_preloaded_price_resolution_are_identical(tmp_path, output_side) -> None:
    database = Database(tmp_path / "parity.db")
    database.initialize()
    prices = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    prices.upsert_many(_market_rows())
    override = UserPriceOverride(
        item_id="OUTPUT",
        city="Bridgewatch",
        quality=2,
        region=Region.AMERICAS,
        side=MarketSide.BUY_ORDER,
        price=1_250,
        entered_at=NOW - timedelta(minutes=1),
    )
    overrides.set(override)
    cached = tuple(
        row
        for row in (
            prices.get("MATERIAL", "Thetford", 1, Region.AMERICAS),
            prices.get("OUTPUT", "Bridgewatch", 2, Region.AMERICAS),
        )
        if row is not None
    )
    policy = FreshnessPolicy(timedelta(hours=4), timedelta(hours=2))

    scalar = PriceResolver(prices, overrides).resolve(
        _recipe(),
        buy_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=2,
        freshness_policy=policy,
        material_side=MarketSide.BUY_ORDER,
        output_side=output_side,
        as_of=NOW,
    )
    indexed = PricingIndex(cached, (override,)).resolve(
        _recipe(),
        material_city="Thetford",
        craft_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        output_quality=2,
        material_side=MarketSide.BUY_ORDER,
        output_side=output_side,
        freshness_policy=policy,
        as_of=NOW,
    )

    scalar_lines = tuple(
        (
            line.item_id,
            line.city,
            line.quality,
            line.side.value,
            line.role,
            line.price,
            line.observation_timestamp,
            line.fetched_at,
            line.provenance,
            line.freshness,
        )
        for line in scalar.resolved_prices
    )
    indexed_lines = tuple(
        (
            line.item_id,
            line.city,
            line.quality,
            line.side,
            line.role,
            line.price,
            line.observation_timestamp,
            line.fetched_at,
            line.provenance,
            line.freshness,
        )
        for line in indexed.evidence
    )
    assert scalar_lines == indexed_lines
    assert scalar.material_prices == indexed.material_prices
    assert scalar.output_price == indexed.output_price
    assert scalar.freshness is indexed.freshness
    assert scalar.oldest_timestamp == indexed.oldest_required_timestamp
    assert scalar.actionability.reasons == indexed.data_quality_reasons
    assert scalar.as_of == NOW
    assert scalar.age_seconds == indexed.oldest_required_age(NOW).total_seconds()


def test_scalar_resolver_treats_nonpositive_cached_value_as_missing(tmp_path) -> None:
    database = Database(tmp_path / "zero.db")
    database.initialize()
    prices = MarketPriceRepository(database)
    rows = list(_market_rows())
    rows[0] = MarketPrice(
        item_id="MATERIAL",
        city="Thetford",
        quality=1,
        region=Region.AMERICAS,
        sell_price=0,
        sell_price_timestamp=NOW,
        buy_price=0,
        buy_price_timestamp=NOW,
        fetched_at=NOW,
    )
    prices.upsert_many(rows)

    snapshot = PriceResolver(prices).resolve(
        _recipe(),
        buy_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=2,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    material = snapshot.resolved_prices[0]
    assert material.price is None
    assert material.observation_timestamp is None


def test_public_request_plan_reports_exact_complete_url_stats_without_network() -> None:
    ids = [f"T4_LONG_{index}_" + "X" * 150 for index in range(35)]
    plan = plan_price_requests(
        [*ids, ids[0].casefold()],
        cities=["Bridgewatch", "Fort Sterling"],
        batch_size=100,
    )

    assert plan.items_requested == len(ids)
    assert 1 < plan.batch_count < len(ids)
    assert plan.max_url_bytes == max(batch.url_bytes for batch in plan.batches)
    assert plan.total_url_bytes == sum(batch.url_bytes for batch in plan.batches)
    assert plan.max_concurrency == 1
    assert all(batch.url_bytes == len(batch.url.encode("ascii")) for batch in plan.batches)
    assert plan.max_url_bytes <= 3_900


def test_current_client_retries_429_and_honors_retry_after() -> None:
    calls = 0
    delays: list[float] = []
    headers = Message()
    headers["Retry-After"] = "2"

    def transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, 429, "rate limited", headers, None)
        return b"[]"

    result = AODPClient(
        transport=transport,
        max_retries=1,
        retry_backoff_seconds=0.1,
        sleeper=delays.append,
        wall_clock=lambda: NOW,
    ).fetch_prices_batched(["ONE"])

    assert result.successful_batches == 1
    assert result.request_attempts == 2
    assert result.retry_count == 1
    assert result.http_attempts == 2
    assert delays == [2.0]


def test_current_client_does_not_retry_permanent_http_failure() -> None:
    calls = 0
    delays: list[float] = []

    def transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise HTTPError(url, 400, "bad request", Message(), None)

    result = AODPClient(
        transport=transport,
        max_retries=3,
        sleeper=delays.append,
        wall_clock=lambda: NOW,
    ).fetch_prices_batched(["ONE"])

    assert result.failed_batches == 1
    assert result.request_attempts == 1
    assert result.retry_count == 0
    assert calls == 1
    assert delays == []


def test_current_refresh_cancellation_keeps_completed_batch_in_cache(tmp_path) -> None:
    database = Database(tmp_path / "cancel-current.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    cancelled = False

    def transport(url: str, _timeout: float) -> bytes:
        item_id = url.partition("/prices/")[2].partition(".json")[0]
        return json.dumps(
            [
                {
                    "item_id": item_id,
                    "city": "Bridgewatch",
                    "quality": 1,
                    "sell_price_min": 100,
                    "sell_price_min_date": "2026-08-18T11:00:00Z",
                    "buy_price_max": 90,
                    "buy_price_max_date": "2026-08-18T11:00:00Z",
                }
            ]
        ).encode()

    def stop_after_first(_progress) -> None:
        nonlocal cancelled
        cancelled = True

    result = CachedMarketService(
        AODPClient(batch_size=1, transport=transport, wall_clock=lambda: NOW),
        repository,
    ).refresh(
        ["ONE", "TWO"],
        cities=["Bridgewatch"],
        is_cancelled=lambda: cancelled,
        on_progress=stop_after_first,
    )

    assert result.cancelled
    assert result.batch_count == 2
    assert result.completed_batches == 1
    assert result.successful_batches == 1
    assert repository.get("ONE", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("TWO", "Bridgewatch", 1, Region.AMERICAS) is None


def test_cached_output_history_distinguishes_success_empty_and_failure(tmp_path) -> None:
    database = Database(tmp_path / "history-cache.db")
    database.initialize()
    repository = MarketHistoryRepository(database)

    def transport(url: str, _timeout: float) -> bytes:
        item_id = url.partition("/history/")[2].partition(".json")[0]
        if item_id == "FAIL":
            raise URLError("offline")
        if item_id == "EMPTY":
            return b"[]"
        return json.dumps(
            [
                {
                    "location": "Bridgewatch",
                    "item_id": item_id,
                    "quality": 1,
                    "data": [
                        {
                            "item_count": 12,
                            "avg_price": 4_000,
                            "timestamp": "2026-08-17T12:00:00Z",
                        }
                    ],
                }
            ]
        ).encode()

    summary = CachedOutputHistoryService(
        AODPHistoryClient(
            batch_size=1,
            max_retries=1,
            retry_backoff_seconds=0,
            transport=transport,
            wall_clock=lambda: NOW,
        ),
        repository,
        wall_clock=lambda: NOW,
    ).refresh_outputs(
        ["ONE", "EMPTY", "FAIL"],
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 18),
        sell_cities=["Bridgewatch"],
    )

    assert summary.fetch.successful_batches == 2
    assert summary.fetch.failed_batches == 1
    assert summary.fetch.request_attempts == 4
    assert summary.fetch.retry_count == 1
    assert summary.success_coverage == 1
    assert summary.empty_coverage == 1
    assert summary.failed_coverage == 1
    cached = repository.list_for_outputs(
        Region.AMERICAS,
        ["ONE", "EMPTY", "FAIL"],
        ["Bridgewatch"],
        1,
        NOW - timedelta(days=30),
        time_scale=HistoryTimeScale.SIX_HOURLY,
    )
    assert [interval.item_id for interval in cached] == ["ONE"]
    coverage = repository.list_coverage(
        Region.AMERICAS,
        ["ONE", "EMPTY", "FAIL"],
        ["Bridgewatch"],
        1,
        HistoryTimeScale.SIX_HOURLY,
    )
    assert {row.item_id: row.status for row in coverage} == {
        "EMPTY": "empty",
        "FAIL": "failed",
        "ONE": "success",
    }


def test_cached_output_history_marks_unattempted_coverage_cancelled(tmp_path) -> None:
    database = Database(tmp_path / "history-cancel.db")
    database.initialize()
    repository = MarketHistoryRepository(database)
    cancelled = False

    def stop_after_first(_progress) -> None:
        nonlocal cancelled
        cancelled = True

    summary = CachedOutputHistoryService(
        AODPHistoryClient(
            batch_size=1,
            transport=lambda _url, _timeout: b"[]",
            wall_clock=lambda: NOW,
        ),
        repository,
        wall_clock=lambda: NOW,
    ).refresh_outputs(
        ["ONE", "TWO"],
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 18),
        sell_cities=["Bridgewatch"],
        is_cancelled=lambda: cancelled,
        on_progress=stop_after_first,
    )

    assert summary.cancelled
    assert summary.empty_coverage == 1
    assert summary.cancelled_coverage == 1
