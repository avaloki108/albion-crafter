import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from albion_crafter.core.stations import StationType, station_type_for_item
from albion_crafter.data.static_importer import (
    StaticCatalogParser,
    StaticCatalogValidator,
    StaticDataClient,
    StaticDataError,
    StaticDataRelease,
    StaticValidationPolicy,
)
from albion_crafter.database.catalog import CatalogImport, CatalogRepository
from albion_crafter.database.database import Database

ACID_UPSTREAM_FIXTURE = Path(__file__).parent / "fixtures" / "acid-potion-upstream-contract"


def _payloads() -> tuple[bytes, bytes]:
    raw = {
        "items": {
            "weapon": {
                "@uniquename": "T4_SWORD",
                "@tier": "4",
                "@maxqualitylevel": "5",
                "@shopcategory": "weapons",
                "@shopsubcategory1": "sword",
                "@craftingcategory": "sword",
                "craftingrequirements": [
                    {
                        "@craftingfocus": "100",
                        "craftresource": [
                            {"@uniquename": "T4_BAR", "@count": "2"},
                            {
                                "@uniquename": "T4_ARTEFACT_SWORD",
                                "@count": "1",
                                "@maxreturnamount": "0",
                            },
                        ],
                    },
                    {
                        "@craftingfocus": "100",
                        "craftresource": {"@uniquename": "TOKEN", "@count": "1"},
                    },
                ],
                "enchantments": {
                    "enchantment": {
                        "@enchantmentlevel": "1",
                        "craftingrequirements": {
                            "@craftingfocus": "175",
                            "craftresource": [
                                {"@uniquename": "T4_BAR_LEVEL1", "@count": "2"},
                                {
                                    "@uniquename": "T4_ARTEFACT_SWORD",
                                    "@count": "1",
                                    "@maxreturnamount": "0",
                                },
                            ],
                        },
                    }
                },
            },
            "simpleitem": [
                {"@uniquename": "T4_BAR", "@tier": "4", "@itemvalue": "16"},
                {
                    "@uniquename": "T4_BAR_LEVEL1",
                    "@tier": "4",
                    "@enchantmentlevel": "1",
                    "@itemvalue": "32",
                },
                {
                    "@uniquename": "T4_ARTEFACT_SWORD",
                    "@tier": "4",
                    "@itemvalue": "96",
                },
            ],
        }
    }
    formatted = [
        {"UniqueName": "T4_SWORD", "LocalizedNames": {"EN-US": "Adept's Test Sword"}},
        {"UniqueName": "T4_SWORD@1", "LocalizedNames": {"EN-US": "Adept's Test Sword"}},
        {"UniqueName": "T4_BAR", "LocalizedNames": {"EN-US": "Steel Bar"}},
    ]
    return json.dumps(raw).encode(), json.dumps(formatted).encode()


def test_static_import_is_real_schema_aware_cached_and_idempotent(tmp_path) -> None:
    raw, formatted = _payloads()
    calls: list[str] = []
    sha = "a" * 40

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        if "api.github.com" in url:
            return json.dumps(
                [
                    {
                        "sha": sha,
                        "commit": {"committer": {"date": "2026-07-27T11:31:41Z"}},
                    }
                ]
            ).encode()
        if "/formatted/items.json" in url:
            return formatted
        return raw

    database = Database(tmp_path / "catalog.db")
    database.initialize()
    repository = CatalogRepository(database)
    client = StaticDataClient(transport=transport)
    first = client.update_catalog(repository, tmp_path / "cache", force=True)
    second = client.update_catalog(repository, tmp_path / "cache")

    assert (first.item_count, first.recipe_count) == (5, 2)
    assert second == first
    assert repository.counts() == (5, 2)
    assert sum("raw.githubusercontent.com" in call for call in calls) == 2
    assert (tmp_path / "cache" / sha / "items.json").is_file()

    base = repository.get_recipe("T4_SWORD")
    assert base is not None
    assert base.output.display_name == "Adept's Test Sword"
    assert base.item_value == 128
    assert base.base_focus_cost == 100
    assert base.recipe_ambiguous
    assert [material.returnable for material in base.materials] == [True, False]

    enchanted = repository.get_recipe("T4_SWORD@1")
    assert enchanted is not None
    assert enchanted.output.enchantment == 1
    assert enchanted.materials[0].item_id == "T4_BAR_LEVEL1@1"
    assert enchanted.item_value == 160
    assert repository.search_recipes("Test Sword", enchantment=1)[0].item_id == "T4_SWORD@1"


def test_acid_potion_item_value_is_derived_without_nonreturnable_catalyst(tmp_path) -> None:
    """Mirror the pinned T5 acid shape: output and rare ingredient omit Item Value."""

    parsed = StaticCatalogParser().parse(
        (ACID_UPSTREAM_FIXTURE / "items.json").read_bytes(),
        (ACID_UPSTREAM_FIXTURE / "formatted-items.json").read_bytes(),
        source_version="acid-pinned-fixture",
    )

    acid = next(recipe for recipe in parsed.recipes if recipe.output.item_id == "T5_POTION_ACID")
    assert acid.item_value == 336
    assert acid.output_quantity == 10
    assert [
        (material.item_id, material.quantity, material.returnable) for material in acid.materials
    ] == [
        ("T5_ALCHEMY_RARE_DIREBEAR", 1, False),
        ("T5_TEASEL", 48, True),
        ("T4_BURDOCK", 24, True),
        ("T4_MILK", 12, True),
    ]
    assert acid.output.crafting_category == "potion"
    assert station_type_for_item(acid.output) is StationType.ALCHEMIST_LAB

    database = Database(tmp_path / "acid.db")
    database.initialize()
    repository = CatalogRepository(database)
    repository.replace_all(
        parsed.items,
        parsed.recipes,
        CatalogImport(
            "acid-fixture",
            "memory://acid",
            "acid-pinned-fixture",
            None,
            datetime(2026, 8, 19, tzinfo=UTC),
            len(parsed.items),
            len(parsed.recipes),
        ),
    )
    coverage = repository.recipe_coverage()
    assert coverage.total == 1
    assert coverage.supported == 1
    assert coverage.unknown_item_value == 0
    assert coverage.unknown_station_type == 0
    assert coverage.ambiguous_recipe == 0


def test_item_value_resolution_walks_intermediate_recipes_and_preserves_direct_values() -> None:
    raw = {
        "items": {
            "consumableitem": [
                {
                    "@uniquename": "T4_INTERMEDIATE",
                    "@tier": "4",
                    "craftingrequirements": {
                        "@amountcrafted": "2",
                        "craftresource": {"@uniquename": "T4_RAW", "@count": "4"},
                    },
                },
                {
                    "@uniquename": "T4_MEAL_TEST",
                    "@tier": "4",
                    "craftingrequirements": {
                        "@amountcrafted": "10",
                        "craftresource": [
                            {"@uniquename": "T4_INTERMEDIATE", "@count": "5"},
                            {"@uniquename": "T4_OTHER", "@count": "10"},
                        ],
                    },
                },
                {
                    "@uniquename": "T4_DIRECT",
                    "@tier": "4",
                    "@itemvalue": "999",
                    "craftingrequirements": {
                        "craftresource": {"@uniquename": "T4_OTHER", "@count": "1"},
                    },
                },
            ],
            "simpleitem": [
                {"@uniquename": "T4_RAW", "@tier": "4", "@itemvalue": "20"},
                {"@uniquename": "T4_OTHER", "@tier": "4", "@itemvalue": "10"},
            ],
        }
    }
    parsed = StaticCatalogParser().parse(
        json.dumps(raw).encode(), b"[]", source_version="recursive-item-values"
    )
    recipes = {recipe.output.item_id: recipe for recipe in parsed.recipes}

    assert recipes["T4_INTERMEDIATE"].item_value == 40
    assert recipes["T4_MEAL_TEST"].item_value == 30
    assert recipes["T4_DIRECT"].item_value == 999


def test_invalid_item_numerics_and_duplicate_ids_are_structural_errors() -> None:
    raw_payload, formatted = _payloads()
    raw = json.loads(raw_payload)
    raw["items"]["simpleitem"][0]["@tier"] = "tier-four"
    raw["items"]["simpleitem"].append({"@uniquename": "T4_BAR", "@tier": "4", "@itemvalue": "17"})
    parsed = StaticCatalogParser().parse(
        json.dumps(raw).encode(), formatted, source_version="invalid-numerics"
    )
    validation = StaticCatalogValidator(
        StaticValidationPolicy(
            minimum_items=0,
            minimum_recipes=0,
            minimum_ingredients=0,
            sentinel_ids=frozenset(),
        )
    ).validate(parsed, previous_counts=(0, 0))

    assert any("T4_BAR has invalid tier" in message for message in validation.hard_errors)
    assert any(
        "Duplicate canonical item ID T4_BAR" in message for message in validation.hard_errors
    )


def test_static_parser_rejects_malformed_json(tmp_path) -> None:
    database = Database(tmp_path / "bad.db")
    database.initialize()
    repository = CatalogRepository(database)
    release_payload = json.dumps(
        [
            {
                "sha": "b" * 40,
                "commit": {"committer": {"date": datetime.now(UTC).isoformat()}},
            }
        ]
    ).encode()

    def transport(url: str, _timeout: float) -> bytes:
        return release_payload if "api.github.com" in url else b"not-json"

    with pytest.raises(StaticDataError, match="valid JSON"):
        StaticDataClient(transport=transport).update_catalog(repository, tmp_path / "cache")
    assert repository.counts() == (0, 0)


def test_static_download_failure_is_reported_without_touching_catalog(tmp_path) -> None:
    database = Database(tmp_path / "offline.db")
    database.initialize()
    repository = CatalogRepository(database)

    def transport(_url: str, _timeout: float) -> bytes:
        raise TimeoutError("controlled offline failure")

    with pytest.raises(StaticDataError, match="Unable to retrieve"):
        StaticDataClient(transport=transport).update_catalog(
            repository,
            tmp_path / "cache",
            release=StaticDataRelease("9" * 40, None),
        )
    assert repository.counts() == (0, 0)
    report = repository.latest_import_report()
    assert report is not None
    assert report.validation_status == "download_failed_v4"
    assert not report.activated
    assert "controlled offline failure" in report.validation_messages[0]


def test_tiny_candidate_is_rejected_and_reported_without_force(tmp_path) -> None:
    raw, formatted = _payloads()

    def transport(url: str, _timeout: float) -> bytes:
        if "/formatted/items.json" in url:
            return formatted
        if "api.github.com" in url:
            return json.dumps([{"sha": "c" * 40, "commit": {"committer": {"date": None}}}]).encode()
        return raw

    database = Database(tmp_path / "tiny.db")
    database.initialize()
    repository = CatalogRepository(database)
    with pytest.raises(StaticDataError, match="minimum"):
        StaticDataClient(transport=transport).update_catalog(repository, tmp_path / "cache")
    assert repository.counts() == (0, 0)
    report = repository.latest_import_report()
    assert report is not None
    assert report.validation_status == "rejected_v4"
    assert not report.activated
    assert any("sentinel" in message for message in report.validation_messages)


def test_force_never_bypasses_malformed_ingredient(tmp_path) -> None:
    raw_payload, formatted = _payloads()
    raw = json.loads(raw_payload)
    raw["items"]["weapon"]["craftingrequirements"][0]["craftresource"][0]["@count"] = "nan"

    def transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else json.dumps(raw).encode()

    database = Database(tmp_path / "malformed.db")
    database.initialize()
    repository = CatalogRepository(database)
    with pytest.raises(StaticDataError, match="invalid quantity"):
        StaticDataClient(transport=transport).update_catalog(
            repository,
            tmp_path / "cache",
            release=StaticDataRelease("d" * 40, None),
            force=True,
        )
    assert repository.counts() == (0, 0)
    report = repository.latest_import_report()
    assert report is not None and report.forced and not report.activated


def test_rejected_relative_drop_preserves_healthy_catalog(tmp_path) -> None:
    raw_payload, formatted = _payloads()

    def original_transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else raw_payload

    database = Database(tmp_path / "preserved.db")
    database.initialize()
    repository = CatalogRepository(database)
    client = StaticDataClient(transport=original_transport)
    original = client.update_catalog(
        repository,
        tmp_path / "cache",
        release=StaticDataRelease("e" * 40, None),
        force=True,
    )

    reduced = json.loads(raw_payload)
    reduced["items"]["weapon"].pop("enchantments")

    def reduced_transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else json.dumps(reduced).encode()

    relaxed = StaticValidationPolicy(
        minimum_items=0,
        minimum_recipes=0,
        minimum_ingredients=0,
        maximum_relative_drop=0.10,
        sentinel_ids=frozenset(),
    )
    with pytest.raises(StaticDataError, match="fell"):
        StaticDataClient(transport=reduced_transport).update_catalog(
            repository,
            tmp_path / "cache",
            release=StaticDataRelease("f" * 40, None),
            validation_policy=relaxed,
        )
    assert repository.counts() == (original.item_count, original.recipe_count)
    assert repository.import_metadata() == original


def test_missing_ingredient_reference_is_structural_even_when_forced(tmp_path) -> None:
    raw_payload, formatted = _payloads()
    raw = json.loads(raw_payload)
    raw["items"]["simpleitem"] = [
        row for row in raw["items"]["simpleitem"] if row["@uniquename"] != "T4_ARTEFACT_SWORD"
    ]

    def transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else json.dumps(raw).encode()

    database = Database(tmp_path / "orphan.db")
    database.initialize()
    repository = CatalogRepository(database)
    with pytest.raises(StaticDataError, match="absent from catalog items"):
        StaticDataClient(transport=transport).update_catalog(
            repository,
            tmp_path / "cache",
            release=StaticDataRelease("1" * 40, None),
            force=True,
        )
    assert repository.counts() == (0, 0)


def test_duplicate_ingredient_identity_is_structural_even_when_forced(tmp_path) -> None:
    raw_payload, formatted = _payloads()
    raw = json.loads(raw_payload)
    duplicate = raw["items"]["weapon"]["craftingrequirements"][0]["craftresource"]
    duplicate[1]["@uniquename"] = duplicate[0]["@uniquename"]

    def transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else json.dumps(raw).encode()

    database = Database(tmp_path / "duplicate.db")
    database.initialize()
    repository = CatalogRepository(database)
    with pytest.raises(StaticDataError, match="duplicate ingredient identities"):
        StaticDataClient(transport=transport).update_catalog(
            repository,
            tmp_path / "cache",
            release=StaticDataRelease("5" * 40, None),
            force=True,
        )
    assert repository.counts() == (0, 0)


def test_same_version_with_damaged_catalog_is_repaired(tmp_path) -> None:
    raw, formatted = _payloads()

    def transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else raw

    policy = StaticValidationPolicy(
        minimum_items=0,
        minimum_recipes=0,
        minimum_ingredients=0,
        sentinel_ids=frozenset(),
    )
    database = Database(tmp_path / "repair.db")
    database.initialize()
    repository = CatalogRepository(database)
    release = StaticDataRelease("2" * 40, None)
    client = StaticDataClient(transport=transport)
    client.update_catalog(
        repository,
        tmp_path / "cache",
        release=release,
        validation_policy=policy,
    )
    with database.connection() as connection:
        connection.execute(
            "DELETE FROM catalog_materials WHERE output_item_id='T4_SWORD' AND position=0"
        )
    assert len(repository.get_recipe("T4_SWORD").materials) == 1

    client.update_catalog(
        repository,
        tmp_path / "cache",
        release=release,
        validation_policy=policy,
    )
    assert len(repository.get_recipe("T4_SWORD").materials) == 2


def test_replacement_failure_rolls_back_catalog_and_metadata(tmp_path, monkeypatch) -> None:
    raw, formatted = _payloads()

    def transport(url: str, _timeout: float) -> bytes:
        return formatted if "/formatted/items.json" in url else raw

    policy = StaticValidationPolicy(
        minimum_items=0,
        minimum_recipes=0,
        minimum_ingredients=0,
        sentinel_ids=frozenset(),
    )
    database = Database(tmp_path / "transaction.db")
    database.initialize()
    repository = CatalogRepository(database)
    client = StaticDataClient(transport=transport)
    original = client.update_catalog(
        repository,
        tmp_path / "cache",
        release=StaticDataRelease("3" * 40, None),
        validation_policy=policy,
    )

    def fail_report(_connection, _report) -> None:
        raise RuntimeError("injected report failure")

    monkeypatch.setattr(CatalogRepository, "_insert_import_report", staticmethod(fail_report))
    with pytest.raises(RuntimeError, match="injected"):
        client.update_catalog(
            repository,
            tmp_path / "cache",
            release=StaticDataRelease("4" * 40, None),
            validation_policy=policy,
        )
    assert repository.import_metadata() == original
    assert repository.counts() == (original.item_count, original.recipe_count)
