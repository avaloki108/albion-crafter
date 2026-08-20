import json
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.database.database import Database, MarketPriceRepository
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.market.aodp import AODPClient
from albion_crafter.market.estimation import MarketPriceSource, PriceConfidence
from albion_crafter.market.history import AODPHistoryClient
from albion_crafter.market.models import MarketPrice, MarketSide, Region
from albion_crafter.market.recipe_refresh import (
    RecipePriceAvailabilityStatus,
    RecipePriceRefreshRequest,
    RecipePriceRefreshService,
    RecipePriceRole,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _recipe(*materials: str) -> Recipe:
    return Recipe(
        output=Item("OUTPUT", "Output", 4),
        output_quantity=1,
        materials=tuple(MaterialRequirement(item_id, 1) for item_id in materials),
    )


def _request(
    recipe: Recipe,
    *,
    material_city: str = "Bridgewatch",
    sell_city: str = "Bridgewatch",
    output_quality: int = 1,
    material_side: MarketSide = MarketSide.SELL_ORDER,
    output_side: MarketSide = MarketSide.SELL_ORDER,
) -> RecipePriceRefreshRequest:
    return RecipePriceRefreshRequest(
        recipe=recipe,
        region=Region.AMERICAS,
        material_city=material_city,
        sell_city=sell_city,
        output_quality=output_quality,
        material_side=material_side,
        output_side=output_side,
    )


def _request_identity(url: str) -> tuple[tuple[str, ...], str, int]:
    parsed = urlparse(url)
    item_path = parsed.path.partition("/prices/")[2].removesuffix(".json")
    query = parse_qs(parsed.query)
    return (
        tuple(unquote(value) for value in item_path.split(",")),
        query["locations"][0],
        int(query["qualities"][0]),
    )


def _payload(
    item_ids: tuple[str, ...],
    city: str,
    quality: int,
    *,
    sell_price: int = 100,
    buy_price: int = 90,
) -> bytes:
    return json.dumps(
        [
            {
                "item_id": item_id,
                "city": city,
                "quality": quality,
                "sell_price_min": sell_price,
                "sell_price_min_date": "2026-08-18T11:00:00Z",
                "buy_price_max": buy_price,
                "buy_price_max_date": "2026-08-18T11:00:00Z",
            }
            for item_id in item_ids
        ]
    ).encode()


def _service(tmp_path, transport, **client_options):
    database = Database(tmp_path / "recipe-refresh.db")
    database.initialize()
    repository = MarketPriceRepository(database, wall_clock=lambda: NOW)
    service = RecipePriceRefreshService(
        repository,
        client_factory=lambda region: AODPClient(
            region,
            transport=transport,
            wall_clock=lambda: NOW,
            retry_backoff_seconds=0,
            **client_options,
        ),
    )
    return service, repository


def test_same_city_normal_quality_merges_exact_recipe_keys_and_selected_sides(tmp_path) -> None:
    requested = []

    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        requested.append(identity)
        return _payload(*identity)

    service, repository = _service(tmp_path, transport)
    progress = []
    request = _request(
        _recipe("MATERIAL_A", "MATERIAL_A", "MATERIAL_B"),
        output_side=MarketSide.BUY_ORDER,
    )

    plan = service.plan(request)
    result = service.refresh(request, on_progress=progress.append)

    assert len(plan.groups) == 1
    assert plan.groups[0].item_ids == ("MATERIAL_A", "MATERIAL_B", "OUTPUT")
    assert requested == [(("MATERIAL_A", "MATERIAL_B", "OUTPUT"), "Bridgewatch", 1)]
    assert result.requirements_requested == 3
    assert result.network_keys_requested == 3
    assert result.groups_planned == result.groups_completed == 1
    assert result.batches_planned == result.batches_completed == 1
    assert result.batches_succeeded == 1
    assert result.batches_failed == result.record_failures == 0
    assert result.selected_sides_available == 3
    assert result.selected_sides_missing == 0
    assert result.is_complete
    assert not result.is_partial
    assert [value.status for value in result.availability] == [
        RecipePriceAvailabilityStatus.UPDATED,
        RecipePriceAvailabilityStatus.UPDATED,
        RecipePriceAvailabilityStatus.UPDATED,
    ]
    material_prices = [
        value.price
        for value in result.availability
        if value.requirement.role is RecipePriceRole.MATERIAL
    ]
    output = next(
        value for value in result.availability if value.requirement.role is RecipePriceRole.OUTPUT
    )
    assert material_prices == [100, 100]
    assert output.price == 90
    assert repository.count() == 3
    assert len(progress) == 1
    assert progress[0].groups_completed == 1
    assert progress[0].batches_completed == 1
    assert progress[0].records_loaded == 3


def test_different_city_and_output_quality_never_widen_to_a_cross_product(tmp_path) -> None:
    requested = []

    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        requested.append(identity)
        return _payload(*identity)

    service, repository = _service(tmp_path, transport)
    request = _request(
        _recipe("MATERIAL_A", "MATERIAL_B"),
        material_city="Thetford",
        sell_city="Fort Sterling",
        output_quality=3,
    )

    result = service.refresh(request)

    assert requested == [
        (("MATERIAL_A", "MATERIAL_B"), "Thetford", 1),
        (("OUTPUT",), "Fort Sterling", 3),
    ]
    assert result.groups_planned == 2
    assert result.network_keys_requested == 3
    assert repository.get("MATERIAL_A", "Thetford", 1, Region.AMERICAS) is not None
    assert repository.get("OUTPUT", "Fort Sterling", 3, Region.AMERICAS) is not None
    assert repository.get("OUTPUT", "Thetford", 1, Region.AMERICAS) is None
    assert repository.get("MATERIAL_A", "Fort Sterling", 3, Region.AMERICAS) is None


def test_failed_group_keeps_successful_group_and_reports_partial_metrics(tmp_path) -> None:
    requested = []

    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        requested.append(identity)
        if identity[1] == "Fort Sterling":
            raise URLError("output market offline")
        return _payload(*identity)

    service, repository = _service(tmp_path, transport)
    result = service.refresh(
        _request(
            _recipe("MATERIAL"),
            material_city="Thetford",
            sell_city="Fort Sterling",
        )
    )

    assert [value[1] for value in requested] == [
        "Thetford",
        "Fort Sterling",
        "Fort Sterling",
    ]
    assert result.groups_completed == 2
    assert result.batches_completed == 2
    assert result.batches_succeeded == 1
    assert result.batches_failed == 1
    assert result.request_attempts == 3
    assert result.retry_count == 1
    assert result.records_loaded == 1
    assert result.has_errors
    assert result.is_partial
    assert not result.is_complete
    assert result.selected_sides_available == 1
    assert result.selected_sides_missing == 1
    assert len(result.failures) == 1
    assert result.failures[0].group_number == 2
    assert result.failures[0].city == "Fort Sterling"
    assert repository.get("MATERIAL", "Thetford", 1, Region.AMERICAS) is not None
    assert repository.get("OUTPUT", "Fort Sterling", 1, Region.AMERICAS) is None


def test_partial_batches_within_one_group_persist_each_success_and_continue(tmp_path) -> None:
    requested = []

    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        requested.append(identity)
        if identity[0] == ("MATERIAL_B",):
            raise URLError("one material offline")
        return _payload(*identity)

    service, repository = _service(tmp_path, transport, batch_size=1)
    result = service.refresh(_request(_recipe("MATERIAL_A", "MATERIAL_B")))

    assert [value[0] for value in requested] == [
        ("MATERIAL_A",),
        ("MATERIAL_B",),
        ("MATERIAL_B",),
        ("OUTPUT",),
    ]
    assert result.groups_planned == result.groups_completed == 1
    assert result.batches_planned == result.batches_completed == 3
    assert result.batches_succeeded == 2
    assert result.batches_failed == 1
    assert result.request_attempts == 4
    assert result.retry_count == 1
    assert result.is_partial
    assert repository.get("MATERIAL_A", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("MATERIAL_B", "Bridgewatch", 1, Region.AMERICAS) is None
    assert repository.get("OUTPUT", "Bridgewatch", 1, Region.AMERICAS) is not None


def test_successful_http_with_missing_selected_side_is_not_complete(tmp_path) -> None:
    def transport(url: str, _timeout: float) -> bytes:
        identity = _request_identity(url)
        return _payload(*identity, buy_price=0)

    service, _repository = _service(tmp_path, transport)
    result = service.refresh(_request(_recipe("MATERIAL"), output_side=MarketSide.BUY_ORDER))

    assert result.batches_succeeded == 1
    assert not result.has_errors
    assert result.selected_sides_available == 1
    assert result.selected_sides_missing == 1
    assert result.missing_requirements[0].role is RecipePriceRole.OUTPUT
    assert not result.is_complete
    assert result.is_partial


def test_missing_sell_is_batched_into_daily_history_and_becomes_estimated(tmp_path) -> None:
    database = Database(tmp_path / "recipe-history-fallback.db")
    database.initialize()
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    history = MarketHistoryRepository(database)
    history_urls: list[str] = []

    def current_transport(url: str, _timeout: float) -> bytes:
        item_ids, city, quality = _request_identity(url)
        rows = json.loads(_payload(item_ids, city, quality))
        for row in rows:
            if row["item_id"] == "T5_POTION_ACID":
                row["sell_price_min"] = 0
                row["sell_price_min_date"] = "0001-01-01T00:00:00"
        return json.dumps(rows).encode()

    def history_transport(url: str, _timeout: float) -> bytes:
        history_urls.append(url)
        assert "/history/T5_POTION_ACID.json" in url
        return json.dumps(
            [
                {
                    "location": "Bridgewatch",
                    "item_id": "T5_POTION_ACID",
                    "quality": 1,
                    "data": [
                        {
                            "item_count": 20,
                            "avg_price": 10_000 + day * 10,
                            "timestamp": f"2026-08-{18 - day:02d}T00:00:00",
                        }
                        for day in range(7)
                    ],
                }
            ]
        ).encode()

    service = RecipePriceRefreshService(
        prices,
        client_factory=lambda region: AODPClient(
            region,
            transport=current_transport,
            wall_clock=lambda: NOW,
        ),
        history_repository=history,
        history_client_factory=lambda region: AODPHistoryClient(
            region,
            transport=history_transport,
            wall_clock=lambda: NOW,
        ),
        wall_clock=lambda: NOW,
    )
    result = service.refresh(
        _request(
            Recipe(
                output=Item("T5_POTION_ACID", "Acid Potion", 5),
                output_quantity=10,
                materials=(MaterialRequirement("T5_TEASEL", 48),),
            )
        )
    )

    output = next(
        value for value in result.availability if value.requirement.role is RecipePriceRole.OUTPUT
    )
    assert len(history_urls) == 1
    assert result.history_batches_succeeded == 1
    assert result.historical_estimates_available == 1
    assert output.status is RecipePriceAvailabilityStatus.HISTORICAL_ESTIMATE
    assert output.source is MarketPriceSource.HISTORICAL_ESTIMATE
    assert output.confidence is PriceConfidence.HIGH
    assert output.historical_days_used == 7
    assert result.selected_sides_available == 2
    assert result.is_complete


def test_missing_buy_never_triggers_sell_history_fallback(tmp_path) -> None:
    database = Database(tmp_path / "recipe-buy-no-history.db")
    database.initialize()
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    history = MarketHistoryRepository(database)
    history_calls = 0

    def current_transport(url: str, _timeout: float) -> bytes:
        return _payload(*_request_identity(url), buy_price=0)

    def history_transport(_url: str, _timeout: float) -> bytes:
        nonlocal history_calls
        history_calls += 1
        return b"[]"

    service = RecipePriceRefreshService(
        prices,
        client_factory=lambda region: AODPClient(
            region,
            transport=current_transport,
            wall_clock=lambda: NOW,
        ),
        history_repository=history,
        history_client_factory=lambda region: AODPHistoryClient(
            region,
            transport=history_transport,
            wall_clock=lambda: NOW,
        ),
        wall_clock=lambda: NOW,
    )
    result = service.refresh(_request(_recipe("T5_TEASEL"), output_side=MarketSide.BUY_ORDER))

    assert history_calls == 0
    assert result.historical_estimates_available == 0
    assert result.selected_sides_missing == 1


def test_malformed_row_is_reported_while_valid_recipe_price_is_persisted(tmp_path) -> None:
    def transport(url: str, _timeout: float) -> bytes:
        item_ids, city, quality = _request_identity(url)
        assert item_ids == ("MATERIAL", "OUTPUT")
        return json.dumps(
            [
                json.loads(_payload(("MATERIAL",), city, quality))[0],
                {"item_id": "OUTPUT", "city": city, "quality": "invalid"},
            ]
        ).encode()

    service, repository = _service(tmp_path, transport)
    result = service.refresh(_request(_recipe("MATERIAL")))

    assert result.batches_succeeded == 1
    assert result.batches_failed == 0
    assert result.record_failures == 1
    assert len(result.record_failure_details) == 1
    assert result.record_failure_details[0].row_number == 2
    assert result.has_errors
    assert result.is_partial
    assert repository.get("MATERIAL", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("OUTPUT", "Bridgewatch", 1, Region.AMERICAS) is None


def test_cancellation_between_sparse_groups_preserves_completed_group(tmp_path) -> None:
    cancelled = False

    def transport(url: str, _timeout: float) -> bytes:
        return _payload(*_request_identity(url))

    service, repository = _service(tmp_path, transport)

    def stop_after_first(_progress) -> None:
        nonlocal cancelled
        cancelled = True

    result = service.refresh(
        _request(
            _recipe("MATERIAL"),
            material_city="Thetford",
            sell_city="Fort Sterling",
        ),
        is_cancelled=lambda: cancelled,
        on_progress=stop_after_first,
    )

    assert result.cancelled
    assert result.groups_planned == 2
    assert result.groups_completed == 1
    assert result.batches_completed == result.batches_succeeded == 1
    assert len(result.outcomes) == 1
    assert result.is_partial
    assert repository.get("MATERIAL", "Thetford", 1, Region.AMERICAS) is not None
    assert repository.get("OUTPUT", "Fort Sterling", 1, Region.AMERICAS) is None


def test_cancellation_inside_group_reports_partial_metrics_and_keeps_first_batch(tmp_path) -> None:
    cancelled = False

    def transport(url: str, _timeout: float) -> bytes:
        return _payload(*_request_identity(url))

    service, repository = _service(tmp_path, transport, batch_size=1)
    progress = []

    def stop_after_first(value) -> None:
        nonlocal cancelled
        progress.append(value)
        if value.batches_completed == 1:
            cancelled = True

    result = service.refresh(
        _request(_recipe("MATERIAL_A", "MATERIAL_B")),
        is_cancelled=lambda: cancelled,
        on_progress=stop_after_first,
    )

    assert result.cancelled
    assert result.groups_planned == 1
    assert result.groups_completed == 0
    assert result.batches_planned == 3
    assert result.batches_completed == result.batches_succeeded == 1
    assert result.batches_failed == 0
    assert result.records_loaded == 1
    assert progress[-1].cancelled
    assert progress[-1].batches_completed == 1
    assert repository.get("MATERIAL_A", "Bridgewatch", 1, Region.AMERICAS) is not None
    assert repository.get("MATERIAL_B", "Bridgewatch", 1, Region.AMERICAS) is None
    assert repository.get("OUTPUT", "Bridgewatch", 1, Region.AMERICAS) is None


def test_newer_cached_selected_side_is_retained_when_live_side_is_older(tmp_path) -> None:
    def transport(url: str, _timeout: float) -> bytes:
        return _payload(*_request_identity(url), buy_price=90)

    service, repository = _service(tmp_path, transport)
    repository.upsert_many(
        (
            MarketPrice(
                item_id="OUTPUT",
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=None,
                sell_price_timestamp=None,
                buy_price=999,
                buy_price_timestamp=NOW,
                fetched_at=NOW,
            ),
        )
    )

    result = service.refresh(_request(_recipe("MATERIAL"), output_side=MarketSide.BUY_ORDER))

    output = next(
        value for value in result.availability if value.requirement.role is RecipePriceRole.OUTPUT
    )
    assert output.status is RecipePriceAvailabilityStatus.RETAINED
    assert output.price == 999
    assert output.observed_at == NOW
    assert result.is_complete


@pytest.mark.parametrize("quality", [True, 0, 6, 1.5])
def test_request_rejects_invalid_output_quality(quality) -> None:
    with pytest.raises(ValueError, match="output_quality"):
        _request(_recipe("MATERIAL"), output_quality=quality)
