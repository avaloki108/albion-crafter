from datetime import UTC, datetime, timedelta

from albion_crafter.core.actionability import ReasonCode
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.market.models import (
    Freshness,
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)
from albion_crafter.market.pricing import PriceResolver


def test_price_resolver_keeps_stale_scenario_actionable_without_hiding_age(tmp_path) -> None:
    database = Database(tmp_path / "pricing.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    now = datetime.now(UTC)
    old = now - timedelta(hours=6)
    repository.upsert_many(
        [
            MarketPrice(
                item_id=item_id,
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=price,
                sell_price_timestamp=old,
                buy_price=price - 10,
                buy_price_timestamp=old,
                fetched_at=now,
            )
            for item_id, price in (("MAT", 100), ("OUT", 1_000))
        ]
    )
    recipe = Recipe(
        output=Item("OUT", "Output", 4),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 2),),
    )
    snapshot = PriceResolver(repository).resolve(
        recipe,
        buy_city="Bridgewatch",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=1,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
    )

    assert snapshot.material_prices == {"MAT": 100}
    assert snapshot.output_price == 1_000
    assert snapshot.freshness is Freshness.STALE
    assert snapshot.oldest_timestamp == old
    assert ReasonCode.STALE_PRICE in {reason.code for reason in snapshot.actionability.reasons}
    assert snapshot.actionability.is_actionable
    assert snapshot.actionability.blocking_reasons == ()


def test_price_resolver_keeps_future_values_visible_but_never_actionable(tmp_path) -> None:
    database = Database(tmp_path / "future-pricing.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    future = now + timedelta(minutes=5)
    repository.upsert_many(
        [
            MarketPrice(
                item_id=item_id,
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=price,
                sell_price_timestamp=future,
                buy_price=price - 10,
                buy_price_timestamp=future,
                fetched_at=now,
            )
            for item_id, price in (("MAT", 100), ("OUT", 1_000))
        ]
    )
    recipe = Recipe(
        output=Item("OUT", "Output", 4),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 2),),
    )

    snapshot = PriceResolver(repository).resolve(
        recipe,
        buy_city="Bridgewatch",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=1,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=now,
    )

    assert snapshot.output_price == 1_000
    assert snapshot.freshness is Freshness.FUTURE
    assert snapshot.age_seconds == -300
    assert ReasonCode.FUTURE_TIMESTAMP in {reason.code for reason in snapshot.actionability.reasons}
    assert not snapshot.actionability.is_actionable


def test_unknown_timestamp_is_explicit_even_when_price_exists(tmp_path) -> None:
    database = Database(tmp_path / "unknown.db")
    database.initialize()
    repository = MarketPriceRepository(database)
    now = datetime.now(UTC)
    repository.upsert_many(
        [
            MarketPrice(
                item_id=item_id,
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=price,
                sell_price_timestamp=None,
                buy_price=None,
                buy_price_timestamp=None,
                fetched_at=now,
            )
            for item_id, price in (("MAT", 100), ("OUT", 1_000))
        ]
    )
    recipe = Recipe(
        output=Item("OUT", "Output", 4),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 2),),
    )
    snapshot = PriceResolver(repository).resolve(
        recipe,
        buy_city="Bridgewatch",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=1,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
    )
    assert snapshot.output_price == 1_000
    assert snapshot.oldest_timestamp is None
    assert ReasonCode.UNKNOWN_TIMESTAMP in {
        reason.code for reason in snapshot.actionability.reasons
    }
    assert snapshot.actionability.is_actionable


def test_user_override_wins_without_overwriting_aodp_and_is_removable(tmp_path) -> None:
    database = Database(tmp_path / "override.db")
    database.initialize()
    prices = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    now = datetime.now(UTC)
    prices.upsert_many(
        [
            MarketPrice(
                item_id=item_id,
                city="Bridgewatch",
                quality=1,
                region=Region.AMERICAS,
                sell_price=sell,
                sell_price_timestamp=now,
                buy_price=buy,
                buy_price_timestamp=now,
                fetched_at=now,
            )
            for item_id, sell, buy in (("MAT", 100, 90), ("OUT", 1_000, 800))
        ]
    )
    overrides.set(
        UserPriceOverride(
            item_id="OUT",
            city="Bridgewatch",
            quality=1,
            region=Region.AMERICAS,
            side=MarketSide.BUY_ORDER,
            price=950,
            entered_at=now,
        )
    )
    recipe = Recipe(
        output=Item("OUT", "Output", 4),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 2),),
    )
    resolver = PriceResolver(prices, overrides)
    kwargs = dict(
        buy_city="Bridgewatch",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=1,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        output_side=MarketSide.BUY_ORDER,
    )
    snapshot = resolver.resolve(recipe, **kwargs)
    output = next(line for line in snapshot.resolved_prices if line.role == "output")
    assert snapshot.output_price == 950
    assert output.provenance is Provenance.USER_OVERRIDE
    cached = prices.get("OUT", "Bridgewatch", 1, Region.AMERICAS)
    assert cached is not None and cached.buy_price == 800

    assert overrides.remove("OUT", "Bridgewatch", 1, Region.AMERICAS, MarketSide.BUY_ORDER)
    assert resolver.resolve(recipe, **kwargs).output_price == 800


def test_missing_output_and_material_sides_remain_visible(tmp_path) -> None:
    database = Database(tmp_path / "missing-pricing.db")
    database.initialize()
    recipe = Recipe(
        output=Item("OUT", "Output", 4),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 2),),
    )
    snapshot = PriceResolver(MarketPriceRepository(database)).resolve(
        recipe,
        buy_city="Bridgewatch",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        quality=1,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
    )
    assert snapshot.material_prices == {"MAT": None}
    assert snapshot.output_price is None
    assert len(snapshot.resolved_prices) == 2
