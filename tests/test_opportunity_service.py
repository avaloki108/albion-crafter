from datetime import UTC, datetime, timedelta

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import CraftingProfileRepository, StationFeeRepository
from albion_crafter.market.models import MarketPrice, Region
from albion_crafter.opportunity.models import CancellationToken, ScanConstraints
from albion_crafter.opportunity.service import OpportunityScannerService


class CountingDatabase(Database):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.read_statements = 0

    def _open_connection(self):
        connection = super()._open_connection()

        def count(statement: str) -> None:
            operation = statement.lstrip().split(None, 1)[0].upper()
            if operation in {"SELECT", "WITH"}:
                self.read_statements += 1

        connection.set_trace_callback(count)
        return connection


def test_service_bulk_load_count_does_not_scale_with_scenarios(tmp_path) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    database = CountingDatabase(tmp_path / "scanner.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    station_fees = StationFeeRepository(database)
    profiles = CraftingProfileRepository(database)
    recipes: list[Recipe] = []
    catalog_items: list[CatalogItem] = []
    prices: list[MarketPrice] = []
    for index in range(120):
        output = Item(
            f"T4_MAIN_SWORD_SERVICE_{index}",
            f"Service Sword {index}",
            4,
            crafting_category="sword",
        )
        material = Item(f"T4_METALBAR_SERVICE_{index}", f"Bar {index}", 4)
        recipe = Recipe(
            output,
            1,
            (MaterialRequirement(material.item_id, 16, True),),
            item_value=100,
            base_focus_cost=200,
            provenance=Provenance.STATIC_GAME_DATA,
            source_version="service-test",
        )
        recipes.append(recipe)
        catalog_items.extend(
            (
                CatalogItem(
                    output,
                    100,
                    True,
                    Provenance.STATIC_GAME_DATA,
                    "service-test",
                ),
                CatalogItem(
                    material,
                    10,
                    False,
                    Provenance.STATIC_GAME_DATA,
                    "service-test",
                ),
            )
        )
        for city in ("Lymhurst", "Thetford"):
            prices.append(_price(material.item_id, city, 100, now))
        for city in ("Bridgewatch", "Martlock"):
            prices.append(_price(output.item_id, city, 2_500, now))
    catalog.replace_all(
        catalog_items,
        recipes,
        CatalogImport(
            "test",
            "memory://test",
            "service-test",
            now,
            now,
            len(catalog_items),
            len(recipes),
        ),
    )
    market.upsert_many(prices)
    for city in ("Lymhurst", "Thetford"):
        station_fees.set(
            StationFeeObservation(
                Region.AMERICAS.value,
                city,
                StationType.WARRIORS_FORGE,
                500,
                now,
            )
        )
    profiles.save(CraftingSkillProfile())
    database.read_statements = 0

    service = OpportunityScannerService(
        catalog,
        market,
        overrides,
        station_fees,
        profiles,
    )
    constraints = ScanConstraints(
        region=Region.AMERICAS,
        craft_cities=("Lymhurst", "Thetford"),
        sell_cities=("Bridgewatch", "Martlock"),
        material_city="Thetford",
        actionable_only=True,
        maximum_price_age=timedelta(hours=4),
    )
    snapshot = service.scan(
        constraints,
        as_of=now,
    )

    assert snapshot.recipes_considered == 120
    assert snapshot.scenarios_evaluated == 480
    assert len(snapshot.opportunities) == 480
    assert all(
        opportunity.calculation.returned_material_craft_city_market_value is not None
        for opportunity in snapshot.opportunities
    )
    assert snapshot.database_load_operations == database.read_statements == 8

    cancellation = CancellationToken()
    cancellation.cancel()
    database.read_statements = 0
    cancelled = service.scan(constraints, as_of=now, cancellation=cancellation)
    assert cancelled.cancelled
    assert cancelled.scenarios_evaluated == 0
    assert cancelled.database_load_operations == database.read_statements == 0


def _price(item_id: str, city: str, value: int, now: datetime) -> MarketPrice:
    return MarketPrice(
        item_id=item_id,
        city=city,
        quality=1,
        region=Region.AMERICAS,
        sell_price=value,
        sell_price_timestamp=now - timedelta(minutes=1),
        buy_price=value - 1,
        buy_price_timestamp=now - timedelta(minutes=1),
        fetched_at=now,
        provenance=Provenance.AODP_CACHED,
    )
