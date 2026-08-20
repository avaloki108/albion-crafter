from datetime import UTC, datetime
from pathlib import Path

import pytest

from albion_crafter.data.static_importer import StaticCatalogParser, StaticDataError
from albion_crafter.database.catalog import CatalogImport, CatalogRepository
from albion_crafter.database.database import Database
from albion_crafter.inspect_static_item import inspect_item

ACID_UPSTREAM_FIXTURE = Path(__file__).parent / "fixtures" / "acid-potion-upstream-contract"


def _acid_payload() -> tuple[bytes, bytes]:
    return (
        (ACID_UPSTREAM_FIXTURE / "items.json").read_bytes(),
        (ACID_UPSTREAM_FIXTURE / "formatted-items.json").read_bytes(),
    )


def test_item_diagnostic_explains_derived_acid_value_and_exact_recipe(tmp_path) -> None:
    raw, formatted = _acid_payload()
    version = "a" * 40
    parsed = StaticCatalogParser().parse(raw, formatted, source_version=version)
    database = Database(tmp_path / "catalog.db")
    database.initialize()
    repository = CatalogRepository(database)
    repository.replace_all(
        parsed.items,
        parsed.recipes,
        CatalogImport(
            "ao-data/ao-bin-dumps",
            "https://github.com/ao-data/ao-bin-dumps",
            version,
            None,
            datetime(2026, 8, 20, tzinfo=UTC),
            len(parsed.items),
            len(parsed.recipes),
        ),
    )
    cache = tmp_path / "cache" / version
    cache.mkdir(parents=True)
    (cache / "items.json").write_bytes(raw)

    diagnostic = inspect_item(repository, tmp_path / "cache", "T5_POTION_ACID")

    assert diagnostic["item"] == {
        "item_id": "T5_POTION_ACID",
        "display_name": "Acid Potion",
        "tier": 5,
        "enchantment": 0,
        "category": "consumables",
        "subcategory": "potions",
        "crafting_category": "potion",
        "item_value": 336,
        "craftable": True,
        "provenance": "static_game_data",
    }
    assert diagnostic["source"]["direct_item_value_present"] is False
    assert diagnostic["source"]["item_value_resolution"] == "recipe_derived"
    assert diagnostic["source"]["raw_recipe"]["output_quantity"] == "10"
    assert [
        (row["item_id"], row["display_name"], row["quantity"], row["returnable"])
        for row in diagnostic["recipe"]["ingredients"]
    ] == [
        ("T5_ALCHEMY_RARE_DIREBEAR", "Fine Spirit Paws", 1, False),
        ("T5_TEASEL", "Dragon Teasel", 48, True),
        ("T4_BURDOCK", "Crenellated Burdock", 24, True),
        ("T4_MILK", "Goat's Milk", 12, True),
    ]


def test_item_diagnostic_rejects_unknown_item(tmp_path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    with pytest.raises(StaticDataError, match="not present"):
        inspect_item(CatalogRepository(database), tmp_path / "cache", "NOT_REAL")
