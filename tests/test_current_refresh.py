import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import pytest

from albion_crafter.core.freshness import Freshness
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.database import Database, MarketPriceRepository
from albion_crafter.market.aodp import AODPClient, plan_price_requests
from albion_crafter.market.backfill import MissingSellHistoryBackfillResult
from albion_crafter.market.models import MarketSide, Region
from albion_crafter.planning.current_refresh import CurrentMarketRefreshExecutor
from albion_crafter.planning.models import MarketKey, PriceRequirement, PriceRole
from albion_crafter.planning.preflight import (
    MarketRefreshPlan,
    ObservationDisposition,
    PlannedAODPBatch,
    PriceRequirementAssessment,
    _default_batch_planner,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _refresh_plan(keys: tuple[MarketKey, ...]) -> MarketRefreshPlan:
    assessments = tuple(
        PriceRequirementAssessment(
            requirement=PriceRequirement(key, MarketSide.SELL_ORDER, PriceRole.OUTPUT),
            disposition=ObservationDisposition.MISSING,
            price=None,
            observed_at=None,
            provenance=Provenance.UNKNOWN,
            freshness=Freshness.UNKNOWN,
            needs_refresh=True,
        )
        for key in keys
    )
    grouped: dict[tuple[Region, str, int], list[str]] = defaultdict(list)
    for key in keys:
        grouped[(key.region, key.city, key.quality)].append(key.item_id)
    batches: list[PlannedAODPBatch] = []
    for (region, city, quality), item_ids in grouped.items():
        request_plan = plan_price_requests(
            item_ids,
            region=region,
            cities=(city,),
            qualities=(quality,),
        )
        batches.extend(
            PlannedAODPBatch(
                region=region,
                city=city,
                quality=quality,
                item_ids=batch.item_ids,
                url_length_bytes=batch.url_bytes,
            )
            for batch in request_plan.batches
        )
    return MarketRefreshPlan(assessments, tuple(batches), False)


def _request_identity(url: str) -> tuple[tuple[str, ...], str, int]:
    parsed = urlparse(url)
    item_path = parsed.path.partition("/prices/")[2].removesuffix(".json")
    query = parse_qs(parsed.query)
    return tuple(item_path.split(",")), query["locations"][0], int(query["qualities"][0])


def _payload(item_ids: tuple[str, ...], city: str, quality: int) -> bytes:
    return json.dumps(
        [
            {
                "item_id": item_id,
                "city": city,
                "quality": quality,
                "sell_price_min": 100,
                "sell_price_min_date": "2026-08-18T11:00:00Z",
                "buy_price_max": 90,
                "buy_price_max_date": "2026-08-18T11:00:00Z",
            }
            for item_id in item_ids
        ]
    ).encode()


def test_sparse_refresh_never_fetches_unused_city_quality_cross_product(tmp_path) -> None:
    database = Database(tmp_path / "sparse-current.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    keys = (
        MarketKey(Region.AMERICAS, "ONE", "Bridgewatch", 1),
        MarketKey(Region.AMERICAS, "TWO", "Martlock", 1),
        MarketKey(Region.AMERICAS, "THREE", "Thetford", 2),
    )
    requested: list[tuple[tuple[str, ...], str, int]] = []

    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        requested.append(identity)
        return _payload(*identity)

    result = CurrentMarketRefreshExecutor(
        repository,
        client_factory=lambda region: AODPClient(
            region,
            transport=transport,
            wall_clock=lambda: NOW,
        ),
        clock=lambda: 10.0 if not requested else 12.0,
    ).execute(_refresh_plan(keys))

    assert set(requested) == {
        (("ONE",), "Bridgewatch", 1),
        (("TWO",), "Martlock", 1),
        (("THREE",), "Thetford", 2),
    }
    assert result.keys_requested == 3
    assert result.groups_planned == result.groups_completed == 3
    assert result.batches_planned == result.batches_completed == 3
    assert result.batches_succeeded == 3
    assert result.batches_failed == 0
    assert result.records_loaded == 3
    assert result.elapsed_seconds == 2.0
    assert repository.get("ONE", "Martlock", 1, Region.AMERICAS) is None
    assert repository.get("THREE", "Bridgewatch", 2, Region.AMERICAS) is None


def test_sparse_refresh_keeps_successes_and_continues_after_one_batch_failure(tmp_path) -> None:
    database = Database(tmp_path / "partial-current.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    keys = (
        MarketKey(Region.AMERICAS, "ONE", "Bridgewatch", 1),
        MarketKey(Region.AMERICAS, "TWO", "Martlock", 1),
        MarketKey(Region.AMERICAS, "THREE", "Thetford", 1),
    )
    requested: list[tuple[tuple[str, ...], str, int]] = []

    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        requested.append(identity)
        if identity[1] == "Martlock":
            raise URLError("one group offline")
        return _payload(*identity)

    progress = []
    result = CurrentMarketRefreshExecutor(
        repository,
        client_factory=lambda region: AODPClient(
            region,
            transport=transport,
            retry_backoff_seconds=0,
            wall_clock=lambda: NOW,
        ),
    ).execute(_refresh_plan(keys), on_progress=progress.append)

    assert [identity[1] for identity in requested] == [
        "Bridgewatch",
        "Martlock",
        "Martlock",
        "Thetford",
    ]
    assert result.is_partial
    assert result.groups_completed == 3
    assert result.batches_completed == 3
    assert result.batches_succeeded == 2
    assert result.batches_failed == 1
    assert result.request_attempts == 4
    assert result.retry_count == 1
    assert result.records_loaded == 2
    assert len(result.failures) == 1
    assert result.failures[0].city == "Martlock"
    assert len(progress) == 3
    assert repository.get("ONE", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("TWO", "Martlock", 1, Region.AMERICAS) is None
    assert repository.get("THREE", "Thetford", 1, Region.AMERICAS) is not None


def test_sparse_refresh_cancels_between_exact_groups(tmp_path) -> None:
    database = Database(tmp_path / "cancel-plan-current.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    keys = (
        MarketKey(Region.AMERICAS, "ONE", "Bridgewatch", 1),
        MarketKey(Region.AMERICAS, "TWO", "Martlock", 1),
    )
    cancelled = False

    def transport(url: str, _timeout: float) -> bytes:
        return _payload(*_request_identity(url))

    def stop(_progress) -> None:
        nonlocal cancelled
        cancelled = True

    result = CurrentMarketRefreshExecutor(
        repository,
        client_factory=lambda region: AODPClient(
            region,
            transport=transport,
            wall_clock=lambda: NOW,
        ),
    ).execute(
        _refresh_plan(keys),
        is_cancelled=lambda: cancelled,
        on_progress=stop,
    )

    assert result.cancelled
    assert result.groups_planned == 2
    assert result.groups_completed == 1
    assert result.batches_completed == 1
    assert result.records_loaded == 1
    assert repository.get("ONE", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("TWO", "Martlock", 1, Region.AMERICAS) is None


def test_sparse_refresh_automatically_backfills_only_required_sell_sides(tmp_path) -> None:
    database = Database(tmp_path / "current-with-history.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    keys = (
        MarketKey(Region.AMERICAS, "SELL_ITEM", "Bridgewatch", 1),
        MarketKey(Region.AMERICAS, "BUY_ITEM", "Bridgewatch", 1),
    )
    base = _refresh_plan(keys)
    buy_assessment = replace(
        base.assessments[1],
        requirement=PriceRequirement(keys[1], MarketSide.BUY_ORDER, PriceRole.OUTPUT),
    )
    plan = MarketRefreshPlan((base.assessments[0], buy_assessment), base.batches, False)

    class RecordingBackfill:
        calls = []

        def refresh_missing(self, region, item_ids, cities, *, quality, is_cancelled):
            self.calls.append((region, item_ids, cities, quality, is_cancelled))
            key = (item_ids[0], cities[0], quality)
            return MissingSellHistoryBackfillResult((key,), (key,), (), ())

    backfill = RecordingBackfill()
    result = CurrentMarketRefreshExecutor(
        repository,
        client_factory=lambda region: AODPClient(
            region,
            transport=lambda url, _timeout: _payload(*_request_identity(url)),
            wall_clock=lambda: NOW,
        ),
        history_backfill=backfill,  # type: ignore[arg-type]
    ).execute(plan)

    assert len(backfill.calls) == 1
    assert backfill.calls[0][1] == ("SELL_ITEM",)
    assert result.history_keys_requested == 1
    assert result.historical_estimates_available == 1
    assert result.history_keys_unresolved == 0


def test_sparse_refresh_rejects_batch_that_widens_the_requested_keys(tmp_path) -> None:
    database = Database(tmp_path / "invalid-current.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    valid = _refresh_plan((MarketKey(Region.AMERICAS, "ONE", "Bridgewatch", 1),))
    invalid = MarketRefreshPlan(
        valid.assessments,
        (
            PlannedAODPBatch(
                Region.AMERICAS,
                "Bridgewatch",
                1,
                ("ONE", "UNUSED"),
                100,
            ),
        ),
        False,
    )

    with pytest.raises(ValueError, match="cover each refresh key exactly once"):
        CurrentMarketRefreshExecutor(repository).execute(invalid)


def test_default_preflight_batch_estimates_match_public_aodp_plan() -> None:
    keys = tuple(
        MarketKey(
            Region.AMERICAS,
            f"T4_LONG_{index}_" + "X" * 150,
            "Fort Sterling",
            1,
        )
        for index in range(35)
    )
    planned = _default_batch_planner(keys)
    public = plan_price_requests(
        tuple(sorted(key.item_id for key in keys)),
        region=Region.AMERICAS,
        cities=("Fort Sterling",),
        qualities=(1,),
    )

    assert tuple(batch.item_ids for batch in planned) == tuple(
        batch.item_ids for batch in public.batches
    )
    assert tuple(batch.url_length_bytes for batch in planned) == tuple(
        batch.url_bytes for batch in public.batches
    )
    assert max(batch.url_length_bytes for batch in planned) == public.max_url_bytes
    assert public.max_url_bytes <= 3_900
