"""Regression-sized scan without network or per-scenario database work."""

from datetime import UTC, datetime, timedelta

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.market.models import MarketPrice, Region
from albion_crafter.opportunity.models import ScanConstraints
from albion_crafter.opportunity.scanner import OpportunityScanner


def test_two_thousand_recipe_scan_evaluates_eighteen_thousand_scenarios() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    cities = ("Lymhurst", "Thetford", "Bridgewatch")
    sell_cities = ("Bridgewatch", "Martlock", "Fort Sterling")
    recipes: list[Recipe] = []
    prices: list[MarketPrice] = []
    for index in range(2_000):
        output_id = f"T4_MAIN_SWORD_SYNTHETIC_{index}"
        material_id = f"T4_METALBAR_SYNTHETIC_{index}"
        recipes.append(
            Recipe(
                output=Item(
                    output_id,
                    f"Synthetic Sword {index}",
                    4,
                    crafting_category="sword",
                ),
                output_quantity=1,
                materials=(MaterialRequirement(material_id, 16, True),),
                item_value=100,
                base_focus_cost=200,
                provenance=Provenance.STATIC_GAME_DATA,
            )
        )
        for city in cities:
            prices.append(_price(material_id, city, 100 + index % 10, now))
        for city in sell_cities:
            prices.append(_price(output_id, city, 2_500 + index % 100, now))
    station_fees = tuple(
        StationFeeObservation(
            Region.AMERICAS.value,
            city,
            StationType.WARRIORS_FORGE,
            500,
            now,
        )
        for city in cities
    )

    snapshot = OpportunityScanner().scan(
        recipes,
        prices,
        (),
        station_fees,
        CraftingSkillProfile(),
        ScanConstraints(
            region=Region.AMERICAS,
            craft_cities=cities,
            sell_cities=sell_cities,
            actionable_only=True,
            maximum_price_age=timedelta(hours=4),
        ),
        as_of=now,
    )

    assert snapshot.recipes_considered == 2_000
    assert snapshot.scenarios_evaluated == 18_000
    assert snapshot.actionable_count == 18_000
    assert len(snapshot.opportunities) == 18_000
    assert snapshot.database_load_operations == 0
    # Runtime is recorded for diagnostics rather than constrained by a flaky microbenchmark.
    assert snapshot.elapsed_seconds > 0


def _price(item_id: str, city: str, price: int, now: datetime) -> MarketPrice:
    return MarketPrice(
        item_id=item_id,
        city=city,
        quality=1,
        region=Region.AMERICAS,
        sell_price=price,
        sell_price_timestamp=now - timedelta(minutes=2),
        buy_price=price - 1,
        buy_price_timestamp=now - timedelta(minutes=2),
        fetched_at=now,
        provenance=Provenance.AODP_CACHED,
    )
