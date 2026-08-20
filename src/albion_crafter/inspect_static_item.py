from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from albion_crafter.data.static_importer import StaticCatalogParser, StaticDataError
from albion_crafter.database.catalog import CatalogImport, CatalogRepository
from albion_crafter.database.database import Database, default_data_directory, default_database_path


def _number(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def _raw_source_evidence(
    cache_directory: Path,
    metadata: CatalogImport,
    item_id: str,
) -> dict[str, Any]:
    raw_path = cache_directory / metadata.source_version / "items.json"
    evidence: dict[str, Any] = {
        "cache_path": str(raw_path),
        "cache_available": raw_path.is_file(),
    }
    if not raw_path.is_file():
        return evidence
    try:
        decoded = json.loads(raw_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        evidence["cache_error"] = str(exc)
        return evidence
    if not isinstance(decoded, dict) or not isinstance(decoded.get("items"), dict):
        evidence["cache_error"] = "cached items.json has an unexpected schema"
        return evidence

    records, _ = StaticCatalogParser._root_item_records(decoded["items"])
    canonical_by_raw_id = {
        str(record["@uniquename"]): StaticCatalogParser._canonical_id(
            str(record["@uniquename"]), record.get("@enchantmentlevel")
        )
        for _, record in records
    }
    selected: tuple[str, dict[str, Any], dict[str, Any] | None, int | None] | None = None
    for item_type, record in records:
        raw_id = str(record["@uniquename"])
        if canonical_by_raw_id[raw_id] == item_id:
            requirement, _ = StaticCatalogParser._select_recipe(record.get("craftingrequirements"))
            selected = (item_type, record, requirement, None)
            break
        enchantments = record.get("enchantments")
        if not isinstance(enchantments, dict):
            continue
        for enchantment in _as_list(enchantments.get("enchantment")):
            if not isinstance(enchantment, dict):
                continue
            if (
                StaticCatalogParser._canonical_id(raw_id, enchantment.get("@enchantmentlevel"))
                != item_id
            ):
                continue
            requirement, _ = StaticCatalogParser._select_recipe(
                enchantment.get("craftingrequirements")
            )
            selected = (
                item_type,
                record,
                requirement,
                StaticCatalogParser._bounded_optional_int(
                    enchantment.get("@enchantmentlevel"), minimum=1, maximum=4
                ),
            )
            break
        if selected is not None:
            break

    if selected is None:
        evidence["raw_record_found"] = False
        return evidence

    item_type, record, requirement, variant_level = selected
    raw_value_present = (
        variant_level is None
        and "@itemvalue" in record
        and record.get("@itemvalue") not in (None, "")
    )
    evidence.update(
        {
            "raw_record_found": True,
            "raw_item_type": item_type,
            "raw_variant_enchantment": variant_level,
            "raw_fields": {
                field: record.get(field)
                for field in (
                    "@uniquename",
                    "@tier",
                    "@enchantmentlevel",
                    "@itemvalue",
                    "@shopcategory",
                    "@shopsubcategory1",
                    "@craftingcategory",
                )
                if field in record
            },
            "direct_item_value_present": raw_value_present,
        }
    )
    if requirement is not None:
        ingredients = []
        for row in _as_list(requirement.get("craftresource")):
            if not isinstance(row, dict) or not row.get("@uniquename"):
                continue
            raw_material_id = str(row["@uniquename"])
            ingredients.append(
                {
                    "item_id": canonical_by_raw_id.get(
                        raw_material_id,
                        StaticCatalogParser._canonical_id(
                            raw_material_id, row.get("@enchantmentlevel")
                        ),
                    ),
                    "count": row.get("@count"),
                    "max_return_amount": row.get("@maxreturnamount"),
                }
            )
        evidence["raw_recipe"] = {
            "output_quantity": requirement.get("@amountcrafted", "1"),
            "crafting_focus": requirement.get("@craftingfocus"),
            "ingredients": ingredients,
        }
    return evidence


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def inspect_item(
    repository: CatalogRepository,
    cache_directory: Path,
    item_id: str,
) -> dict[str, Any]:
    clean_id = item_id.strip()
    record = repository.get_item(clean_id)
    if record is None:
        raise StaticDataError(f"Item {clean_id!r} is not present in the active static catalog")
    metadata = repository.import_metadata()
    if metadata is None:
        raise StaticDataError("No active static catalog metadata is available")
    recipe = repository.get_recipe(clean_id)
    source = _raw_source_evidence(cache_directory, metadata, clean_id)
    source["source_id"] = metadata.source_id
    source["source_url"] = metadata.source_url
    source["source_version"] = metadata.source_version
    source["item_value_resolution"] = (
        "direct_@itemvalue"
        if source.get("direct_item_value_present")
        else "recipe_derived"
        if record.item_value is not None
        else "unavailable"
    )

    result: dict[str, Any] = {
        "item": {
            "item_id": record.item.item_id,
            "display_name": record.item.display_name,
            "tier": record.item.tier,
            "enchantment": record.item.enchantment,
            "category": record.item.category,
            "subcategory": record.item.subcategory,
            "crafting_category": record.item.crafting_category,
            "item_value": _number(record.item_value),
            "craftable": record.craftable,
            "provenance": record.provenance.value,
        },
        "recipe": None,
        "source": source,
    }
    if recipe is not None:
        ingredients = []
        for material in recipe.materials:
            material_record = repository.get_item(material.item_id)
            ingredients.append(
                {
                    "item_id": material.item_id,
                    "display_name": (
                        material_record.item.display_name if material_record is not None else None
                    ),
                    "quantity": _number(float(material.quantity)),
                    "returnable": material.returnable,
                }
            )
        result["recipe"] = {
            "output_quantity": recipe.output_quantity,
            "base_focus_cost": _number(recipe.base_focus_cost),
            "recipe_ambiguous": recipe.recipe_ambiguous,
            "ingredients": ingredients,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect one item in Albion Crafter's active imported static catalog."
    )
    parser.add_argument("item_id", help="Canonical Albion item ID, for example T5_POTION_ACID")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument(
        "--cache-directory",
        type=Path,
        default=default_data_directory() / "static-cache",
    )
    arguments = parser.parse_args(argv)
    database = Database(arguments.database)
    database.initialize()
    try:
        diagnostic = inspect_item(
            CatalogRepository(database), arguments.cache_directory, arguments.item_id
        )
    except StaticDataError as exc:
        print(f"Static item inspection failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(diagnostic, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
