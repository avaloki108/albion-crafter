from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
    ReasonSeverity,
)
from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.city_bonuses import (
    CITY_BONUS_DATASET_VERSION,
    CityBonusClassification,
)
from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillProfile,
    FocusEfficiencySource,
    ManualFocusEfficiencyOverride,
    crafting_skill_mapping_for_item,
    crafting_skill_mapping_for_recipe,
)
from albion_crafter.core.mechanics import CURRENT_RULES
from albion_crafter.core.models import (
    CraftingContext,
    CraftingProfile,
    Item,
    MaterialRequirement,
    Recipe,
)
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import (
    StationFeeObservation,
    StationType,
    resolve_station_fee,
    station_type_for_item,
)


@pytest.fixture
def sword_recipe() -> Recipe:
    return Recipe(
        output=Item(
            "T4_MAIN_SWORD",
            "Adept's Broadsword",
            4,
            crafting_category="sword",
            max_quality=5,
        ),
        output_quantity=1,
        materials=(MaterialRequirement("T4_METALBAR", 10, returnable=True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
    )


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (Item("T4_MAIN_SWORD", "Sword", 4, crafting_category="sword"), StationType.WARRIORS_FORGE),
        (Item("T4_BAG", "Bag", 4, crafting_category="bag"), StationType.TOOLMAKER),
        (Item("T4_MEAL", "Food", 4, crafting_category="food"), StationType.COOK),
        (Item("T3_FLOUR", "Flour", 3, crafting_category="food"), StationType.MILL),
        (
            Item("T6_ALCOHOL", "Potato Schnapps", 6, crafting_category="food"),
            StationType.ALCHEMIST_LAB,
        ),
        (Item("T4_POTION", "Potion", 4, crafting_category="potion"), StationType.ALCHEMISTS_LAB),
        (
            Item("T4_SHIELD", "Shield", 4, subcategory="shieldtype", crafting_category="offhand"),
            StationType.WARRIORS_FORGE,
        ),
        (
            Item("T4_TORCH", "Torch", 4, subcategory="torchtype", crafting_category="offhand"),
            StationType.HUNTERS_LODGE,
        ),
        (
            Item("T4_BOOK", "Book", 4, subcategory="booktype", crafting_category="offhand"),
            StationType.MAGES_TOWER,
        ),
    ],
)
def test_station_mapping_is_structured_and_category_specific(
    item: Item, expected: StationType
) -> None:
    assert station_type_for_item(item) is expected


def test_station_fee_resolution_uses_region_city_station_and_latest_observation() -> None:
    item = Item("T4_MAIN_SWORD", "Sword", 4, crafting_category="sword")
    now = datetime.now(UTC)
    observations = (
        StationFeeObservation("americas", "Thetford", StationType.WARRIORS_FORGE, 500, now),
        StationFeeObservation(
            "americas",
            "Thetford",
            StationType.WARRIORS_FORGE,
            475,
            now + timedelta(minutes=1),
        ),
        StationFeeObservation("europe", "Thetford", StationType.WARRIORS_FORGE, 100, now),
    )
    resolution = resolve_station_fee(
        item, region="americas", city="Thetford", observations=observations
    )
    assert resolution.is_known
    assert resolution.displayed_fee == 475
    assert resolution.provenance is Provenance.USER_OVERRIDE


def test_item_skill_mapping_is_stable_across_tier_and_enchantment() -> None:
    tier_four = crafting_skill_mapping_for_item(
        Item("T4_MAIN_SWORD@3", "Sword", 4, enchantment=3, crafting_category="sword")
    )
    tier_eight = crafting_skill_mapping_for_item(
        Item("T8_MAIN_SWORD", "Sword", 8, crafting_category="sword")
    )
    assert tier_four is not None
    assert tier_eight is not None
    assert tier_four.mapping_key == tier_eight.mapping_key == "sword/main_sword"
    assert tier_four.specialization_skill_key == "sword:main_sword"
    assert tier_four.verified


@pytest.mark.parametrize("returnable", [False, None])
def test_recipe_mapping_requires_manual_override_for_artifact_or_uncertain_inputs(
    sword_recipe: Recipe, returnable: bool | None
) -> None:
    recipe = Recipe(
        output=sword_recipe.output,
        output_quantity=1,
        materials=(MaterialRequirement("T4_ARTIFACT", 1, returnable=returnable),),
        item_value=sword_recipe.item_value,
        base_focus_cost=sword_recipe.base_focus_cost,
        provenance=Provenance.STATIC_GAME_DATA,
    )
    item_mapping = crafting_skill_mapping_for_item(recipe.output)
    recipe_mapping = crafting_skill_mapping_for_recipe(recipe)
    assert item_mapping is not None and item_mapping.verified
    assert recipe_mapping is not None and not recipe_mapping.verified
    assert recipe_mapping.mapping_key == item_mapping.mapping_key


def test_recipe_mapping_preserves_verified_ordinary_recipe(sword_recipe: Recipe) -> None:
    assert crafting_skill_mapping_for_recipe(sword_recipe) == crafting_skill_mapping_for_item(
        sword_recipe.output
    )


def test_crafting_profile_derives_item_specific_fce_without_cross_tree_leakage() -> None:
    mapping = crafting_skill_mapping_for_item(
        Item("T4_MAIN_SWORD", "Sword", 4, crafting_category="sword")
    )
    assert mapping is not None
    profile = CraftingSkillProfile(
        available_focus=10_000,
        skill_levels=(
            CraftingSkillLevel("sword:main_sword", "sword", 50, 30),
            CraftingSkillLevel("sword:claymore", "sword", 20, 30),
            CraftingSkillLevel("bow:bow", "bow", 100, 30),
        ),
        complete_groups=frozenset({"sword"}),
    )
    resolution = profile.resolve(mapping)
    assert resolution.source is FocusEfficiencySource.DERIVED_PROFILE
    assert resolution.focus_cost_efficiency == 50 * 250 + (50 + 20) * 30


def test_unspecified_skill_group_is_unknown_unless_user_explicitly_assumes_zero() -> None:
    mapping = crafting_skill_mapping_for_item(
        Item("T4_MAIN_SWORD", "Sword", 4, crafting_category="sword")
    )
    assert mapping is not None
    unknown = CraftingSkillProfile().resolve(mapping)
    explicit_zero = CraftingSkillProfile(assume_zero_for_unspecified=True).resolve(mapping)
    assert not unknown.is_known
    assert unknown.missing_skill_keys
    assert explicit_zero.focus_cost_efficiency == 0


def test_manual_fce_override_precedes_but_does_not_replace_skill_levels() -> None:
    mapping = crafting_skill_mapping_for_item(
        Item("T4_MAIN_SWORD", "Sword", 4, crafting_category="sword")
    )
    assert mapping is not None
    level = CraftingSkillLevel("sword:main_sword", "sword", 70, 30)
    profile = CraftingSkillProfile(
        skill_levels=(level,),
        manual_fce_overrides=(
            ManualFocusEfficiencyOverride(mapping.mapping_key, 12_345, datetime.now(UTC)),
        ),
        complete_groups=frozenset({"sword"}),
    )
    resolution = profile.resolve(mapping)
    assert resolution.source is FocusEfficiencySource.MANUAL_OVERRIDE
    assert resolution.focus_cost_efficiency == 12_345
    assert profile.skill_levels == (level,)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_profile_and_manual_numeric_inputs_reject_non_finite_values(bad_value: float) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="finite"):
        ManualFocusEfficiencyOverride("sword/main_sword", bad_value, now)
    with pytest.raises(ValueError, match="finite"):
        CraftingSkillLevel("sword:main_sword", "sword", 1, bad_value)
    with pytest.raises(ValueError, match="finite"):
        CraftingSkillProfile(available_focus=bad_value)
    with pytest.raises(ValueError, match="finite"):
        CraftingProfile(available_focus=1, focus_cost_efficiency=bad_value)
    with pytest.raises(ValueError, match="finite"):
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            station_usage_fee_percent=bad_value,
        )


@pytest.mark.parametrize("bad_level", [True, 1.5])
def test_crafting_skill_level_requires_a_bounded_integer(bad_level) -> None:
    with pytest.raises(ValueError, match="integer between 0 and 100"):
        CraftingSkillLevel("sword:main_sword", "sword", bad_level, 30)


@pytest.mark.parametrize(
    ("city", "category", "expected"),
    [
        ("Brecilien", "bag", 0.33),
        ("Brecilien", "potion", 0.33),
        ("Caerleon", "food", 0.33),
        ("Thetford", "mace", 0.33),
        ("Bridgewatch", "bag", 0.18),
    ],
)
def test_versioned_city_table_covers_equipment_and_non_equipment(
    city: str, category: str, expected: float
) -> None:
    item = Item("ITEM", "Item", 4, crafting_category=category)
    resolution = CURRENT_RULES.city_bonus(item, city, use_focus=False)
    assert resolution.is_verified
    assert resolution.total_production_bonus == pytest.approx(expected)
    assert resolution.dataset_version == CITY_BONUS_DATASET_VERSION


def test_unknown_or_deferred_city_groups_never_silently_receive_baseline() -> None:
    future = CURRENT_RULES.city_bonus(
        Item("FUTURE", "Future", 4, crafting_category="future_category"),
        "Bridgewatch",
        use_focus=False,
    )
    meat = CURRENT_RULES.city_bonus(
        Item("MEAT", "Meat", 4, crafting_category="meat_goat"),
        "Thetford",
        use_focus=False,
    )
    assert future.classification is CityBonusClassification.UNKNOWN_CRAFTING_GROUP
    assert meat.classification is CityBonusClassification.UNSUPPORTED_CRAFTING_GROUP
    assert future.total_production_bonus is None
    assert meat.total_production_bonus is None


@pytest.mark.parametrize("quality", [2, 3, 4, 5])
def test_above_normal_quality_is_hypothetical_and_non_actionable(
    sword_recipe: Recipe, quality: int
) -> None:
    result = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            output_quality=quality,
            station_usage_fee_percent=0,
        ),
        data_quality=ActionabilityAssessment(),
    )
    assert result.profit is not None
    assert not result.actionability.is_actionable
    assert ReasonCode.UNSUPPORTED_OUTPUT_QUALITY in {
        reason.code for reason in result.actionability.reasons
    }


def test_normal_quality_can_remain_actionable(sword_recipe: Recipe) -> None:
    result = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext("Bridgewatch", "Bridgewatch", station_usage_fee_percent=0),
        data_quality=ActionabilityAssessment(),
    )
    assert result.actionability.is_actionable


def test_unknown_station_fee_keeps_evidence_visible_but_blocks_profit(
    sword_recipe: Recipe,
) -> None:
    result = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext("Bridgewatch", "Bridgewatch"),
        data_quality=ActionabilityAssessment(),
    )
    assert result.raw_material_cost == 1_000
    assert result.profit is None
    assert result.upfront_capital_required is None
    assert ReasonCode.UNKNOWN_STATION_FEE in {
        reason.code for reason in result.actionability.reasons
    }


def test_matching_station_observation_is_used_and_mismatched_station_is_rejected(
    sword_recipe: Recipe,
) -> None:
    now = datetime.now(UTC)
    matching = StationFeeObservation("americas", "Bridgewatch", StationType.WARRIOR_FORGE, 500, now)
    wrong_station = StationFeeObservation(
        "americas", "Bridgewatch", StationType.MAGE_TOWER, 500, now
    )
    accepted = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext("Bridgewatch", "Bridgewatch", station_fee_observation=matching),
        data_quality=ActionabilityAssessment(),
    )
    rejected = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext("Bridgewatch", "Bridgewatch", station_fee_observation=wrong_station),
        data_quality=ActionabilityAssessment(),
    )
    assert accepted.station_fee == pytest.approx(100 * 0.1125 * 5)
    assert accepted.actionability.is_actionable
    assert rejected.station_fee is None
    assert ReasonCode.UNKNOWN_STATION_FEE in {
        reason.code for reason in rejected.actionability.reasons
    }


def test_unknown_city_bonus_classification_has_a_specific_reason() -> None:
    recipe = Recipe(
        output=Item("T4_FUTURE", "Future", 4, crafting_category="future_category"),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 1, True),),
        item_value=10,
        provenance=Provenance.STATIC_GAME_DATA,
    )
    result = CraftCalculator().calculate(
        recipe,
        {"MAT": 10},
        20,
        CraftingContext("Bridgewatch", "Bridgewatch", station_usage_fee_percent=0),
        data_quality=ActionabilityAssessment(),
    )
    assert result.profit is None
    assert ReasonCode.UNKNOWN_CITY_BONUS_CLASSIFICATION in {
        reason.code for reason in result.actionability.reasons
    }


def test_recipe_specific_profile_controls_focus_cost(sword_recipe: Recipe) -> None:
    mapping = crafting_skill_mapping_for_item(sword_recipe.output)
    assert mapping is not None
    profile = CraftingSkillProfile(
        available_focus=1_000,
        skill_levels=(CraftingSkillLevel(mapping.specialization_skill_key, "sword", 40, 30),),
        complete_groups=frozenset({"sword"}),
    )
    result = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            use_focus=True,
            station_usage_fee_percent=0,
            profile=profile,
        ),
        data_quality=ActionabilityAssessment(),
    )
    expected_fce = 40 * 250 + 40 * 30
    assert result.focus_used == pytest.approx(200 * 0.5 ** (expected_fce / 10_000))
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION not in {
        reason.code for reason in result.actionability.reasons
    }


def test_artifact_recipe_rejects_derived_levels_but_accepts_manual_fce_override(
    sword_recipe: Recipe,
) -> None:
    recipe = Recipe(
        output=sword_recipe.output,
        output_quantity=1,
        materials=(
            MaterialRequirement("T4_METALBAR", 10, returnable=True),
            MaterialRequirement("T4_ARTIFACT", 1, returnable=False),
        ),
        item_value=sword_recipe.item_value,
        base_focus_cost=sword_recipe.base_focus_cost,
        provenance=Provenance.STATIC_GAME_DATA,
    )
    mapping = crafting_skill_mapping_for_recipe(recipe)
    assert mapping is not None and not mapping.verified
    derived_profile = CraftingSkillProfile(
        available_focus=1_000,
        skill_levels=(CraftingSkillLevel(mapping.specialization_skill_key, "sword", 40, 30),),
        complete_groups=frozenset({"sword"}),
    )
    manual_profile = CraftingSkillProfile(
        available_focus=1_000,
        skill_levels=derived_profile.skill_levels,
        manual_fce_overrides=(
            ManualFocusEfficiencyOverride(mapping.mapping_key, 10_000, datetime.now(UTC)),
        ),
        complete_groups=derived_profile.complete_groups,
    )
    derived = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100, "T4_ARTIFACT": 500},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            use_focus=True,
            station_usage_fee_percent=0,
            profile=derived_profile,
        ),
        data_quality=ActionabilityAssessment(),
    )
    manual = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100, "T4_ARTIFACT": 500},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            use_focus=True,
            station_usage_fee_percent=0,
            profile=manual_profile,
        ),
        data_quality=ActionabilityAssessment(),
    )
    assert derived.focus_used is None
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION in {
        reason.code for reason in derived.actionability.reasons
    }
    assert manual.focus_used == pytest.approx(100)
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION not in {
        reason.code for reason in manual.actionability.reasons
    }


def test_unknown_recipe_specialization_blocks_focused_result(sword_recipe: Recipe) -> None:
    result = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            use_focus=True,
            station_usage_fee_percent=0,
            profile=CraftingSkillProfile(available_focus=10_000),
        ),
        data_quality=ActionabilityAssessment(),
    )
    assert result.profit is not None
    assert result.focus_used is None
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION in {
        reason.code for reason in result.actionability.reasons
    }


def test_upfront_capital_does_not_subtract_expected_future_returns(
    sword_recipe: Recipe,
) -> None:
    result = CraftCalculator().calculate(
        sword_recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext("Bridgewatch", "Bridgewatch", station_usage_fee_percent=500),
        data_quality=ActionabilityAssessment(),
    )
    assert result.upfront_material_cost == 1_000
    assert result.upfront_capital_required == pytest.approx(1_000 + 100 * 0.1125 * 5)
    assert result.total_craft_cost is not None
    assert result.upfront_capital_required > result.total_craft_cost


def test_warning_reason_does_not_block_actionability() -> None:
    assessment = ActionabilityAssessment(
        (
            ActionabilityReason(
                ReasonCode.UNKNOWN_LIQUIDITY,
                "No history is cached.",
                ReasonSeverity.WARNING,
            ),
        )
    )
    assert assessment.is_actionable
    assert assessment.warnings == assessment.reasons


def test_material_purchase_city_is_separate_but_defaults_to_craft_city() -> None:
    default = CraftingContext("Thetford", "Bridgewatch")
    separate = CraftingContext("Thetford", "Bridgewatch", material_buy_city="Fort Sterling")
    assert default.effective_material_buy_city == "Thetford"
    assert separate.effective_material_buy_city == "Fort Sterling"
