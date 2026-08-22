from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import station_type_for_item

from .database import Database, _parse_datetime, _serialize_datetime


@dataclass(frozen=True, slots=True)
class CatalogItem:
    item: Item
    item_value: float | None
    craftable: bool
    provenance: Provenance
    source_version: str


@dataclass(frozen=True, slots=True)
class CatalogImport:
    source_id: str
    source_url: str
    source_version: str
    source_timestamp: datetime | None
    imported_at: datetime
    item_count: int
    recipe_count: int


@dataclass(frozen=True, slots=True)
class CatalogRecipeCoverage:
    total: int
    supported: int
    unknown_item_value: int
    unknown_station_type: int
    unknown_returnability: int
    ambiguous_recipe: int
    untrusted_recipe: int

    @property
    def unsupported(self) -> int:
        return self.total - self.supported


@dataclass(frozen=True, slots=True)
class CatalogImportReport:
    source_id: str
    source_url: str
    source_version: str
    source_timestamp: datetime | None
    started_at: datetime
    finished_at: datetime
    raw_sha256: str | None
    formatted_sha256: str | None
    previous_item_count: int
    previous_recipe_count: int
    item_count: int
    recipe_count: int
    ingredient_count: int
    unknown_returnability_count: int
    skipped_malformed_count: int
    validation_status: str
    validation_messages: tuple[str, ...]
    forced: bool
    activated: bool


class CatalogRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def replace_all(
        self,
        items: list[CatalogItem],
        recipes: list[Recipe],
        metadata: CatalogImport,
        *,
        report: CatalogImportReport | None = None,
        allow_same_version: bool = False,
    ) -> CatalogImport:
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM catalog_imports WHERE source_id=?",
                (metadata.source_id,),
            ).fetchone()
            if (
                not allow_same_version
                and existing
                and existing["source_version"] == metadata.source_version
                and self._catalog_matches_metadata(connection, existing)
            ):
                return self._import_from_row(existing)

            connection.execute("DELETE FROM catalog_materials")
            connection.execute("DELETE FROM catalog_recipes")
            connection.execute("DELETE FROM catalog_items")
            connection.executemany(
                """
                INSERT INTO catalog_items (
                    item_id, display_name, tier, enchantment, category, subcategory,
                    crafting_category, max_quality, item_value, craftable,
                    provenance, source_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.item.item_id,
                        record.item.display_name,
                        record.item.tier,
                        record.item.enchantment,
                        record.item.category,
                        record.item.subcategory,
                        record.item.crafting_category,
                        record.item.max_quality,
                        record.item_value,
                        int(record.craftable),
                        record.provenance.value,
                        record.source_version,
                    )
                    for record in items
                ],
            )
            connection.executemany(
                """
                INSERT INTO catalog_recipes (
                    output_item_id, output_quantity, base_focus_cost,
                    recipe_ambiguous, provenance, source_version
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        recipe.output.item_id,
                        recipe.output_quantity,
                        recipe.base_focus_cost,
                        int(recipe.recipe_ambiguous),
                        recipe.provenance.value,
                        recipe.source_version,
                    )
                    for recipe in recipes
                ],
            )
            material_rows = []
            for recipe in recipes:
                for position, material in enumerate(recipe.materials):
                    material_rows.append(
                        (
                            recipe.output.item_id,
                            position,
                            material.item_id,
                            material.quantity,
                            None if material.returnable is None else int(material.returnable),
                        )
                    )
            connection.executemany(
                """
                INSERT INTO catalog_materials (
                    output_item_id, position, item_id, quantity, returnable
                ) VALUES (?, ?, ?, ?, ?)
                """,
                material_rows,
            )
            connection.execute(
                """
                INSERT INTO catalog_imports (
                    source_id, source_url, source_version, source_timestamp,
                    imported_at, item_count, recipe_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_url=excluded.source_url,
                    source_version=excluded.source_version,
                    source_timestamp=excluded.source_timestamp,
                    imported_at=excluded.imported_at,
                    item_count=excluded.item_count,
                    recipe_count=excluded.recipe_count
                """,
                (
                    metadata.source_id,
                    metadata.source_url,
                    metadata.source_version,
                    _serialize_datetime(metadata.source_timestamp),
                    _serialize_datetime(metadata.imported_at),
                    len(items),
                    len(recipes),
                ),
            )
            if report is not None:
                self._insert_import_report(connection, report)
        return CatalogImport(
            source_id=metadata.source_id,
            source_url=metadata.source_url,
            source_version=metadata.source_version,
            source_timestamp=metadata.source_timestamp,
            imported_at=metadata.imported_at,
            item_count=len(items),
            recipe_count=len(recipes),
        )

    def record_import_report(self, report: CatalogImportReport) -> None:
        with self.database.connection() as connection:
            self._insert_import_report(connection, report)

    def latest_import_report(self) -> CatalogImportReport | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_import_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        return self._report_from_row(row) if row else None

    def get_item(self, item_id: str) -> CatalogItem | None:
        """Return one persisted catalog item, including its static-data metadata."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return CatalogItem(
            item=self._item_from_row(row),
            item_value=row["item_value"],
            craftable=bool(row["craftable"]),
            provenance=Provenance(row["provenance"]),
            source_version=row["source_version"],
        )

    def get_recipe(self, item_id: str) -> Recipe | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT i.*, r.output_quantity, r.base_focus_cost,
                       r.recipe_ambiguous, r.provenance AS recipe_provenance,
                       r.source_version AS recipe_source_version
                FROM catalog_items i JOIN catalog_recipes r
                  ON r.output_item_id=i.item_id
                WHERE i.item_id=?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            materials = connection.execute(
                """
                SELECT * FROM catalog_materials
                WHERE output_item_id=? ORDER BY position
                """,
                (item_id,),
            ).fetchall()
        item = self._item_from_row(row)
        return Recipe(
            output=item,
            output_quantity=row["output_quantity"],
            materials=tuple(
                MaterialRequirement(
                    material["item_id"],
                    material["quantity"],
                    None if material["returnable"] is None else bool(material["returnable"]),
                )
                for material in materials
            ),
            item_value=row["item_value"],
            base_focus_cost=row["base_focus_cost"],
            recipe_ambiguous=bool(row["recipe_ambiguous"]),
            provenance=Provenance(row["recipe_provenance"]),
            source_version=row["recipe_source_version"],
        )

    def list_recipes(
        self,
        query: str = "",
        *,
        tier_min: int | None = None,
        tier_max: int | None = None,
        enchantments: tuple[int, ...] = (),
        categories: tuple[str, ...] = (),
        crafting_categories: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> list[Recipe]:
        """Hydrate a candidate recipe set in exactly two SQL statements."""
        clauses = ["i.craftable=1"]
        values: list[object] = []
        if query.strip():
            clauses.append("(i.display_name LIKE ? OR i.item_id LIKE ?)")
            pattern = f"%{query.strip()}%"
            values.extend((pattern, pattern))
        if tier_min is not None:
            clauses.append("i.tier>=?")
            values.append(tier_min)
        if tier_max is not None:
            clauses.append("i.tier<=?")
            values.append(tier_max)
        for column, selected in (
            ("i.enchantment", enchantments),
            ("i.category", categories),
            ("i.crafting_category", crafting_categories),
        ):
            if selected:
                clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        limit_sql = "" if limit is None else " LIMIT ?"
        if limit is not None:
            if limit < 1:
                return []
            values.append(limit)
        selected_sql = f"""
            SELECT i.item_id
            FROM catalog_items i JOIN catalog_recipes r ON r.output_item_id=i.item_id
            WHERE {" AND ".join(clauses)}
            ORDER BY i.display_name, i.tier, i.enchantment, i.item_id{limit_sql}
        """
        with self.database.connection() as connection:
            recipe_rows = connection.execute(
                f"""
                WITH selected AS ({selected_sql})
                SELECT i.*, r.output_quantity, r.base_focus_cost,
                       r.recipe_ambiguous, r.provenance AS recipe_provenance,
                       r.source_version AS recipe_source_version
                FROM selected s
                JOIN catalog_items i ON i.item_id=s.item_id
                JOIN catalog_recipes r ON r.output_item_id=s.item_id
                ORDER BY i.display_name, i.tier, i.enchantment, i.item_id
                """,  # noqa: S608 - clauses and identifiers are fixed internally
                values,
            ).fetchall()
            material_rows = connection.execute(
                f"""
                WITH selected AS ({selected_sql})
                SELECT m.* FROM selected s
                JOIN catalog_materials m ON m.output_item_id=s.item_id
                ORDER BY m.output_item_id, m.position
                """,  # noqa: S608 - clauses and identifiers are fixed internally
                values,
            ).fetchall()

        materials_by_output: dict[str, list[MaterialRequirement]] = {}
        for material in material_rows:
            materials_by_output.setdefault(material["output_item_id"], []).append(
                MaterialRequirement(
                    material["item_id"],
                    material["quantity"],
                    None if material["returnable"] is None else bool(material["returnable"]),
                )
            )
        return [
            Recipe(
                output=self._item_from_row(row),
                output_quantity=row["output_quantity"],
                materials=tuple(materials_by_output[row["item_id"]]),
                item_value=row["item_value"],
                base_focus_cost=row["base_focus_cost"],
                recipe_ambiguous=bool(row["recipe_ambiguous"]),
                provenance=Provenance(row["recipe_provenance"]),
                source_version=row["recipe_source_version"],
            )
            for row in recipe_rows
        ]

    def search_recipes(
        self,
        query: str = "",
        *,
        tier: int | None = None,
        enchantment: int | None = None,
        limit: int = 100,
    ) -> list[Item]:
        clauses = ["r.output_item_id=i.item_id"]
        values: list[object] = []
        if query.strip():
            clauses.append("(i.display_name LIKE ? OR i.item_id LIKE ?)")
            pattern = f"%{query.strip()}%"
            values.extend((pattern, pattern))
        if tier is not None:
            clauses.append("i.tier=?")
            values.append(tier)
        if enchantment is not None:
            clauses.append("i.enchantment=?")
            values.append(enchantment)
        values.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT i.* FROM catalog_items i, catalog_recipes r
                WHERE {" AND ".join(clauses)}
                ORDER BY i.display_name, i.tier, i.enchantment LIMIT ?
                """,  # noqa: S608 - clauses are fixed internal strings
                values,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def search_items(
        self,
        query: str = "",
        *,
        categories: tuple[str, ...] = (),
        subcategories: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[Item]:
        """Search the complete static catalog for bounded item-picker results.

        Unlike :meth:`search_recipes`, this intentionally includes non-craftable
        equipment such as mounts. Category filters are supplied by trusted UI
        slot definitions rather than raw SQL identifiers.
        """

        if limit < 1:
            return []
        clauses = ["i.tier IS NOT NULL"]
        values: list[object] = []
        if query.strip():
            clauses.append("(i.display_name LIKE ? OR i.item_id LIKE ?)")
            pattern = f"%{query.strip()}%"
            values.extend((pattern, pattern))
        for column, selected in (
            ("i.category", categories),
            ("i.subcategory", subcategories),
        ):
            if selected:
                clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        values.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT i.* FROM catalog_items i
                WHERE {" AND ".join(clauses)}
                ORDER BY i.display_name, i.tier, i.enchantment, i.item_id
                LIMIT ?
                """,  # noqa: S608 - clauses and identifiers are fixed internally
                values,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def import_metadata(self) -> CatalogImport | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_imports ORDER BY imported_at DESC LIMIT 1"
            ).fetchone()
        return self._import_from_row(row) if row else None

    def counts(self) -> tuple[int, int]:
        with self.database.connection() as connection:
            items = connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0]
            recipes = connection.execute("SELECT COUNT(*) FROM catalog_recipes").fetchone()[0]
        return int(items), int(recipes)

    def list_item_ids(self) -> tuple[str, ...]:
        """Return every canonical item ID in the active static catalog."""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT item_id FROM catalog_items ORDER BY item_id"
            ).fetchall()
        return tuple(str(row["item_id"]) for row in rows)

    def list_items(self, item_ids: Iterable[str] | None = None) -> list[Item]:
        """Bulk-load canonical catalog items, optionally restricted to explicit IDs."""

        requested = None if item_ids is None else tuple(dict.fromkeys(item_ids))
        if requested == ():
            return []
        rows = []
        with self.database.connection() as connection:
            if requested is None:
                rows = connection.execute("SELECT * FROM catalog_items ORDER BY item_id").fetchall()
            else:
                for offset in range(0, len(requested), 900):
                    chunk = requested[offset : offset + 900]
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        connection.execute(
                            f"SELECT * FROM catalog_items "  # noqa: S608
                            f"WHERE item_id IN ({placeholders}) ORDER BY item_id",
                            chunk,
                        ).fetchall()
                    )
        return [self._item_from_row(row) for row in rows]

    def recipe_coverage(self) -> CatalogRecipeCoverage:
        """Classify imported recipes without hydrating ingredients or querying per recipe."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.*, r.recipe_ambiguous,
                       r.provenance AS recipe_provenance,
                       EXISTS (
                           SELECT 1 FROM catalog_materials m
                           WHERE m.output_item_id=i.item_id AND m.returnable IS NULL
                       ) AS has_unknown_returnability
                FROM catalog_items i JOIN catalog_recipes r
                  ON r.output_item_id=i.item_id
                WHERE i.craftable=1
                """
            ).fetchall()
        counts = {
            "supported": 0,
            "unknown_item_value": 0,
            "unknown_station_type": 0,
            "unknown_returnability": 0,
            "ambiguous_recipe": 0,
            "untrusted_recipe": 0,
        }
        for row in rows:
            item = self._item_from_row(row)
            if Provenance(row["recipe_provenance"]) is not Provenance.STATIC_GAME_DATA:
                counts["untrusted_recipe"] += 1
            elif bool(row["recipe_ambiguous"]):
                counts["ambiguous_recipe"] += 1
            elif row["item_value"] is None:
                counts["unknown_item_value"] += 1
            elif bool(row["has_unknown_returnability"]):
                counts["unknown_returnability"] += 1
            elif station_type_for_item(item) is None:
                counts["unknown_station_type"] += 1
            else:
                counts["supported"] += 1
        return CatalogRecipeCoverage(len(rows), **counts)

    def catalog_is_healthy(
        self,
        metadata: CatalogImport,
        *,
        expected_ingredient_count: int | None = None,
        required_item_ids: tuple[str, ...] = (),
    ) -> bool:
        with self.database.connection() as connection:
            if not self._catalog_matches_metadata(connection, metadata):
                return False
            if expected_ingredient_count is not None:
                actual = int(
                    connection.execute("SELECT COUNT(*) FROM catalog_materials").fetchone()[0]
                )
                if actual != expected_ingredient_count:
                    return False
            if required_item_ids:
                placeholders = ",".join("?" for _ in required_item_ids)
                present = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM catalog_items WHERE item_id IN ({placeholders})",  # noqa: S608
                        required_item_ids,
                    ).fetchone()[0]
                )
                if present != len(set(required_item_ids)):
                    return False
            inconsistent = connection.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM catalog_items
                        WHERE source_version != ? OR provenance != ?
                    )
                    OR EXISTS(
                        SELECT 1 FROM catalog_recipes
                        WHERE source_version != ? OR provenance != ?
                    )
                    OR EXISTS(
                        SELECT 1 FROM catalog_materials m
                        LEFT JOIN catalog_items i ON i.item_id=m.item_id
                        WHERE i.item_id IS NULL OR m.quantity <= 0
                           OR m.returnable NOT IN (0, 1)
                    )
                    OR EXISTS(
                        SELECT 1 FROM catalog_items
                        WHERE (tier IS NOT NULL AND (tier < 1 OR tier > 8))
                           OR enchantment < 0 OR enchantment > 4
                           OR (max_quality IS NOT NULL AND (max_quality < 1 OR max_quality > 5))
                           OR (item_value IS NOT NULL AND item_value < 0)
                           OR craftable NOT IN (0, 1)
                    )
                    OR EXISTS(
                        SELECT 1 FROM catalog_recipes
                        WHERE output_quantity <= 0
                           OR (base_focus_cost IS NOT NULL AND base_focus_cost < 0)
                           OR recipe_ambiguous NOT IN (0, 1)
                    )
                """,
                (
                    metadata.source_version,
                    Provenance.STATIC_GAME_DATA.value,
                    metadata.source_version,
                    Provenance.STATIC_GAME_DATA.value,
                ),
            ).fetchone()[0]
            return not bool(inconsistent)

    @staticmethod
    def _catalog_matches_metadata(connection, metadata_row) -> bool:
        item_count = int(connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0])
        recipe_count = int(connection.execute("SELECT COUNT(*) FROM catalog_recipes").fetchone()[0])
        expected_items = (
            metadata_row.item_count
            if isinstance(metadata_row, CatalogImport)
            else metadata_row["item_count"]
        )
        expected_recipes = (
            metadata_row.recipe_count
            if isinstance(metadata_row, CatalogImport)
            else metadata_row["recipe_count"]
        )
        return item_count == expected_items and recipe_count == expected_recipes

    @staticmethod
    def _insert_import_report(connection, report: CatalogImportReport) -> None:
        connection.execute(
            """
            INSERT INTO catalog_import_runs (
                source_id, source_url, source_version, source_timestamp,
                started_at, finished_at, raw_sha256, formatted_sha256,
                previous_item_count, previous_recipe_count, item_count, recipe_count,
                ingredient_count, unknown_returnability_count, skipped_malformed_count,
                validation_status, validation_messages_json, forced, activated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.source_id,
                report.source_url,
                report.source_version,
                _serialize_datetime(report.source_timestamp),
                _serialize_datetime(report.started_at),
                _serialize_datetime(report.finished_at),
                report.raw_sha256,
                report.formatted_sha256,
                report.previous_item_count,
                report.previous_recipe_count,
                report.item_count,
                report.recipe_count,
                report.ingredient_count,
                report.unknown_returnability_count,
                report.skipped_malformed_count,
                report.validation_status,
                json.dumps(report.validation_messages),
                int(report.forced),
                int(report.activated),
            ),
        )

    @staticmethod
    def _report_from_row(row) -> CatalogImportReport:
        started_at = _parse_datetime(row["started_at"])
        finished_at = _parse_datetime(row["finished_at"])
        assert started_at is not None and finished_at is not None
        return CatalogImportReport(
            source_id=row["source_id"],
            source_url=row["source_url"],
            source_version=row["source_version"],
            source_timestamp=_parse_datetime(row["source_timestamp"]),
            started_at=started_at,
            finished_at=finished_at,
            raw_sha256=row["raw_sha256"],
            formatted_sha256=row["formatted_sha256"],
            previous_item_count=row["previous_item_count"],
            previous_recipe_count=row["previous_recipe_count"],
            item_count=row["item_count"],
            recipe_count=row["recipe_count"],
            ingredient_count=row["ingredient_count"],
            unknown_returnability_count=row["unknown_returnability_count"],
            skipped_malformed_count=row["skipped_malformed_count"],
            validation_status=row["validation_status"],
            validation_messages=tuple(json.loads(row["validation_messages_json"])),
            forced=bool(row["forced"]),
            activated=bool(row["activated"]),
        )

    @staticmethod
    def _item_from_row(row) -> Item:
        return Item(
            item_id=row["item_id"],
            display_name=row["display_name"],
            tier=row["tier"],
            enchantment=row["enchantment"],
            category=row["category"],
            subcategory=row["subcategory"],
            crafting_category=row["crafting_category"],
            max_quality=row["max_quality"],
        )

    @staticmethod
    def _import_from_row(row) -> CatalogImport:
        imported_at = _parse_datetime(row["imported_at"])
        assert imported_at is not None
        return CatalogImport(
            source_id=row["source_id"],
            source_url=row["source_url"],
            source_version=row["source_version"],
            source_timestamp=_parse_datetime(row["source_timestamp"]),
            imported_at=imported_at,
            item_count=row["item_count"],
            recipe_count=row["recipe_count"],
        )
