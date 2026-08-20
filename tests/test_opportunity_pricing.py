from datetime import UTC, datetime, timedelta

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.market.models import (
    Freshness,
    FreshnessPolicy,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)
from albion_crafter.opportunity.pricing import PricingIndex

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _recipe() -> Recipe:
    return Recipe(
        output=Item("OUTPUT", "Output", 4, crafting_category="bag"),
        output_quantity=1,
        materials=(
            MaterialRequirement("FRESH_MAT", 2, True),
            MaterialRequirement("STALE_MAT", 1, False),
        ),
        provenance=Provenance.STATIC_GAME_DATA,
    )


def _price(item_id: str, city: str, price: int, observed_at: datetime) -> MarketPrice:
    return MarketPrice(
        item_id=item_id,
        city=city,
        quality=1,
        region=Region.AMERICAS,
        sell_price=price,
        sell_price_timestamp=observed_at,
        buy_price=max(price - 1, 0),
        buy_price_timestamp=observed_at,
        fetched_at=NOW,
        provenance=Provenance.AODP_CACHED,
    )


def test_pricing_index_uses_preloaded_rows_and_one_scan_clock() -> None:
    index = PricingIndex(
        (
            _price("FRESH_MAT", "Thetford", 100, NOW - timedelta(minutes=5)),
            _price("STALE_MAT", "Thetford", 200, NOW - timedelta(hours=5)),
            _price("OUTPUT", "Bridgewatch", 1_000, NOW - timedelta(minutes=2)),
        )
    )
    snapshot = index.resolve(
        _recipe(),
        material_city="Thetford",
        craft_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert snapshot.material_prices == {"FRESH_MAT": 100.0, "STALE_MAT": 200.0}
    assert snapshot.output_price == 1_000
    assert snapshot.freshness is Freshness.STALE
    assert snapshot.output_age(NOW) == timedelta(minutes=2)
    assert snapshot.oldest_material_age(NOW) == timedelta(hours=5)
    assert snapshot.oldest_required_age(NOW) == timedelta(hours=5)
    assert any("STALE_MAT" in reason.message for reason in snapshot.data_quality_reasons)


def test_pricing_index_keeps_material_and_sell_cities_independent() -> None:
    index = PricingIndex(
        (
            _price("FRESH_MAT", "Lymhurst", 100, NOW),
            _price("STALE_MAT", "Lymhurst", 200, NOW),
            _price("OUTPUT", "Martlock", 1_000, NOW),
        )
    )
    snapshot = index.resolve(
        _recipe(),
        material_city="Lymhurst",
        craft_city="Thetford",
        sell_city="Martlock",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert {line.city for line in snapshot.evidence if line.role == "material"} == {"Lymhurst"}
    assert next(line.city for line in snapshot.evidence if line.role == "output") == "Martlock"
    assert snapshot.returned_material_craft_city_prices == {}


def test_optional_craft_city_return_price_does_not_change_required_freshness() -> None:
    index = PricingIndex(
        (
            _price("FRESH_MAT", "Lymhurst", 100, NOW),
            _price("STALE_MAT", "Lymhurst", 200, NOW),
            _price("FRESH_MAT", "Thetford", 350, NOW - timedelta(hours=5)),
            _price("OUTPUT", "Martlock", 1_000, NOW),
        )
    )
    snapshot = index.resolve(
        _recipe(),
        material_city="Lymhurst",
        craft_city="Thetford",
        sell_city="Martlock",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
        include_returned_material_prices=True,
    )

    assert snapshot.returned_material_craft_city_prices == {"FRESH_MAT": 350.0}
    informational = next(
        line for line in snapshot.evidence if line.role == "returned_material_informational"
    )
    assert informational.city == "Thetford"
    assert informational.freshness is Freshness.STALE
    assert snapshot.freshness is Freshness.FRESH
    assert snapshot.oldest_required_timestamp == NOW
    assert not any(
        reason.code.value in {"stale_price", "unknown_timestamp"}
        for reason in snapshot.data_quality_reasons
    )


def test_pricing_index_override_wins_without_mutating_cached_row() -> None:
    cached = _price("OUTPUT", "Bridgewatch", 1_000, NOW)
    override = UserPriceOverride(
        item_id="OUTPUT",
        city="Bridgewatch",
        quality=1,
        region=Region.AMERICAS,
        side=MarketSide.SELL_ORDER,
        price=1_500,
        entered_at=NOW - timedelta(minutes=1),
    )
    index = PricingIndex(
        (
            _price("FRESH_MAT", "Thetford", 100, NOW),
            _price("STALE_MAT", "Thetford", 200, NOW),
            cached,
        ),
        (override,),
    )
    snapshot = index.resolve(
        _recipe(),
        material_city="Thetford",
        craft_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert snapshot.output_price == 1_500
    assert next(line for line in snapshot.evidence if line.role == "output").provenance is (
        Provenance.USER_OVERRIDE
    )
    assert cached.sell_price == 1_000


def test_pricing_index_missing_prices_remain_missing() -> None:
    snapshot = PricingIndex(()).resolve(
        _recipe(),
        material_city="Thetford",
        craft_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert snapshot.material_prices == {"FRESH_MAT": None, "STALE_MAT": None}
    assert snapshot.output_price is None
    assert snapshot.oldest_required_timestamp is None


def test_one_missing_required_timestamp_makes_aggregate_age_unknown() -> None:
    snapshot = PricingIndex(
        (
            _price("FRESH_MAT", "Thetford", 100, NOW),
            _price("OUTPUT", "Bridgewatch", 1_000, NOW),
        )
    ).resolve(
        _recipe(),
        material_city="Thetford",
        craft_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert snapshot.output_timestamp == NOW
    assert snapshot.oldest_material_timestamp is None
    assert snapshot.oldest_required_timestamp is None
    assert snapshot.freshness is Freshness.UNKNOWN


def test_nonpositive_cached_prices_are_missing_not_free_or_executable() -> None:
    zero_material = _price("FRESH_MAT", "Thetford", 0, NOW)
    zero_output = _price("OUTPUT", "Bridgewatch", 0, NOW)
    snapshot = PricingIndex(
        (
            zero_material,
            _price("STALE_MAT", "Thetford", 200, NOW),
            zero_output,
        )
    ).resolve(
        _recipe(),
        material_city="Thetford",
        craft_city="Thetford",
        sell_city="Bridgewatch",
        region=Region.AMERICAS,
        output_quality=1,
        material_side=MarketSide.SELL_ORDER,
        output_side=MarketSide.SELL_ORDER,
        freshness_policy=FreshnessPolicy(timedelta(hours=4)),
        as_of=NOW,
    )

    assert snapshot.material_prices["FRESH_MAT"] is None
    assert snapshot.output_price is None
    assert snapshot.oldest_required_timestamp is None
