import gzip
import json
from datetime import UTC, datetime, timedelta
from urllib.error import URLError

import pytest

import albion_crafter.market.aodp as aodp_module
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.database import Database, MarketPriceRepository
from albion_crafter.market.aodp import AODPClient, MarketDataError
from albion_crafter.market.cache import CachedMarketService
from albion_crafter.market.models import (
    Freshness,
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)


def test_freshness_states_are_timestamp_driven() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = FreshnessPolicy(timedelta(hours=4), timedelta(hours=2))
    assert policy.classify(now - timedelta(hours=1), now=now) is Freshness.FRESH
    assert policy.classify(now - timedelta(hours=3), now=now) is Freshness.AGING
    assert policy.classify(now - timedelta(hours=5), now=now) is Freshness.STALE
    assert policy.classify(None, now=now) is Freshness.UNKNOWN


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(0), Freshness.FRESH),
        (timedelta(minutes=2), Freshness.FRESH),
        (timedelta(minutes=5), Freshness.FUTURE),
        (timedelta(hours=1), Freshness.FUTURE),
        (timedelta(days=30), Freshness.FUTURE),
    ],
)
def test_freshness_rejects_timestamps_beyond_inclusive_two_minute_skew(
    offset: timedelta,
    expected: Freshness,
) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    assert FreshnessPolicy().classify(now + offset, now=now) is expected


def test_aodp_parser_preserves_timestamps_and_maps_zero_to_missing() -> None:
    payload = json.dumps(
        [
            {
                "item_id": "T4_TEST",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": 1234,
                "sell_price_min_date": "2026-01-01T12:00:00Z",
                "buy_price_max": 0,
                "buy_price_max_date": "0001-01-01T00:00:00",
            }
        ]
    ).encode()
    seen: list[str] = []

    def transport(url: str, timeout: float) -> bytes:
        seen.append(url)
        assert timeout == 3
        return payload

    record = AODPClient(Region.EUROPE, timeout=3, transport=transport).fetch_prices(
        ["T4_TEST"], cities=["Bridgewatch"]
    )[0]
    assert seen[0].startswith("https://europe.albion-online-data.com/api/v2/stats/prices/")
    assert record.sell_price == 1234
    assert record.sell_price_timestamp == datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert record.buy_price is None
    assert record.buy_price_timestamp is None


@pytest.mark.parametrize(
    ("price", "observed_at"),
    ((1234, "0001-01-01T00:00:00"), (0, "2026-01-01T12:00:00Z")),
)
def test_aodp_parser_normalizes_incomplete_side_pair_to_missing(
    price: int,
    observed_at: str,
) -> None:
    payload = json.dumps(
        [
            {
                "item_id": "T4_TEST",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": price,
                "sell_price_min_date": observed_at,
                "buy_price_max": 0,
                "buy_price_max_date": "0001-01-01T00:00:00",
            }
        ]
    ).encode()

    record = AODPClient(transport=lambda _url, _timeout: payload).fetch_prices(
        ["T4_TEST"], cities=["Bridgewatch"]
    )[0]

    assert record.sell_price is None
    assert record.sell_price_timestamp is None


def test_aodp_parser_rejects_materially_future_dated_observation() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    payload = json.dumps(
        [
            {
                "item_id": "T4_TEST",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": 1234,
                "sell_price_min_date": (now + timedelta(minutes=5)).isoformat(),
                "buy_price_max": 0,
                "buy_price_max_date": "0001-01-01T00:00:00",
            }
        ]
    ).encode()
    result = AODPClient(
        transport=lambda _url, _timeout: payload,
        wall_clock=lambda: now,
    ).fetch_prices_batched(["T4_TEST"], cities=["Bridgewatch"])

    assert not result.records
    assert len(result.record_failures) == 1
    assert "future-dated" in result.record_failures[0].message


def test_current_cache_refresh_replaces_poisoned_future_dated_sides(tmp_path) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    database = Database(tmp_path / "future-cache.db")
    database.initialize()
    repository = MarketPriceRepository(database, wall_clock=lambda: now)
    repository.upsert_many(
        (
            MarketPrice(
                "T4_TEST",
                "Bridgewatch",
                1,
                Region.AMERICAS,
                9_999,
                now + timedelta(days=30),
                8_888,
                now + timedelta(days=30),
                now,
            ),
        )
    )
    repository.upsert_many(
        (
            MarketPrice(
                "T4_TEST",
                "Bridgewatch",
                1,
                Region.AMERICAS,
                100,
                now,
                90,
                now,
                now,
            ),
        )
    )

    restored = repository.get("T4_TEST", "Bridgewatch", 1, Region.AMERICAS)
    assert restored is not None
    assert restored.sell_price == 100
    assert restored.sell_price_timestamp == now
    assert restored.buy_price == 90
    assert restored.buy_price_timestamp == now


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), float("-inf"), 1.5])
def test_manual_price_override_requires_positive_integral_price(bad_price: float) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        UserPriceOverride(
            "T4_TEST",
            "Bridgewatch",
            1,
            Region.AMERICAS,
            MarketSide.SELL_ORDER,
            bad_price,  # type: ignore[arg-type]
            datetime(2026, 8, 18, tzinfo=UTC),
            Provenance.USER_OVERRIDE,
        )


def test_aodp_malformed_response_is_recoverable_error() -> None:
    client = AODPClient(transport=lambda _url, _timeout: b"not json")
    with pytest.raises(MarketDataError):
        client.fetch_prices(["T4_TEST"])


def test_aodp_network_failure_is_recoverable_error() -> None:
    def failing_transport(_url: str, _timeout: float) -> bytes:
        raise URLError("offline")

    with pytest.raises(MarketDataError, match="batch 1 failed"):
        AODPClient(transport=failing_transport).fetch_prices(["T4_TEST"])


def test_aodp_requests_are_bounded_batches() -> None:
    calls: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return b"[]"

    result = AODPClient(batch_size=2, transport=transport).fetch_prices_batched(
        ["ONE", "TWO", "THREE", "FOUR", "FIVE"], cities=["Bridgewatch"]
    )
    assert result.batch_count == 3
    assert result.http_batches == 3
    assert len(calls) == 3
    assert all(len(url.encode("ascii")) <= 3_900 for url in calls)
    assert result.items_requested == 5
    assert result.successful_batches == 3
    assert result.failed_batches == 0
    assert result.records_returned == 0


def test_aodp_partial_batch_failure_keeps_successful_records() -> None:
    calls = 0

    def transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        requested = url.partition("/prices/")[2].partition(".json")[0]
        if requested == "THREE,FOUR":
            raise URLError("one batch failed")
        returned_id = "ONE" if requested == "ONE,TWO" else "FIVE"
        return json.dumps(
            [
                {
                    "item_id": returned_id,
                    "city": "Bridgewatch",
                    "quality": 1,
                    "sell_price_min": 100,
                    "sell_price_min_date": "2026-01-01T00:00:00Z",
                    "buy_price_max": 90,
                    "buy_price_max_date": "2026-01-01T00:00:00Z",
                }
            ]
        ).encode()

    result = AODPClient(batch_size=2, transport=transport).fetch_prices_batched(
        ["ONE", "TWO", "THREE", "FOUR", "FIVE"]
    )
    assert result.is_partial
    assert len(result.records) == 2
    assert len(result.failures) == 1
    assert result.failures[0].batch_number == 2


def test_aodp_invalid_record_is_a_batch_failure() -> None:
    result = AODPClient(
        transport=lambda _url, _timeout: b'[{"city":"Bridgewatch"}]'
    ).fetch_prices_batched(["T4_TEST"])
    assert not result.records
    assert not result.failures
    assert len(result.record_failures) == 1


def test_aodp_mixed_valid_and_invalid_rows_preserve_valid_records() -> None:
    payload = json.dumps(
        [
            {
                "item_id": "GOOD",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": 123,
                "sell_price_min_date": "2026-01-01T00:00:00Z",
                "buy_price_max": 0,
                "buy_price_max_date": "0001-01-01T00:00:00",
            },
            {"item_id": None, "city": "Bridgewatch", "quality": 1},
            {
                "item_id": "NOT_REQUESTED",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": 1,
            },
            {
                "item_id": "GOOD",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": -1,
            },
            {
                "item_id": "GOOD",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": 100,
                "sell_price_min_date": 0,
            },
        ]
    ).encode()
    result = AODPClient(transport=lambda _url, _timeout: payload).fetch_prices_batched(["GOOD"])

    assert [record.item_id for record in result.records] == ["GOOD"]
    assert len(result.record_failures) == 4
    assert result.is_partial
    assert result.successful_batches == 1


def test_aodp_strict_fetch_raises_for_an_invalid_individual_record() -> None:
    client = AODPClient(transport=lambda _url, _timeout: b'[{"item_id": null}]')
    with pytest.raises(MarketDataError, match="row 1 was invalid"):
        client.fetch_prices(["T4_TEST"])


def test_aodp_deduplicates_ids_and_reports_elapsed_time() -> None:
    calls: list[str] = []
    clock_values = iter((10.0, 12.5))

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return b"[]"

    result = AODPClient(
        transport=transport,
        clock=lambda: next(clock_values),
    ).fetch_prices_batched(["T4_TEST", "t4_test", "T5_TEST"])

    assert result.items_requested == 2
    assert result.elapsed_seconds == 2.5
    assert len(calls) == 1
    assert "T4_TEST,T5_TEST" in calls[0]


def test_aodp_rejects_an_oversized_first_or_single_item_before_transport() -> None:
    calls = 0

    def transport(_url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return b"[]"

    client = AODPClient(max_url_length=512, transport=transport)
    with pytest.raises(ValueError, match="too long"):
        client.fetch_prices_batched(["ID WITH SPACES" * 100], cities=["Fort Sterling"])
    assert calls == 0


def test_aodp_rejects_more_than_the_configured_request_count_before_network() -> None:
    calls = 0

    def transport(_url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return b"[]"

    client = AODPClient(batch_size=1, max_batches=1, transport=transport)
    with pytest.raises(ValueError, match="needs 2 batches"):
        client.fetch_prices_batched(["ONE", "TWO"])
    assert calls == 0


def test_aodp_url_bounding_uses_encoded_full_url_and_still_groups_items() -> None:
    calls: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return b"[]"

    ids = [f"T4_LONG_{index}_" + ("X" * 120) for index in range(40)]
    result = AODPClient(batch_size=100, transport=transport).fetch_prices_batched(
        ids,
        cities=["Bridgewatch", "Fort Sterling", "Thetford"],
    )

    assert result.batch_count > 1
    assert result.batch_count < len(ids)
    assert all(len(url.encode("ascii")) <= 3_900 for url in calls)
    assert any("," in url.partition("/prices/")[2].partition(".json")[0] for url in calls)


def test_default_transport_requests_and_decodes_gzip(monkeypatch) -> None:
    payload = json.dumps(
        [
            {
                "item_id": "T4_TEST",
                "city": "Bridgewatch",
                "quality": 1,
                "sell_price_min": 100,
                "sell_price_min_date": "2026-01-01T00:00:00Z",
                "buy_price_max": 0,
                "buy_price_max_date": "0001-01-01T00:00:00",
            }
        ]
    ).encode()
    seen_headers: dict[str, str | None] = {}

    class Response:
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return gzip.compress(payload)

    def fake_urlopen(request, *, timeout):
        assert timeout == 10
        seen_headers["accept"] = request.get_header("Accept")
        seen_headers["encoding"] = request.get_header("Accept-encoding")
        return Response()

    monkeypatch.setattr(aodp_module, "urlopen", fake_urlopen)
    record = AODPClient().fetch_prices(["T4_TEST"], cities=["Bridgewatch"])[0]

    assert record.sell_price == 100
    assert seen_headers == {"accept": "application/json", "encoding": "gzip"}


def test_partial_batch_refresh_commits_only_successful_batches(tmp_path) -> None:
    database = Database(tmp_path / "partial.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    calls = 0

    def transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if "/prices/FAIL.json" in url:
            raise URLError("failed batch")
        return json.dumps(
            [
                {
                    "item_id": "GOOD",
                    "city": "Bridgewatch",
                    "quality": 1,
                    "sell_price_min": 123,
                    "sell_price_min_date": "2026-01-01T00:00:00Z",
                    "buy_price_max": 0,
                    "buy_price_max_date": "0001-01-01T00:00:00",
                }
            ]
        ).encode()

    result = CachedMarketService(AODPClient(batch_size=1, transport=transport), repository).refresh(
        ["GOOD", "FAIL"], cities=["Bridgewatch"]
    )
    assert result.is_partial
    assert repository.get("GOOD", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("FAIL", "Bridgewatch", 1, Region.AMERICAS) is None
