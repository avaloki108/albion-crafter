from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.database.catalog import (
    CatalogImport,
    CatalogImportReport,
    CatalogItem,
    CatalogRepository,
)

SOURCE_ID = "ao-data/ao-bin-dumps"
SOURCE_URL = "https://github.com/ao-data/ao-bin-dumps"
LATEST_COMMIT_URL = "https://api.github.com/repos/ao-data/ao-bin-dumps/commits?per_page=1"
RAW_ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/{version}/items.json"
FORMATTED_ITEMS_URL = (
    "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/{version}/formatted/items.json"
)
IMPORTER_VERSION = 4
DEFAULT_SENTINEL_IDS = frozenset(
    {
        "T4_MAIN_SWORD",
        "T4_METALBAR",
        "T4_BAG",
        "T4_MEAL_SANDWICH",
        "T4_POTION_HEAL",
    }
)


class StaticDataError(RuntimeError):
    """A recoverable static-data download or parsing failure."""


StaticTransport = Callable[[str, float], bytes]


def _default_transport(url: str, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "AlbionCrafter/0.6.2"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS hosts
        return response.read()


@dataclass(frozen=True, slots=True)
class StaticDataRelease:
    version: str
    source_timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class ParsedCatalog:
    items: list[CatalogItem]
    recipes: list[Recipe]
    diagnostics: ParseDiagnostics


@dataclass(frozen=True, slots=True)
class ParseDiagnostics:
    ingredient_count: int
    unknown_returnability_count: int
    skipped_malformed_count: int
    structural_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StaticValidationPolicy:
    minimum_items: int = 5_000
    minimum_recipes: int = 3_000
    minimum_ingredients: int = 5_000
    maximum_relative_drop: float = 0.40
    sentinel_ids: frozenset[str] = DEFAULT_SENTINEL_IDS

    def __post_init__(self) -> None:
        if min(self.minimum_items, self.minimum_recipes, self.minimum_ingredients) < 0:
            raise ValueError("static catalog minimum counts cannot be negative")
        if not 0 <= self.maximum_relative_drop < 1:
            raise ValueError("maximum_relative_drop must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class CatalogValidationResult:
    hard_errors: tuple[str, ...]
    soft_errors: tuple[str, ...]

    def accepted(self, *, force: bool) -> bool:
        return not self.hard_errors and (force or not self.soft_errors)

    @property
    def messages(self) -> tuple[str, ...]:
        return (*self.hard_errors, *self.soft_errors)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _optional_int(value: Any) -> int | None:
    try:
        parsed = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class StaticCatalogParser:
    """Convert pinned ao-bin-dumps JSON into the application's typed catalog."""

    def parse(
        self,
        raw_payload: bytes,
        formatted_payload: bytes,
        *,
        source_version: str,
    ) -> ParsedCatalog:
        try:
            raw = json.loads(raw_payload)
            formatted = json.loads(formatted_payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StaticDataError("Static item data is not valid JSON") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), dict):
            raise StaticDataError("Raw static dataset has an unexpected schema")
        if not isinstance(formatted, list):
            raise StaticDataError("Formatted static dataset has an unexpected schema")

        names = self._localized_names(formatted)
        records, skipped_roots = self._root_item_records(raw["items"])
        structural_errors: list[str] = []
        canonical_by_raw_id: dict[str, str] = {}
        for _, record in records:
            raw_id = str(record["@uniquename"])
            self._validate_item_numeric_fields(raw_id, record, structural_errors)
            canonical_id = self._canonical_id(raw_id, record.get("@enchantmentlevel"))
            previous = canonical_by_raw_id.setdefault(raw_id, canonical_id)
            if previous != canonical_id:
                structural_errors.append(
                    f"Conflicting canonical identities for {raw_id}: {previous}, {canonical_id}."
                )
        direct_item_values: dict[str, float] = {}
        for _, record in records:
            raw_value = record.get("@itemvalue")
            if raw_value in (None, ""):
                continue
            value = self._finite_optional_float(raw_value)
            canonical_id = canonical_by_raw_id[str(record["@uniquename"])]
            if value is None or value < 0:
                structural_errors.append(
                    f"Item {canonical_id} has invalid Item Value {raw_value!r}."
                )
                continue
            direct_item_values[canonical_id] = value
        item_records: dict[str, CatalogItem] = {}
        recipe_specs: list[tuple[Item, dict[str, Any], bool]] = []

        for item_type, record in records:
            raw_id = str(record["@uniquename"])
            base_id = canonical_by_raw_id[raw_id]
            base_enchantment = (
                self._bounded_optional_int(record.get("@enchantmentlevel"), minimum=0, maximum=4)
                or 0
            )
            base_item = self._make_item(
                base_id,
                record,
                names,
                item_type=item_type,
                enchantment=base_enchantment,
            )
            base_recipe, base_ambiguous = self._select_recipe(record.get("craftingrequirements"))
            if base_id in item_records:
                structural_errors.append(f"Duplicate canonical item ID {base_id}.")
            item_records[base_id] = CatalogItem(
                item=base_item,
                item_value=direct_item_values.get(base_id),
                craftable=False,
                provenance=Provenance.STATIC_GAME_DATA,
                source_version=source_version,
            )
            if base_recipe is not None:
                recipe_specs.append((base_item, base_recipe, base_ambiguous))

            enchantments = record.get("enchantments")
            if not isinstance(enchantments, dict):
                continue
            for enchantment in _as_list(enchantments.get("enchantment")):
                if not isinstance(enchantment, dict):
                    continue
                raw_level = enchantment.get("@enchantmentlevel")
                level = self._bounded_optional_int(raw_level, minimum=1, maximum=4)
                if raw_level not in (None, "") and level is None:
                    structural_errors.append(
                        f"Item {base_id} has invalid enchantment level {raw_level!r}."
                    )
                if not level:
                    continue
                variant_id = self._canonical_id(raw_id, level)
                variant_item = self._make_item(
                    variant_id,
                    record,
                    names,
                    item_type=item_type,
                    enchantment=level,
                )
                variant_recipe, variant_ambiguous = self._select_recipe(
                    enchantment.get("craftingrequirements")
                )
                if variant_id in item_records:
                    structural_errors.append(f"Duplicate canonical item ID {variant_id}.")
                item_records[variant_id] = CatalogItem(
                    item=variant_item,
                    item_value=direct_item_values.get(variant_id),
                    craftable=False,
                    provenance=Provenance.STATIC_GAME_DATA,
                    source_version=source_version,
                )
                if variant_recipe is not None:
                    recipe_specs.append((variant_item, variant_recipe, variant_ambiguous))

        recipes: list[Recipe] = []
        skipped_malformed = skipped_roots
        unknown_returnability = 0
        for output, requirement, ambiguous in recipe_specs:
            materials, material_errors = self._materials(
                requirement, canonical_by_raw_id, output_item_id=output.item_id
            )
            if material_errors:
                structural_errors.extend(material_errors)
                skipped_malformed += 1
                continue
            if not materials:
                structural_errors.append(f"Recipe {output.item_id} has no valid ingredients.")
                skipped_malformed += 1
                continue
            raw_output_quantity = requirement.get("@amountcrafted")
            output_quantity = (
                1 if raw_output_quantity in (None, "") else _optional_int(raw_output_quantity)
            )
            if output_quantity is None or output_quantity <= 0:
                structural_errors.append(
                    f"Recipe {output.item_id} has invalid output quantity {raw_output_quantity!r}."
                )
                skipped_malformed += 1
                continue
            raw_focus = requirement.get("@craftingfocus")
            base_focus = self._finite_optional_float(raw_focus)
            if raw_focus not in (None, "") and (base_focus is None or base_focus < 0):
                structural_errors.append(
                    f"Recipe {output.item_id} has invalid Focus cost {raw_focus!r}."
                )
                skipped_malformed += 1
                continue
            recipes.append(
                Recipe(
                    output=output,
                    output_quantity=output_quantity,
                    materials=materials,
                    item_value=direct_item_values.get(output.item_id),
                    base_focus_cost=base_focus,
                    recipe_ambiguous=ambiguous,
                    provenance=Provenance.STATIC_GAME_DATA,
                    source_version=source_version,
                )
            )
            unknown_returnability += sum(material.returnable is None for material in materials)

        resolved_item_values = self._resolve_item_values(recipes, direct_item_values)
        recipes = [
            replace(recipe, item_value=resolved_item_values.get(recipe.output.item_id))
            for recipe in recipes
        ]
        for recipe in recipes:
            existing = item_records[recipe.output.item_id]
            item_records[recipe.output.item_id] = CatalogItem(
                item=existing.item,
                item_value=recipe.item_value,
                craftable=True,
                provenance=existing.provenance,
                source_version=existing.source_version,
            )

        diagnostics = ParseDiagnostics(
            ingredient_count=sum(len(recipe.materials) for recipe in recipes),
            unknown_returnability_count=unknown_returnability,
            skipped_malformed_count=skipped_malformed,
            structural_errors=tuple(structural_errors),
        )
        return ParsedCatalog(list(item_records.values()), recipes, diagnostics)

    @staticmethod
    def _localized_names(formatted: list[Any]) -> dict[str, str]:
        names: dict[str, str] = {}
        for row in formatted:
            if not isinstance(row, dict) or not row.get("UniqueName"):
                continue
            localized = row.get("LocalizedNames")
            if isinstance(localized, dict) and localized.get("EN-US"):
                names[str(row["UniqueName"])] = str(localized["EN-US"])
        return names

    @staticmethod
    def _root_item_records(
        items: dict[str, Any],
    ) -> tuple[list[tuple[str, dict[str, Any]]], int]:
        records: list[tuple[str, dict[str, Any]]] = []
        skipped = 0
        for item_type, value in items.items():
            if item_type.startswith("@") or item_type == "shopcategories":
                continue
            for record in _as_list(value):
                if isinstance(record, dict) and record.get("@uniquename"):
                    records.append((item_type, record))
                else:
                    skipped += 1
        return records, skipped

    @staticmethod
    def _canonical_id(raw_id: str, enchantment: Any) -> str:
        level = _optional_int(enchantment) or 0
        if level > 0 and "@" not in raw_id:
            return f"{raw_id}@{level}"
        return raw_id

    @staticmethod
    def _bounded_optional_int(
        value: Any,
        *,
        minimum: int,
        maximum: int,
    ) -> int | None:
        parsed = _optional_int(value)
        return parsed if parsed is not None and minimum <= parsed <= maximum else None

    @classmethod
    def _validate_item_numeric_fields(
        cls,
        item_id: str,
        record: dict[str, Any],
        errors: list[str],
    ) -> None:
        for field, label, minimum, maximum in (
            ("@tier", "tier", 1, 8),
            ("@enchantmentlevel", "enchantment level", 0, 4),
            ("@maxqualitylevel", "maximum quality", 1, 5),
        ):
            raw_value = record.get(field)
            if raw_value in (None, ""):
                continue
            if cls._bounded_optional_int(raw_value, minimum=minimum, maximum=maximum) is None:
                errors.append(f"Item {item_id} has invalid {label} {raw_value!r}.")

    @staticmethod
    def _display_name(item_id: str, names: dict[str, str], enchantment: int) -> str:
        if item_id in names:
            return names[item_id]
        if enchantment and (base_name := names.get(item_id.rsplit("@", 1)[0])):
            return f"{base_name} .{enchantment}"
        if "_LEVEL" in item_id:
            base_id, _, level = item_id.rpartition("_LEVEL")
            if base_name := names.get(base_id):
                return f"{base_name} .{level}"
        return item_id

    def _make_item(
        self,
        item_id: str,
        record: dict[str, Any],
        names: dict[str, str],
        *,
        item_type: str,
        enchantment: int,
    ) -> Item:
        return Item(
            item_id=item_id,
            display_name=self._display_name(item_id, names, enchantment),
            tier=self._bounded_optional_int(record.get("@tier"), minimum=1, maximum=8),
            enchantment=enchantment,
            category=str(record.get("@shopcategory") or item_type),
            subcategory=str(record.get("@shopsubcategory1") or ""),
            crafting_category=str(record.get("@craftingcategory") or ""),
            max_quality=self._bounded_optional_int(
                record.get("@maxqualitylevel"), minimum=1, maximum=5
            ),
        )

    @staticmethod
    def _select_recipe(value: Any) -> tuple[dict[str, Any] | None, bool]:
        candidates = [candidate for candidate in _as_list(value) if isinstance(candidate, dict)]
        craftable = [candidate for candidate in candidates if candidate.get("craftresource")]
        return (craftable[0], len(craftable) > 1) if craftable else (None, False)

    @classmethod
    def _materials(
        cls,
        requirement: dict[str, Any],
        canonical_by_raw_id: dict[str, str],
        *,
        output_item_id: str,
    ) -> tuple[tuple[MaterialRequirement, ...], tuple[str, ...]]:
        materials: list[MaterialRequirement] = []
        errors: list[str] = []
        declared = _as_list(requirement.get("craftresource"))
        for position, row in enumerate(declared):
            if not isinstance(row, dict) or not row.get("@uniquename"):
                errors.append(
                    f"Recipe {output_item_id} ingredient {position} has no item identity."
                )
                continue
            quantity = cls._finite_optional_float(row.get("@count"))
            if quantity is None or quantity <= 0:
                errors.append(
                    f"Recipe {output_item_id} ingredient {position} has invalid quantity "
                    f"{row.get('@count')!r}."
                )
                continue
            maximum_return = row.get("@maxreturnamount")
            if maximum_return is None:
                returnable: bool | None = True
            else:
                parsed_maximum_return = cls._finite_optional_float(maximum_return)
                if parsed_maximum_return is None or parsed_maximum_return < 0:
                    errors.append(
                        f"Recipe {output_item_id} ingredient {position} has invalid maximum "
                        f"return amount {maximum_return!r}."
                    )
                    continue
                returnable = False if parsed_maximum_return == 0 else None
            raw_id = str(row["@uniquename"])
            canonical_id = canonical_by_raw_id.get(
                raw_id, cls._canonical_id(raw_id, row.get("@enchantmentlevel"))
            )
            materials.append(MaterialRequirement(canonical_id, quantity, returnable=returnable))
        if declared and len(materials) != len(declared):
            errors.append(
                f"Recipe {output_item_id} parsed {len(materials)} of "
                f"{len(declared)} declared ingredients."
            )
        return tuple(materials), tuple(errors)

    @staticmethod
    def _finite_optional_float(value: Any) -> float | None:
        parsed = _optional_float(value)
        return parsed if parsed is not None and math.isfinite(parsed) else None

    @staticmethod
    def _derived_item_value(
        materials: tuple[MaterialRequirement, ...],
        known_item_values: dict[str, float],
    ) -> float | None:
        total = 0.0
        for material in materials:
            value = known_item_values.get(material.item_id)
            if value is None:
                if material.returnable is False:
                    # Albion marks catalysts/artifacts with maxreturnamount=0. When such an
                    # input has no direct Item Value, it contributes zero to the output's
                    # recipe-derived Item Value; it must still be bought at its market price.
                    continue
                return None
            total += value * material.quantity
        return total

    @classmethod
    def _resolve_item_values(
        cls,
        recipes: list[Recipe],
        direct_item_values: dict[str, float],
    ) -> dict[str, float]:
        """Resolve missing Item Values through the recipe graph to a fixed point.

        Direct ``@itemvalue`` attributes always win. Many consumables omit that attribute and
        depend on intermediate craftables (bread, alcohol, extracts) whose values must be derived
        first, so a single pass is insufficient.
        """

        resolved = dict(direct_item_values)
        pending = {
            recipe.output.item_id: recipe
            for recipe in recipes
            if recipe.output.item_id not in resolved
        }
        while pending:
            newly_resolved: dict[str, float] = {}
            for item_id, recipe in pending.items():
                batch_value = cls._derived_item_value(recipe.materials, resolved)
                if batch_value is not None:
                    newly_resolved[item_id] = batch_value / recipe.output_quantity
            if not newly_resolved:
                break
            resolved.update(newly_resolved)
            for item_id in newly_resolved:
                pending.pop(item_id)
        return resolved


class StaticCatalogValidator:
    def __init__(self, policy: StaticValidationPolicy | None = None) -> None:
        self.policy = policy or StaticValidationPolicy()

    def validate(
        self,
        catalog: ParsedCatalog,
        *,
        previous_counts: tuple[int, int],
    ) -> CatalogValidationResult:
        hard = list(catalog.diagnostics.structural_errors)
        soft: list[str] = []
        item_ids = {record.item.item_id for record in catalog.items}
        if len(item_ids) != len(catalog.items):
            hard.append("Candidate catalog contains duplicate item IDs.")
        for record in catalog.items:
            if record.item.tier is not None and not 1 <= record.item.tier <= 8:
                hard.append(f"Item {record.item.item_id} has an invalid tier.")
            if not 0 <= record.item.enchantment <= 4:
                hard.append(f"Item {record.item.item_id} has an invalid enchantment level.")
            if record.item.max_quality is not None and not 1 <= record.item.max_quality <= 5:
                hard.append(f"Item {record.item.item_id} has an invalid maximum quality.")
            if record.item_value is not None and (
                not math.isfinite(record.item_value) or record.item_value < 0
            ):
                hard.append(f"Item {record.item.item_id} has an invalid Item Value.")
        output_ids: set[str] = set()
        for recipe in catalog.recipes:
            if recipe.output.item_id in output_ids:
                hard.append(f"Duplicate recipe output {recipe.output.item_id}.")
            output_ids.add(recipe.output.item_id)
            if recipe.output.item_id not in item_ids:
                hard.append(f"Recipe output {recipe.output.item_id} is absent from catalog items.")
            if recipe.output_quantity <= 0:
                hard.append(f"Recipe {recipe.output.item_id} has non-positive output quantity.")
            if not recipe.materials:
                hard.append(f"Recipe {recipe.output.item_id} has no ingredients.")
            material_ids = [material.item_id for material in recipe.materials]
            if len(material_ids) != len(set(material_ids)):
                hard.append(
                    f"Recipe {recipe.output.item_id} contains duplicate ingredient identities."
                )
            if recipe.item_value is not None and (
                not math.isfinite(recipe.item_value) or recipe.item_value < 0
            ):
                hard.append(f"Recipe {recipe.output.item_id} has an invalid Item Value.")
            if recipe.base_focus_cost is not None and (
                not math.isfinite(recipe.base_focus_cost) or recipe.base_focus_cost < 0
            ):
                hard.append(f"Recipe {recipe.output.item_id} has an invalid Focus cost.")
            for material in recipe.materials:
                if material.item_id not in item_ids:
                    hard.append(
                        f"Recipe {recipe.output.item_id} ingredient {material.item_id} "
                        "is absent from catalog items."
                    )
                if not math.isfinite(material.quantity) or material.quantity <= 0:
                    hard.append(
                        f"Recipe {recipe.output.item_id} ingredient {material.item_id} "
                        "has an invalid quantity."
                    )
        for record in catalog.items:
            if record.craftable != (record.item.item_id in output_ids):
                hard.append(
                    f"Item {record.item.item_id} craftable flag does not match recipe coverage."
                )

        item_count = len(catalog.items)
        recipe_count = len(catalog.recipes)
        ingredient_count = catalog.diagnostics.ingredient_count
        for label, actual, minimum in (
            ("items", item_count, self.policy.minimum_items),
            ("recipes", recipe_count, self.policy.minimum_recipes),
            ("ingredients", ingredient_count, self.policy.minimum_ingredients),
        ):
            if actual < minimum:
                soft.append(f"Candidate has {actual:,} {label}; minimum is {minimum:,}.")
        missing_sentinels = sorted(self.policy.sentinel_ids - item_ids)
        if missing_sentinels:
            soft.append("Candidate is missing sentinel IDs: " + ", ".join(missing_sentinels))
        previous_items, previous_recipes = previous_counts
        for label, previous, candidate in (
            ("items", previous_items, item_count),
            ("recipes", previous_recipes, recipe_count),
        ):
            if previous > 0:
                drop = 1 - candidate / previous
                if drop > self.policy.maximum_relative_drop:
                    soft.append(
                        f"Candidate {label} fell {drop:.1%}: {previous:,} -> {candidate:,}."
                    )
        return CatalogValidationResult(tuple(dict.fromkeys(hard)), tuple(soft))


class StaticDataClient:
    """Explicit, cached updater for the pinned ao-bin-dumps source."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: StaticTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self._transport = transport or _default_transport

    def latest_release(self) -> StaticDataRelease:
        decoded = self._request_json(LATEST_COMMIT_URL)
        if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
            raise StaticDataError("GitHub returned no ao-bin-dumps release")
        row = decoded[0]
        commit = row.get("commit")
        if not row.get("sha") or not isinstance(commit, dict):
            raise StaticDataError("GitHub commit metadata has an unexpected schema")
        committer = commit.get("committer")
        timestamp = committer.get("date") if isinstance(committer, dict) else None
        return StaticDataRelease(str(row["sha"]), _parse_datetime(timestamp))

    def load_release(
        self,
        release: StaticDataRelease,
        cache_directory: Path,
    ) -> tuple[bytes, bytes]:
        if re.fullmatch(r"[0-9a-fA-F]{40}", release.version) is None:
            raise StaticDataError("Static release version must be a 40-character Git commit SHA")
        release_dir = cache_directory / release.version
        raw_path = release_dir / "items.json"
        formatted_path = release_dir / "formatted-items.json"
        if raw_path.is_file() and formatted_path.is_file():
            return raw_path.read_bytes(), formatted_path.read_bytes()

        raw_payload = self._request(RAW_ITEMS_URL.format(version=release.version))
        formatted_payload = self._request(FORMATTED_ITEMS_URL.format(version=release.version))
        release_dir.mkdir(parents=True, exist_ok=True)
        self._write_atomic(raw_path, raw_payload)
        self._write_atomic(formatted_path, formatted_payload)
        return raw_payload, formatted_payload

    def update_catalog(
        self,
        repository: CatalogRepository,
        cache_directory: Path,
        *,
        release: StaticDataRelease | None = None,
        force: bool = False,
        validation_policy: StaticValidationPolicy | None = None,
    ) -> CatalogImport:
        started_at = datetime.now(UTC)
        selected = release or self.latest_release()
        existing = repository.import_metadata()
        previous_counts = repository.counts()
        latest_report = repository.latest_import_report()
        validator = StaticCatalogValidator(validation_policy)
        if (
            not force
            and existing is not None
            and existing.source_id == SOURCE_ID
            and existing.source_version == selected.version
            and previous_counts == (existing.item_count, existing.recipe_count)
            and latest_report is not None
            and latest_report.activated
            and latest_report.source_version == selected.version
            and latest_report.validation_status
            in {f"passed_v{IMPORTER_VERSION}", f"forced_v{IMPORTER_VERSION}"}
            and repository.catalog_is_healthy(
                existing,
                expected_ingredient_count=latest_report.ingredient_count,
                required_item_ids=(
                    tuple(sorted(validator.policy.sentinel_ids))
                    if latest_report.validation_status == f"passed_v{IMPORTER_VERSION}"
                    else ()
                ),
            )
        ):
            return existing
        try:
            raw_payload, formatted_payload = self.load_release(selected, cache_directory)
        except StaticDataError as exc:
            repository.record_import_report(
                CatalogImportReport(
                    source_id=SOURCE_ID,
                    source_url=SOURCE_URL,
                    source_version=selected.version,
                    source_timestamp=selected.source_timestamp,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    raw_sha256=None,
                    formatted_sha256=None,
                    previous_item_count=previous_counts[0],
                    previous_recipe_count=previous_counts[1],
                    item_count=0,
                    recipe_count=0,
                    ingredient_count=0,
                    unknown_returnability_count=0,
                    skipped_malformed_count=0,
                    validation_status=f"download_failed_v{IMPORTER_VERSION}",
                    validation_messages=(str(exc),),
                    forced=force,
                    activated=False,
                )
            )
            raise
        try:
            parsed = StaticCatalogParser().parse(
                raw_payload,
                formatted_payload,
                source_version=selected.version,
            )
        except (StaticDataError, TypeError, ValueError) as exc:
            error = exc if isinstance(exc, StaticDataError) else StaticDataError(str(exc))
            repository.record_import_report(
                CatalogImportReport(
                    source_id=SOURCE_ID,
                    source_url=SOURCE_URL,
                    source_version=selected.version,
                    source_timestamp=selected.source_timestamp,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    raw_sha256=sha256(raw_payload).hexdigest(),
                    formatted_sha256=sha256(formatted_payload).hexdigest(),
                    previous_item_count=previous_counts[0],
                    previous_recipe_count=previous_counts[1],
                    item_count=0,
                    recipe_count=0,
                    ingredient_count=0,
                    unknown_returnability_count=0,
                    skipped_malformed_count=0,
                    validation_status=f"rejected_v{IMPORTER_VERSION}",
                    validation_messages=(str(error),),
                    forced=force,
                    activated=False,
                )
            )
            if isinstance(exc, StaticDataError):
                raise
            raise error from exc
        validation = validator.validate(parsed, previous_counts=previous_counts)
        finished_at = datetime.now(UTC)
        accepted = validation.accepted(force=force)
        status = f"forced_v{IMPORTER_VERSION}" if force else f"passed_v{IMPORTER_VERSION}"
        report = CatalogImportReport(
            source_id=SOURCE_ID,
            source_url=SOURCE_URL,
            source_version=selected.version,
            source_timestamp=selected.source_timestamp,
            started_at=started_at,
            finished_at=finished_at,
            raw_sha256=sha256(raw_payload).hexdigest(),
            formatted_sha256=sha256(formatted_payload).hexdigest(),
            previous_item_count=previous_counts[0],
            previous_recipe_count=previous_counts[1],
            item_count=len(parsed.items),
            recipe_count=len(parsed.recipes),
            ingredient_count=parsed.diagnostics.ingredient_count,
            unknown_returnability_count=parsed.diagnostics.unknown_returnability_count,
            skipped_malformed_count=parsed.diagnostics.skipped_malformed_count,
            validation_status=(status if accepted else f"rejected_v{IMPORTER_VERSION}"),
            validation_messages=validation.messages,
            forced=force,
            activated=accepted,
        )
        if not accepted:
            repository.record_import_report(report)
            blocking = validation.hard_errors or validation.soft_errors
            raise StaticDataError("Static catalog validation failed: " + " ".join(blocking))
        return repository.replace_all(
            parsed.items,
            parsed.recipes,
            CatalogImport(
                source_id=SOURCE_ID,
                source_url=SOURCE_URL,
                source_version=selected.version,
                source_timestamp=selected.source_timestamp,
                imported_at=finished_at,
                item_count=len(parsed.items),
                recipe_count=len(parsed.recipes),
            ),
            report=report,
            allow_same_version=True,
        )

    def _request_json(self, url: str) -> Any:
        try:
            return json.loads(self._request(url))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StaticDataError("Static source returned malformed JSON") from exc

    def _request(self, url: str) -> bytes:
        try:
            return self._transport(url, self.timeout)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise StaticDataError(f"Unable to retrieve static game data: {exc}") from exc

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
