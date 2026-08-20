from __future__ import annotations

import argparse
import sys

from albion_crafter.data.static_importer import StaticDataClient, StaticDataError
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import Database, default_data_directory, default_database_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Albion Crafter static game data.")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass count/drop/sentinel sanity checks. Structural and referential failures "
            "are never bypassed."
        ),
    )
    arguments = parser.parse_args(argv)
    database = Database(default_database_path())
    database.initialize()
    repository = CatalogRepository(database)
    try:
        metadata = StaticDataClient().update_catalog(
            repository,
            default_data_directory() / "static-cache",
            force=arguments.force,
        )
    except StaticDataError as exc:
        print(f"Static catalog update rejected: {exc}", file=sys.stderr)
        report = repository.latest_import_report()
        if report is not None and report.validation_messages:
            for message in report.validation_messages:
                print(f"- {message}", file=sys.stderr)
        if not arguments.force:
            print(
                "Use --force only after reviewing the diagnostics; structural failures "
                "cannot be forced.",
                file=sys.stderr,
            )
        return 2
    report = repository.latest_import_report()
    print(
        f"Imported {metadata.item_count:,} items and {metadata.recipe_count:,} recipes "
        f"from {metadata.source_version}."
    )
    if report is not None:
        print(
            f"Source timestamp: {report.source_timestamp or 'unknown'}; completed: "
            f"{report.finished_at}."
        )
        print(
            f"Previous catalog: {report.previous_item_count:,} items / "
            f"{report.previous_recipe_count:,} recipes; new catalog: "
            f"{report.item_count:,} items / {report.recipe_count:,} recipes."
        )
        print(
            f"Ingredients: {report.ingredient_count:,}; unknown returnability: "
            f"{report.unknown_returnability_count:,}; skipped malformed: "
            f"{report.skipped_malformed_count:,}; validation: {report.validation_status}."
        )
        if report.validation_messages:
            print("Validation diagnostics:")
            for message in report.validation_messages:
                print(f"- {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
