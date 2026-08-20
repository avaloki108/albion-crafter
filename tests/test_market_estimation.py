from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.actionability import ReasonCode, ReasonSeverity
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.database.database import Database, MarketPriceRepository
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.market.estimation import (
    MarketPriceSource,
    PriceConfidence,
    estimate_historical_sell_price,
)
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.models import (
    Freshness,
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
)
from albion_crafter.market.pricing import PriceResolver, price_quality_reasons, resolve_price

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
CITY = "Bridgewatch"


def _history(
    item_id: str,
    prices: tuple[float, ...],
    *,
    volumes: tuple[int, ...] | None = None,
) -> tuple[MarketHistoryInterval, ...]:
    selected_volumes = volumes or tuple(20 for _ in prices)
    return tuple(
        MarketHistoryInterval(
            item_id=item_id,
            city=CITY,
            quality=1,
            region=Region.AMERICAS,
            observed_at=NOW - timedelta(days=offset),
            item_count=volume,
            average_price=price,
            time_scale=HistoryTimeScale.DAILY,
            fetched_at=NOW,
        )
        for offset, (price, volume) in enumerate(
            zip(prices, selected_volumes, strict=True), start=1
        )
    )


def _current(
    item_id: str,
    *,
    sell: int | None,
    buy: int | None = None,
    observed_at: datetime | None = NOW - timedelta(minutes=5),
) -> MarketPrice:
    return MarketPrice(
        item_id=item_id,
        city=CITY,
        quality=1,
        region=Region.AMERICAS,
        sell_price=sell,
        sell_price_timestamp=observed_at if sell else None,
        buy_price=buy,
        buy_price_timestamp=observed_at if buy else None,
        fetched_at=NOW,
    )


def _resolve(
    item_id: str,
    *,
    current: MarketPrice | None = None,
    history: tuple[MarketHistoryInterval, ...] = (),
    side: MarketSide = MarketSide.SELL_ORDER,
):
    return resolve_price(
        item_id=item_id,
        city=CITY,
        quality=1,
        side=side,
        role="test",
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
        market_price=current,
        history=history,
    )


def test_current_t5_teasel_sell_wins_over_history() -> None:
    item_id = "T5_TEASEL"
    line = _resolve(
        item_id,
        current=_current(item_id, sell=321),
        history=_history(item_id, (900, 910, 920, 930, 940, 950, 960)),
    )

    assert line.price == 321
    assert line.source is MarketPriceSource.CURRENT
    assert line.confidence is PriceConfidence.LIVE
    assert line.historical_reference_price is None


@pytest.mark.parametrize("item_id", ["T5_POTION_ACID", "T5_ALCHEMY_RARE_DIREBEAR"])
def test_zero_current_and_recent_history_resolve_to_labeled_estimate(item_id: str) -> None:
    line = _resolve(
        item_id,
        current=_current(item_id, sell=0, buy=0),
        history=_history(item_id, (10_000, 10_200, 9_900, 10_100, 10_050, 10_150, 10_000)),
    )

    assert line.price == pytest.approx(10_050)
    assert line.source is MarketPriceSource.HISTORICAL_ESTIMATE
    assert line.confidence is PriceConfidence.HIGH
    assert line.current_price is None
    assert line.historical_days_used == 7
    assert line.historical_total_volume == 140
    reasons = price_quality_reasons(line)
    assert reasons[0].code is ReasonCode.HISTORICAL_PRICE_ESTIMATE
    assert reasons[0].severity is ReasonSeverity.WARNING


def test_missing_t4_milk_current_and_history_remains_missing() -> None:
    line = _resolve("T4_MILK")

    assert line.price is None
    assert line.source is MarketPriceSource.MISSING
    assert line.confidence is PriceConfidence.MISSING


def test_missing_buy_remains_missing_even_when_sell_history_exists() -> None:
    item_id = "T5_POTION_ACID"
    line = _resolve(
        item_id,
        current=_current(item_id, sell=0, buy=0),
        history=_history(item_id, (10_000,) * 7),
        side=MarketSide.BUY_ORDER,
    )

    assert line.price is None
    assert line.source is MarketPriceSource.MISSING
    assert line.historical_reference_price is None


def test_one_bizarre_t4_burdock_history_spike_does_not_dominate_estimate() -> None:
    estimate = estimate_historical_sell_price(
        _history("T4_BURDOCK", (100, 102, 99, 101, 100, 98, 50_000)),
        as_of=NOW,
    )

    assert estimate is not None
    assert 98 <= estimate.reference_price <= 102
    assert estimate.outliers_ignored == 1


def test_low_volume_dragon_teasel_history_has_low_confidence() -> None:
    estimate = estimate_historical_sell_price(
        _history("T5_TEASEL", (400, 405, 395), volumes=(1, 1, 1)),
        as_of=NOW,
    )

    assert estimate is not None
    assert estimate.confidence is PriceConfidence.LOW
    assert estimate.average_daily_volume_7d == pytest.approx(3 / 7)


def test_recent_history_replaces_stale_current_but_preserves_raw_stale_evidence() -> None:
    item_id = "T5_ALCHEMY_RARE_DIREBEAR"
    stale = NOW - timedelta(days=3)
    line = _resolve(
        item_id,
        current=_current(item_id, sell=50_000, observed_at=stale),
        history=_history(item_id, (9_000, 9_100, 9_200, 9_050, 9_150)),
    )

    assert line.source is MarketPriceSource.HISTORICAL_ESTIMATE
    assert line.price == pytest.approx(9_100)
    assert line.current_price == 50_000
    assert line.current_timestamp == stale
    assert line.current_freshness is Freshness.STALE
    assert line.current_is_stale


def test_acid_recipe_resolves_three_current_and_two_history_prices(tmp_path) -> None:
    database = Database(tmp_path / "acid-resolution.db")
    database.initialize()
    prices = MarketPriceRepository(database, wall_clock=lambda: NOW)
    history = MarketHistoryRepository(database)
    current_values = {
        "T5_TEASEL": 200,
        "T4_BURDOCK": 100,
        "T4_MILK": 50,
    }
    prices.upsert_many(
        [
            _current(item_id, sell=current_values.get(item_id))
            for item_id in (
                "T5_POTION_ACID",
                "T5_ALCHEMY_RARE_DIREBEAR",
                "T5_TEASEL",
                "T4_BURDOCK",
                "T4_MILK",
            )
        ]
    )
    history.upsert_many(
        (
            *_history("T5_POTION_ACID", (10_000,) * 7),
            *_history("T5_ALCHEMY_RARE_DIREBEAR", (8_000,) * 7),
        )
    )
    recipe = Recipe(
        output=Item("T5_POTION_ACID", "Acid Potion", 5),
        output_quantity=10,
        item_value=336,
        materials=(
            MaterialRequirement("T5_ALCHEMY_RARE_DIREBEAR", 1),
            MaterialRequirement("T5_TEASEL", 48),
            MaterialRequirement("T4_BURDOCK", 24),
            MaterialRequirement("T4_MILK", 12),
        ),
    )
    snapshot = PriceResolver(prices, history=history).resolve(
        recipe,
        buy_city=CITY,
        sell_city=CITY,
        region=Region.AMERICAS,
        quality=1,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert snapshot.material_prices == {
        "T5_ALCHEMY_RARE_DIREBEAR": 8_000,
        "T5_TEASEL": 200,
        "T4_BURDOCK": 100,
        "T4_MILK": 50,
    }
    assert snapshot.output_price == 10_000
    assert snapshot.live_price_count == 3
    assert snapshot.historical_estimate_count == 2
    assert snapshot.actionability.is_actionable
