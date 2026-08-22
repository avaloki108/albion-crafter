import json
from datetime import UTC, date, datetime, timedelta
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import pytest

from albion_crafter.core.provenance import Provenance
from albion_crafter.market.history import (
    AODPHistoryClient,
    HistoryTimeScale,
)
from albion_crafter.market.models import Region


def _history_series(*points: dict, item_id: str = "T4_BAG") -> dict:
    return {
        "location": "Bridgewatch",
        "item_id": item_id,
        "quality": 1,
        "data": list(points),
    }


def _point(
    timestamp: str = "2026-08-15T00:00:00",
    *,
    item_count: int = 20,
    average_price: int = 4_000,
) -> dict:
    return {
        "item_count": item_count,
        "avg_price": average_price,
        "timestamp": timestamp,
    }


def test_history_url_is_explicit_bounded_and_uses_documented_parameters() -> None:
    client = AODPHistoryClient(Region.EUROPE)
    url = client.build_history_url(
        ["T4_BAG", "T4_BAG"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 8),
        cities=["Bridgewatch", "Fort Sterling"],
        qualities=[1],
        time_scale=HistoryTimeScale.DAILY,
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.netloc == "europe.albion-online-data.com"
    assert parsed.path.endswith("/history/T4_BAG.json")
    assert query == {
        "date": ["2026-08-01"],
        "end_date": ["2026-08-08"],
        "locations": ["Bridgewatch,Fort Sterling"],
        "qualities": ["1"],
        "time-scale": ["24"],
    }
    assert len(url.encode("ascii")) <= 3_900


def test_history_parser_preserves_reported_counts_prices_and_utc_timestamp() -> None:
    payload = json.dumps(
        [
            _history_series(
                _point(item_count=10, average_price=4_000),
                _point("2026-08-16T00:00:00", item_count=30, average_price=5_000),
            )
        ]
    ).encode()
    clock_values = iter((5.0, 7.25))
    result = AODPHistoryClient(
        transport=lambda _url, _timeout: payload,
        clock=lambda: next(clock_values),
    ).fetch_history(
        ["T4_BAG"],
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 17),
        cities=["Bridgewatch"],
    )

    assert result.records_returned == 2
    assert result.items_requested == 1
    assert result.successful_batches == 1
    assert result.http_batches == 1
    assert result.failed_batches == 0
    assert result.elapsed_seconds == 2.25
    first = result.intervals[0]
    assert first.observed_at == datetime(2026, 8, 15, tzinfo=UTC)
    assert first.item_count == 10
    assert first.average_price == 4_000
    assert first.time_scale is HistoryTimeScale.DAILY
    assert first.provenance is Provenance.AODP_LIVE


def test_history_parser_rejects_materially_future_dated_point() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    payload = json.dumps(
        [_history_series(_point((now + timedelta(minutes=5)).isoformat()))]
    ).encode()
    result = AODPHistoryClient(
        transport=lambda _url, _timeout: payload,
        wall_clock=lambda: now,
    ).fetch_history(
        ["T4_BAG"],
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 18),
        cities=["Bridgewatch"],
    )

    assert not result.intervals
    assert len(result.record_failures) == 1
    assert "future-dated" in result.record_failures[0].message


def test_history_daily_source_timestamp_is_preserved_without_guessing_boundary() -> None:
    payload = json.dumps([_history_series(_point("2026-08-14T00:00:00"))]).encode()
    result = AODPHistoryClient(transport=lambda _url, _timeout: payload).fetch_history(
        ["T4_BAG"],
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 18),
        cities=["Bridgewatch"],
    )
    assert result.intervals[0].observed_at == datetime(2026, 8, 14, tzinfo=UTC)


def test_history_malformed_points_do_not_destroy_valid_points() -> None:
    payload = json.dumps(
        [
            _history_series(
                _point(),
                _point("bad timestamp"),
                {"item_count": 0, "avg_price": 100, "timestamp": "2026-08-16T00:00:00"},
            )
        ]
    ).encode()
    result = AODPHistoryClient(transport=lambda _url, _timeout: payload).fetch_history(
        ["T4_BAG"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 20),
        cities=["Bridgewatch"],
    )

    assert len(result.intervals) == 1
    assert len(result.record_failures) == 2
    assert result.is_partial
    assert result.successful_batches == 1


def test_history_rejects_unrequested_series_without_losing_valid_series() -> None:
    payload = json.dumps(
        [
            _history_series(_point()),
            _history_series(_point(), item_id="T8_NOT_REQUESTED"),
        ]
    ).encode()
    result = AODPHistoryClient(transport=lambda _url, _timeout: payload).fetch_history(
        ["T4_BAG"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 20),
        cities=["Bridgewatch"],
    )

    assert len(result.intervals) == 1
    assert len(result.record_failures) == 1
    assert "unexpected history item_id" in result.record_failures[0].message


def test_history_empty_success_remains_empty_not_zero_volume() -> None:
    result = AODPHistoryClient(transport=lambda _url, _timeout: b"[]").fetch_history(
        ["T4_BAG"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 8),
        cities=["Bridgewatch"],
    )
    assert result.intervals == ()
    assert result.successful_batches == 1
    assert not result.has_errors


def test_history_partial_batch_failure_preserves_successes() -> None:
    calls = 0

    def transport(url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if "/history/TWO.json" in url:
            raise URLError("offline batch")
        return json.dumps([_history_series(_point(), item_id="ONE")]).encode()

    result = AODPHistoryClient(batch_size=1, transport=transport).fetch_history(
        ["ONE", "TWO"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 8),
        cities=["Bridgewatch"],
    )
    assert len(result.intervals) == 1
    assert result.is_partial
    assert result.successful_batches == 1
    assert result.failed_batches == 1


def test_history_stops_after_repeated_endpoint_failures() -> None:
    calls = 0

    def transport(_url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise URLError("endpoint offline")

    result = AODPHistoryClient(
        batch_size=1,
        max_batches=3,
        transport=transport,
        retry_backoff_seconds=0,
    ).fetch_history(
        ["ONE", "TWO", "THREE"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 8),
        cities=["Bridgewatch"],
    )

    assert calls == 4
    assert result.circuit_breaker_open
    assert result.batch_count == 3
    assert result.completed_batches == result.failed_batches == 2


def test_history_batches_long_ids_by_complete_url_length() -> None:
    calls: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return b"[]"

    ids = [f"T4_HISTORY_{index}_" + ("X" * 125) for index in range(40)]
    result = AODPHistoryClient(transport=transport).fetch_history(
        ids,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 8),
        cities=["Bridgewatch", "Fort Sterling"],
    )
    assert 1 < result.batch_count < len(ids)
    assert all(len(url.encode("ascii")) <= 3_900 for url in calls)


def test_history_request_limits_and_window_validation_happen_before_network() -> None:
    calls = 0

    def transport(_url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return b"[]"

    client = AODPHistoryClient(batch_size=1, max_batches=1, transport=transport)
    kwargs = {
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 8),
        "cities": ["Bridgewatch"],
    }
    with pytest.raises(ValueError, match="needs 2 batches"):
        client.fetch_history(["ONE", "TWO"], **kwargs)
    with pytest.raises(ValueError, match="cannot exceed 31 days"):
        client.fetch_history(
            ["ONE"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 1),
            cities=["Bridgewatch"],
        )
    with pytest.raises(ValueError, match="explicit history city"):
        client.fetch_history(
            ["ONE"],
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 8),
            cities=[],
        )
    assert calls == 0
