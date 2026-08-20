import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillProfile,
    ManualFocusEfficiencyOverride,
    focus_skill_mapping_for_recipe,
    refining_skill_mapping_for_item,
)
from albion_crafter.core.mechanics import CURRENT_RULES, MechanicsRules, VerificationStatus
from albion_crafter.core.models import (
    ActionKind,
    CraftingContext,
    Item,
    MaterialRequirement,
    Recipe,
    SaleMethod,
)
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.returns import calculate_return_rate
from albion_crafter.core.stations import (
    StationFeeObservation,
    StationType,
    station_type_for_item,
)
from albion_crafter.data.static_importer import StaticCatalogParser
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.database.v4 import (
    FIND_MONEY_PREFERENCES_KEY,
    LEGACY_FIND_MONEY_PREFERENCES_KEY,
    V2_FIND_MONEY_PREFERENCES_KEY,
    FindMoneyPreferencesRepository,
    PlanSnapshotRepository,
)
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import MarketPrice, Region
from albion_crafter.planning.models import (
    CandidateEconomics,
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationDiagnostics,
    OptimizationStatus,
    PlanAction,
    PlanCandidate,
    PlanReasonCode,
    PlanSnapshot,
    PlanStatus,
    TransportPolicy,
)
from albion_crafter.planning.optimizer import optimize_plan
from albion_crafter.planning.preflight import FindMoneyPreflightPlanner
from albion_crafter.planning.quantity import QuantityCeiling, QuantityCeilingSource
from albion_crafter.planning.service import FindMoneyService
from albion_crafter.planning.validation import action_evidence_hook, validate_plan

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
UPSTREAM_REFINING_FIXTURE = Path(__file__).parent / "fixtures" / "refining-upstream-contract"


def _market_price(item_id: str, price: int, *, city: str = "Thetford") -> MarketPrice:
    return MarketPrice(
        item_id,
        city,
        1,
        Region.AMERICAS,
        price,
        NOW,
        price - 1,
        NOW,
        NOW,
        Provenance.AODP_CACHED,
    )


def _unified_stack(
    tmp_path,
    *,
    include_smelter_fee: bool = True,
    assume_zero_for_unspecified: bool = True,
    rules: MechanicsRules = CURRENT_RULES,
):
    database = Database(tmp_path / "unified-v05.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    fees = StationFeeRepository(database)
    profiles = CraftingProfileRepository(database)
    history = MarketHistoryRepository(database)
    snapshots = PlanSnapshotRepository(database)

    craft_output = Item("T5_MAIN_MACE", "Heavy Mace", 5, crafting_category="mace")
    craft_material = Item("T5_PLANKS", "Cedar Planks", 5)
    refine_output = Item("T5_METALBAR", "Titanium Steel Bar", 5, crafting_category="ore")
    raw_ore = Item("T5_ORE", "Titanium Ore", 5)
    lower_bar = Item("T4_METALBAR", "Steel Bar", 4)
    craft_recipe = Recipe(
        craft_output,
        1,
        (MaterialRequirement(craft_material.item_id, 2, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="unified-v5",
    )
    refine_recipe = Recipe(
        refine_output,
        1,
        (
            MaterialRequirement(raw_ore.item_id, 4, True),
            MaterialRequirement(lower_bar.item_id, 1, True),
        ),
        item_value=20,
        base_focus_cost=94,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="unified-v5",
    )
    items = (craft_output, craft_material, refine_output, raw_ore, lower_bar)
    catalog.replace_all(
        tuple(
            CatalogItem(
                item,
                100 if item is craft_output else 20,
                item in {craft_output, refine_output},
                Provenance.STATIC_GAME_DATA,
                "unified-v5",
            )
            for item in items
        ),
        (craft_recipe, refine_recipe),
        CatalogImport("fixture", "memory://unified-v5", "unified-v5", NOW, NOW, 5, 2),
    )
    market.upsert_many(
        (
            _market_price(craft_material.item_id, 100),
            _market_price(craft_output.item_id, 800),
            _market_price(raw_ore.item_id, 50),
            _market_price(lower_bar.item_id, 60),
            _market_price(refine_output.item_id, 1_000),
        )
    )
    fees.set(
        StationFeeObservation(
            Region.AMERICAS.value,
            "Thetford",
            StationType.WARRIORS_FORGE,
            500,
            NOW,
        )
    )
    if include_smelter_fee:
        fees.set(
            StationFeeObservation(
                Region.AMERICAS.value,
                "Thetford",
                StationType.SMELTER,
                500,
                NOW,
            )
        )
    profiles.save(
        CraftingSkillProfile(
            available_focus=10_000,
            assume_zero_for_unspecified=assume_zero_for_unspecified,
        )
    )
    planner = FindMoneyPreflightPlanner(catalog, market, overrides, fees, profiles, rules=rules)
    service = FindMoneyService(
        planner,
        market,
        overrides,
        profiles,
        history,
        snapshots=snapshots,
        rules=rules,
        clock=lambda: NOW,
        identifier_factory=lambda _: "unified-plan",
    )
    constraints = FindMoneyConstraints(
        available_silver=100_000,
        available_focus=10_000,
        material_cities=("Thetford",),
        craft_cities=("Thetford",),
        sell_cities=("Thetford",),
        transport_policy=TransportPolicy.LOCAL_ONLY,
        history_enabled=False,
        per_item_craft_cap=1,
    )
    return service, planner, snapshots, constraints


@pytest.mark.parametrize(
    ("family", "city", "station"),
    (
        ("ore", "Thetford", StationType.SMELTER),
        ("wood", "Fort Sterling", StationType.LUMBERMILL),
        ("hide", "Martlock", StationType.TANNER),
        ("fiber", "Lymhurst", StationType.WEAVER),
        ("rock", "Bridgewatch", StationType.STONEMASON),
    ),
)
def test_verified_refining_city_and_station_mappings(family, city, station) -> None:
    item = Item(f"T5_{family.upper()}", family.title(), 5, crafting_category=family)
    specialty = CURRENT_RULES.production_bonus_resolution(
        ActionKind.REFINE, item, city, use_focus=False
    )
    focused = CURRENT_RULES.production_bonus_resolution(
        ActionKind.REFINE, item, city, use_focus=True
    )
    nonmatching = CURRENT_RULES.production_bonus_resolution(
        ActionKind.REFINE, item, "Caerleon", use_focus=False
    )

    assert specialty.is_verified
    assert specialty.action_kind is ActionKind.REFINE
    assert specialty.baseline_bonus == pytest.approx(0.18)
    assert specialty.specialty_bonus == pytest.approx(0.40)
    assert specialty.total_production_bonus == pytest.approx(0.58)
    assert focused.total_production_bonus == pytest.approx(1.17)
    assert nonmatching.total_production_bonus == pytest.approx(0.18)
    assert station_type_for_item(item) is station


def test_refining_unknown_city_and_family_remain_unknown() -> None:
    ore = Item("T5_METALBAR", "Titanium Steel Bar", 5, crafting_category="ore")
    unknown_city = CURRENT_RULES.production_bonus_resolution(
        ActionKind.REFINE, ore, "Unknown", use_focus=False
    )
    wrong_family = CURRENT_RULES.production_bonus_resolution(
        ActionKind.REFINE,
        replace(ore, crafting_category="new_resource"),
        "Thetford",
        use_focus=False,
    )
    assert not unknown_city.is_verified and unknown_city.total_production_bonus is None
    assert not wrong_family.is_verified and wrong_family.total_production_bonus is None


@pytest.mark.parametrize(
    ("bonus", "expected"),
    ((0.18, 0.15254237), (0.58, 0.36708861), (0.77, 0.43502825), (1.17, 0.53917051)),
)
def test_refining_rrr_reference_values(bonus, expected) -> None:
    assert calculate_return_rate(bonus) == pytest.approx(expected, abs=1e-8)


def _refining_recipe(*, enchantment: int = 0) -> Recipe:
    suffix = f"@{enchantment}" if enchantment else ""
    return Recipe(
        Item(
            f"T6_METALBAR{suffix}",
            "Runite Steel Bar",
            6,
            enchantment=enchantment,
            crafting_category="ore",
        ),
        1,
        (
            MaterialRequirement("T6_ORE", 4, True),
            MaterialRequirement("T5_METALBAR", 1, True),
        ),
        item_value=20,
        base_focus_cost=164,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="fixture-v5",
    )


def test_refining_fce_is_family_and_tier_specific() -> None:
    recipe = _refining_recipe(enchantment=2)
    mapping = focus_skill_mapping_for_recipe(recipe)
    assert mapping == refining_skill_mapping_for_item(recipe.output)
    assert mapping is not None
    assert mapping.specialization_skill_key == "refining:ore:t6"
    levels = tuple(
        CraftingSkillLevel(f"refining:ore:t{tier}", "refining:ore", level, 30)
        for tier, level in zip(range(4, 9), (10, 20, 30, 40, 50), strict=True)
    ) + (CraftingSkillLevel("refining:wood:t6", "refining:wood", 100, 30),)
    profile = CraftingSkillProfile(
        skill_levels=levels,
        complete_groups=frozenset({"refining:ore"}),
    )
    resolution = profile.resolve(mapping)
    assert resolution.focus_cost_efficiency == 12_000  # 150*30 mutual + 30*250 unique

    incomplete = CraftingSkillProfile(skill_levels=levels[:1]).resolve(mapping)
    assert not incomplete.is_known
    override = ManualFocusEfficiencyOverride(mapping.mapping_key, 12_345, NOW)
    manual = CraftingSkillProfile(manual_fce_overrides=(override,)).resolve(mapping)
    assert manual.focus_cost_efficiency == 12_345


def test_refining_economics_reuse_cash_fee_and_return_primitives() -> None:
    recipe = Recipe(
        Item("T5_LEATHER", "Cured Leather", 5, crafting_category="hide"),
        1,
        (
            MaterialRequirement("T5_HIDE", 3, True),
            MaterialRequirement("T4_LEATHER", 1, True),
        ),
        item_value=10,
        base_focus_cost=94,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="fixture-v5",
    )
    result = CraftCalculator().calculate(
        recipe,
        {"T5_HIDE": 100, "T4_LEATHER": 200},
        800,
        CraftingContext(
            craft_city="Martlock",
            sell_city="Martlock",
            use_focus=False,
            station_usage_fee_percent=500,
            sale_method=SaleMethod.SELL_ORDER,
        ),
    )
    rrr = calculate_return_rate(0.58)
    assert recipe.action_kind is ActionKind.REFINE
    assert result.return_rate == pytest.approx(rrr)
    assert result.gross_material_purchase_cash == pytest.approx(500)
    assert result.returned_material_cost_basis_value == pytest.approx(500 * rrr)
    assert result.station_cash == pytest.approx(5.625)
    assert result.listing_setup_cash == pytest.approx(20)
    assert result.transaction_tax == pytest.approx(32)
    assert result.total_pre_revenue_cash_required == pytest.approx(525.625)
    assert result.net_sale_value == pytest.approx(748)
    assert result.profit == pytest.approx(748 - (500 * (1 - rrr) + 5.625))


def test_refining_static_recipe_import_and_hydration_preserve_lower_tier_input(tmp_path) -> None:
    raw = {
        "items": {
            "resource": [
                {"@uniquename": "T4_ORE", "@tier": "4", "@itemvalue": "4"},
                {
                    "@uniquename": "T4_METALBAR",
                    "@tier": "4",
                    "@craftingcategory": "ore",
                    "@itemvalue": "8",
                    "craftingrequirements": {
                        "@amountcrafted": "1",
                        "@craftingfocus": "48",
                        "craftresource": {"@uniquename": "T4_ORE", "@count": "2"},
                    },
                },
                {
                    "@uniquename": "T5_ORE",
                    "@tier": "5",
                    "@itemvalue": "8",
                },
                {
                    "@uniquename": "T5_METALBAR",
                    "@tier": "5",
                    "@craftingcategory": "ore",
                    "@itemvalue": "20",
                    "craftingrequirements": {
                        "@amountcrafted": "1",
                        "@craftingfocus": "94",
                        "craftresource": [
                            {"@uniquename": "T5_ORE", "@count": "4"},
                            {"@uniquename": "T4_METALBAR", "@count": "1"},
                        ],
                    },
                },
                {"@uniquename": "T6_ORE", "@tier": "6", "@itemvalue": "12"},
                {
                    "@uniquename": "T6_METALBAR",
                    "@tier": "6",
                    "@craftingcategory": "ore",
                    "@itemvalue": "32",
                    "craftingrequirements": {
                        "@amountcrafted": "1",
                        "@craftingfocus": "164",
                        "craftresource": [
                            {"@uniquename": "T6_ORE", "@count": "4"},
                            {"@uniquename": "T5_METALBAR", "@count": "1"},
                        ],
                    },
                },
            ]
        }
    }
    formatted = [
        {
            "UniqueName": f"T{tier}_METALBAR",
            "LocalizedNames": {"EN-US": name},
        }
        for tier, name in ((4, "Steel Bar"), (5, "Titanium Steel Bar"), (6, "Runite Steel Bar"))
    ]
    parsed = StaticCatalogParser().parse(
        json.dumps(raw).encode(), json.dumps(formatted).encode(), source_version="static-v5"
    )
    recipes = {value.output.item_id: value for value in parsed.recipes}
    assert set(recipes) == {"T4_METALBAR", "T5_METALBAR", "T6_METALBAR"}
    assert all(value.action_kind is ActionKind.REFINE for value in recipes.values())
    assert [
        (value.item_id, value.quantity, value.returnable)
        for value in recipes["T4_METALBAR"].materials
    ] == [("T4_ORE", 2, True)]
    assert [
        (value.item_id, value.quantity, value.returnable)
        for value in recipes["T5_METALBAR"].materials
    ] == [
        ("T5_ORE", 4, True),
        ("T4_METALBAR", 1, True),
    ]


def test_pinned_upstream_refining_contract_covers_every_family_and_enchantment(
    tmp_path,
) -> None:
    parsed = StaticCatalogParser().parse(
        (UPSTREAM_REFINING_FIXTURE / "items.json").read_bytes(),
        (UPSTREAM_REFINING_FIXTURE / "formatted-items.json").read_bytes(),
        source_version="5cf2e8e9b7021f98683181fa5b0e3c64575978e4",
    )
    recipes = {recipe.output.item_id: recipe for recipe in parsed.recipes}
    suffixes = {
        "wood": "PLANKS",
        "ore": "METALBAR",
        "hide": "LEATHER",
        "fiber": "CLOTH",
        "rock": "STONEBLOCK",
    }
    for family, suffix in suffixes.items():
        for tier in (4, 5, 6):
            recipe = recipes[f"T{tier}_{suffix}"]
            assert recipe.action_kind is ActionKind.REFINE
            assert recipe.output.crafting_category == family
            assert recipe.output.tier == tier
            assert recipe.output_quantity == 1
            assert recipe.materials[-1].item_id == f"T{tier - 1}_{suffix}"
            assert all(material.returnable is True for material in recipe.materials)

    enchanted = recipes["T5_METALBAR_LEVEL1@1"]
    assert enchanted.output.display_name == "Uncommon Titanium Steel Bar"
    assert enchanted.output.enchantment == 1
    assert [material.item_id for material in enchanted.materials] == [
        "T5_ORE_LEVEL1@1",
        "T4_METALBAR_LEVEL1@1",
    ]

    database = Database(tmp_path / "upstream-refining-contract.db")
    database.initialize()
    repository = CatalogRepository(database)
    repository.replace_all(
        parsed.items,
        parsed.recipes,
        CatalogImport(
            "fixture",
            "https://github.com/ao-data/ao-bin-dumps",
            "5cf2e8e9b7021f98683181fa5b0e3c64575978e4",
            NOW,
            NOW,
            len(parsed.items),
            len(parsed.recipes),
        ),
    )
    hydrated = repository.get_recipe("T5_METALBAR_LEVEL1@1")
    assert hydrated == enchanted
    assert [
        (value.item_id, value.quantity, value.returnable)
        for value in recipes["T6_METALBAR"].materials
    ] == [
        ("T6_ORE", 4, True),
        ("T5_METALBAR", 1, True),
    ]

    database = Database(tmp_path / "refining-static.db")
    database.initialize()
    catalog = CatalogRepository(database)
    catalog.replace_all(
        parsed.items,
        parsed.recipes,
        CatalogImport(
            "fixture",
            "memory://fixture",
            "static-v5",
            NOW,
            NOW,
            len(parsed.items),
            len(parsed.recipes),
        ),
    )
    for item_id, expected in recipes.items():
        hydrated = catalog.get_recipe(item_id)
        assert hydrated is not None
        assert hydrated.action_kind is ActionKind.REFINE
        assert hydrated.materials == expected.materials
        assert hydrated.base_focus_cost == expected.base_focus_cost


def _action(kind: ActionKind) -> PlanAction:
    route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Bridgewatch",
        TransportPolicy.LOCAL_ONLY,
    )
    item_id = "T5_LEATHER" if kind is ActionKind.REFINE else "T5_MAIN_AXE"
    return PlanAction(
        candidate_id=f"{kind.value}|candidate",
        item_id=item_id,
        display_name=item_id,
        route=route,
        quantity=1,
        focused_quantity=0,
        nonfocused_quantity=1,
        output_units=1,
        quality=1,
        sale_method=SaleMethod.SELL_ORDER,
        pre_revenue_cash_required=100,
        focus_required=0,
        expected_profit=20,
        liquidity=LiquidityLevel.MODERATE,
        execution_capacity_key=(Region.AMERICAS, item_id, "Bridgewatch", 1),
        quantity_ceiling=2,
        action_kind=kind,
    )


def _snapshot(action: PlanAction, *, format_version: int = 2) -> PlanSnapshot:
    constraints = FindMoneyConstraints(1_000, 100, history_enabled=False)
    optimizer = OptimizationDiagnostics(
        "test", OptimizationStatus.EXACT, 1, 1, 1, 1, 0, 100, False, 0
    )
    return PlanSnapshot(
        "snapshot",
        NOW,
        NOW,
        Region.AMERICAS,
        constraints,
        (action,),
        100,
        0,
        20,
        900,
        100,
        PlanStatus.DECISION_GRADE,
        (),
        optimizer,
        "static-v5",
        CURRENT_RULES.ruleset_id,
        snapshot_format_version=format_version,
    )


def test_snapshot_v2_round_trips_refining_and_v1_defaults_to_craft() -> None:
    refined = _snapshot(_action(ActionKind.REFINE))
    payload = refined.to_dict()
    assert payload["snapshot_format_version"] == 2
    assert payload["actions"][0]["action_kind"] == "refine"
    assert payload["actions"][0]["route"]["production_city"] == "Bridgewatch"
    assert PlanSnapshot.from_dict(payload) == refined

    current = _snapshot(_action(ActionKind.REFINE), format_version=3)
    current_payload = current.to_dict()
    assert current_payload["snapshot_format_version"] == 3
    assert current_payload["actions"][0]["capacity_requirements"][0]["role"] == "liquidation"
    assert PlanSnapshot.from_dict(current_payload) == current

    legacy = _snapshot(_action(ActionKind.CRAFT), format_version=1)
    legacy_payload = legacy.to_dict()
    assert "action_kind" not in legacy_payload["actions"][0]
    assert "craft_city" in legacy_payload["actions"][0]["route"]
    loaded = PlanSnapshot.from_dict(legacy_payload)
    assert loaded.actions[0].action_kind is ActionKind.CRAFT
    assert loaded.constraints.action_kinds == frozenset({ActionKind.CRAFT})


def test_legacy_preferences_migrate_as_crafting_only_without_schema_change(tmp_path) -> None:
    database = Database(tmp_path / "preferences.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    legacy = FindMoneyConstraints(1_000, 100).to_dict(legacy=True)
    preferences.settings.set(
        LEGACY_FIND_MONEY_PREFERENCES_KEY,
        {"format_version": 1, "constraints": legacy},
    )
    loaded = preferences.load()
    assert loaded is not None
    assert loaded.action_kinds == frozenset({ActionKind.CRAFT})
    migrated = preferences.settings.get(FIND_MONEY_PREFERENCES_KEY)
    assert migrated["format_version"] == 3
    assert migrated["constraints"]["action_kinds"] == ["craft"]
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_v2_preferences_retain_craft_refine_and_leave_arbitrage_disabled(tmp_path) -> None:
    database = Database(tmp_path / "preferences-v2.db")
    database.initialize()
    preferences = FindMoneyPreferencesRepository(database)
    original = FindMoneyConstraints(
        1_000,
        100,
        action_kinds=frozenset({ActionKind.CRAFT, ActionKind.REFINE}),
    )
    v2_payload = {
        "format_version": 2,
        "constraints": original.to_dict(format_version=2),
    }
    preferences.settings.set(V2_FIND_MONEY_PREFERENCES_KEY, v2_payload)

    loaded = preferences.load()
    assert loaded is not None
    assert loaded.action_kinds == frozenset({ActionKind.CRAFT, ActionKind.REFINE})
    assert ActionKind.ARBITRAGE not in loaded.action_kinds
    assert preferences.settings.get(V2_FIND_MONEY_PREFERENCES_KEY) == v2_payload
    migrated = preferences.settings.get(FIND_MONEY_PREFERENCES_KEY)
    assert migrated["format_version"] == 3
    assert migrated["constraints"]["action_kinds"] == ["craft", "refine"]


def test_unified_optimizer_selects_mixed_actions_and_shares_focus_and_silver() -> None:
    route = CandidateRoute(
        Region.AMERICAS,
        "Bridgewatch",
        "Bridgewatch",
        "Bridgewatch",
        TransportPolicy.LOCAL_ONLY,
    )
    craft = PlanCandidate(
        "craft|candidate",
        "CRAFT_OUTPUT",
        "Craft output",
        route,
        CandidateEconomics(100, 10, 40, 10),
        action_kind=ActionKind.CRAFT,
    )
    refine = PlanCandidate(
        "refine|candidate",
        "REFINE_OUTPUT",
        "Refine output",
        route,
        CandidateEconomics(100, 20, 60, 10),
        action_kind=ActionKind.REFINE,
    )
    ceilings = {
        candidate.execution_capacity_key: QuantityCeiling(
            candidate.execution_capacity_key,
            1,
            1,
            QuantityCeilingSource.EXPLICIT_CAP,
            explanation="fixture",
        )
        for candidate in (craft, refine)
    }
    constraints = FindMoneyConstraints(
        200,
        10,
        history_enabled=False,
        per_item_craft_cap=1,
    )
    result = optimize_plan((craft, refine), ceilings, constraints)

    assert result.diagnostics.status is OptimizationStatus.EXACT
    assert {action.action_kind for action in result.actions} == {
        ActionKind.CRAFT,
        ActionKind.REFINE,
    }
    assert result.total_pre_revenue_cash == 200
    assert result.total_focus == 10
    assert result.total_expected_profit == 70
    focused = next(action for action in result.actions if action.focused_quantity)
    assert focused.action_kind is ActionKind.REFINE


@pytest.mark.parametrize(
    ("craft_profit", "refine_profit", "expected"),
    (
        (40, 60, ActionKind.REFINE),
        (70, 30, ActionKind.CRAFT),
    ),
)
def test_action_kinds_compete_in_one_optimizer(craft_profit, refine_profit, expected) -> None:
    route = CandidateRoute(
        Region.AMERICAS,
        "Thetford",
        "Thetford",
        "Thetford",
        TransportPolicy.LOCAL_ONLY,
    )
    candidates = tuple(
        PlanCandidate(
            f"{kind.value}|shared-output",
            "SHARED_OUTPUT",
            "Shared output",
            route,
            CandidateEconomics(100, profit),
            action_kind=kind,
        )
        for kind, profit in (
            (ActionKind.CRAFT, craft_profit),
            (ActionKind.REFINE, refine_profit),
        )
    )
    key = candidates[0].execution_capacity_key
    assert key is not None
    result = optimize_plan(
        candidates,
        {
            key: QuantityCeiling(
                key,
                1,
                1,
                QuantityCeilingSource.EXPLICIT_CAP,
                explanation="one shared output-market batch",
            )
        },
        FindMoneyConstraints(200, 0, history_enabled=False, per_item_craft_cap=1),
    )

    assert len(result.actions) == 1
    assert result.actions[0].action_kind is expected
    assert result.actions[0].quantity == 1


def test_mixed_preflight_is_sparse_network_free_and_keeps_unknown_refining_fce_nonfocus(
    tmp_path,
) -> None:
    service, _, _, constraints = _unified_stack(tmp_path)
    preflight = service.preflight(constraints, as_of=NOW)

    assert preflight.summary.crafting_recipes == 1
    assert preflight.summary.refining_recipes == 1
    assert preflight.summary.crafting_recipe_routes == 1
    assert preflight.summary.refining_recipe_routes == 1
    assert not preflight.market_refresh.refresh_keys
    required = {
        (
            value.requirement.key.item_id,
            value.requirement.side.value,
            value.requirement.role.value,
        )
        for value in preflight.market_refresh.assessments
        if value.requirement.required_for_actionability
    }
    assert {value[0] for value in required} == {
        "T5_PLANKS",
        "T5_MAIN_MACE",
        "T5_ORE",
        "T4_METALBAR",
        "T5_METALBAR",
    }
    assert ("T5_METALBAR", "sell_order", "output") in required

    unknown_service, _, _, unknown_constraints = _unified_stack(
        tmp_path / "unknown-profile",
        assume_zero_for_unspecified=False,
    )
    refine_only = replace(
        unknown_constraints,
        action_kinds=frozenset({ActionKind.REFINE}),
    )
    unknown = unknown_service.preflight(refine_only, as_of=NOW)
    assert len(unknown.eligible) == 1
    assert not unknown.eligible[0].focused_variant_eligible
    assert unknown.summary.unknown_focus_profiles == 1


def test_missing_refining_station_fee_is_visible_without_blocking_crafting(tmp_path) -> None:
    service, _, _, constraints = _unified_stack(tmp_path, include_smelter_fee=False)
    preflight = service.preflight(constraints, as_of=NOW)

    assert {value.action_kind for value in preflight.eligible} == {ActionKind.CRAFT}
    missing = next(value for value in preflight.station_requirements if value.observation is None)
    assert missing.station_type is StationType.SMELTER
    assert missing.needs_attention
    assert dict(preflight.rejection_counts)["missing_station_fee"] == 1


def test_unified_service_builds_and_independently_validates_mixed_plan(tmp_path) -> None:
    service, _, snapshots, constraints = _unified_stack(tmp_path)
    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )

    assert result.snapshot is not None
    assert result.validation is not None and result.validation.is_feasible
    assert result.snapshot.optimizer.status is OptimizationStatus.EXACT
    assert {action.action_kind for action in result.snapshot.actions} == {
        ActionKind.CRAFT,
        ActionKind.REFINE,
    }
    assert all(
        action.candidate_id.startswith(f"{action.action_kind.value}|")
        for action in result.snapshot.actions
    )
    assert snapshots.load("unified-plan") == result.snapshot

    assert result.optimization is not None
    refine = next(
        action for action in result.optimization.actions if action.action_kind is ActionKind.REFINE
    )
    evidence = dict(refine.evidence)
    recipe = json.loads(evidence["recipe"])
    recipe["materials"][0]["quantity"] += 1
    evidence["recipe"] = json.dumps(recipe, sort_keys=True, separators=(",", ":"))
    tampered_actions = tuple(
        replace(refine, evidence=tuple(sorted(evidence.items()))) if action is refine else action
        for action in result.optimization.actions
    )
    validation = validate_plan(
        replace(result.optimization, actions=tampered_actions),
        constraints,
        dict(result.ceilings),
        as_of=NOW,
        freshness_hooks=(action_evidence_hook(constraints, CURRENT_RULES),),
    )
    assert not validation.is_feasible
    assert PlanReasonCode.INVALID_ACTION_EVIDENCE in {reason.code for reason in validation.reasons}


def test_v2_preferences_round_trip_refining_selection(tmp_path) -> None:
    database = Database(tmp_path / "v2-preferences.db")
    database.initialize()
    repository = FindMoneyPreferencesRepository(database)
    constraints = FindMoneyConstraints(
        10_000,
        5_000,
        action_kinds=frozenset({ActionKind.REFINE}),
        refining_families=frozenset({"ore", "wood"}),
    )
    repository.save(constraints)

    assert repository.load() == constraints
    payload = repository.settings.get(FIND_MONEY_PREFERENCES_KEY)
    assert payload["format_version"] == 3
    assert payload["constraints"]["action_kinds"] == ["refine"]
    assert payload["constraints"]["refining_families"] == ["ore", "wood"]


def test_provisional_refining_component_only_downgrades_dependent_plan(tmp_path) -> None:
    rules = replace(
        CURRENT_RULES,
        component_statuses=tuple(
            (
                name,
                VerificationStatus.PROVISIONAL if name == "refining_city_bonuses" else status,
            )
            for name, status in CURRENT_RULES.verification_components
        ),
    )
    service, _, _, constraints = _unified_stack(tmp_path, rules=rules)
    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )

    assert result.validation is not None
    assert not result.validation.is_feasible
    dependency_reasons = [
        reason.message
        for reason in result.validation.reasons
        if "depends on provisional mechanics" in reason.message
    ]
    assert len(dependency_reasons) == 1
    assert dependency_reasons[0].startswith("Action refine|")
    assert "refining_city_bonuses" in dependency_reasons[0]
