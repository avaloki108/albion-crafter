import pytest

from albion_crafter.core.actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
    ReasonSeverity,
)
from albion_crafter.core.calculator import (
    CraftCalculator,
    calculate_break_even_price,
    calculate_material_cost,
)
from albion_crafter.core.models import (
    CraftingContext,
    CraftingProfile,
    Item,
    MaterialRequirement,
    Recipe,
    SaleMethod,
)
from albion_crafter.core.provenance import Provenance


@pytest.fixture
def recipe() -> Recipe:
    return Recipe(
        output=Item("OUTPUT", "Output", 4, crafting_category="bag"),
        output_quantity=1,
        materials=(
            MaterialRequirement("RETURNABLE", 10, returnable=True),
            MaterialRequirement("ARTIFACT", 1, returnable=False),
        ),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
    )


def test_material_cost_separates_non_returnable_materials(recipe: Recipe) -> None:
    returnable, non_returnable, missing = calculate_material_cost(
        recipe.materials, {"RETURNABLE": 100, "ARTIFACT": 500}, crafts=2
    )
    assert returnable == 2_000
    assert non_returnable == 1_000
    assert missing == ()


def test_full_profit_uses_verified_rules_and_station_ui_units(recipe: Recipe) -> None:
    context = CraftingContext(
        craft_city="Bridgewatch",
        sell_city="Bridgewatch",
        station_usage_fee_percent=500,
    )
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100, "ARTIFACT": 500},
        3_000,
        context,
        data_quality=ActionabilityAssessment(),
    )
    returned = 1_000 * (1 - 1 / 1.18)
    expected_station = 100 * 0.1125 * 5
    expected_cost = 1_500 - returned + expected_station
    expected_fees = 3_000 * (0.025 + 0.04)
    assert result.expected_returned_material_value == pytest.approx(returned)
    assert result.station_fee == expected_station
    assert result.total_craft_cost == pytest.approx(expected_cost)
    assert result.market_fees == expected_fees
    assert result.profit == pytest.approx(3_000 - expected_fees - expected_cost)
    assert result.break_even_price == pytest.approx(expected_cost / 0.935)
    assert result.actionability.is_actionable


def test_focus_fields_use_incremental_profit_and_fce(recipe: Recipe) -> None:
    context = CraftingContext(
        craft_city="Bridgewatch",
        sell_city="Bridgewatch",
        use_focus=True,
        station_usage_fee_percent=0,
        profile=CraftingProfile(available_focus=1_000, focus_cost_efficiency=10_000),
    )
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100, "ARTIFACT": 500},
        2_000,
        context,
        data_quality=ActionabilityAssessment(),
    )
    no_focus_return = 1_000 * (1 - 1 / 1.18)
    focus_return = 1_000 * (1 - 1 / 1.77)
    assert result.incremental_focus_profit == pytest.approx(focus_return - no_focus_return)
    assert result.focus_used == 100
    assert result.focus_shortfall == 0
    assert result.silver_per_focus == pytest.approx(
        result.incremental_focus_profit / result.focus_used
    )


def test_insufficient_focus_keeps_arithmetic_visible_but_not_actionable(
    recipe: Recipe,
) -> None:
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100, "ARTIFACT": 500},
        2_000,
        CraftingContext(
            craft_city="Bridgewatch",
            sell_city="Bridgewatch",
            use_focus=True,
            station_usage_fee_percent=0,
            profile=CraftingProfile(available_focus=50),
        ),
        data_quality=ActionabilityAssessment(),
    )
    assert result.profit is not None
    assert result.focus_used == 200
    assert result.focus_shortfall == 150
    assert not result.actionability.is_actionable
    assert ReasonCode.INSUFFICIENT_FOCUS in {reason.code for reason in result.actionability.reasons}


def test_missing_market_data_is_explicit(recipe: Recipe) -> None:
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100},
        None,
        CraftingContext(
            craft_city="Bridgewatch",
            sell_city="Bridgewatch",
            station_usage_fee_percent=0,
        ),
        data_quality=ActionabilityAssessment(),
    )
    assert not result.is_complete
    assert result.profit is None
    assert result.raw_material_cost is None
    assert result.missing_price_item_ids == ("ARTIFACT", "OUTPUT")
    assert {reason.code for reason in result.actionability.reasons} >= {
        ReasonCode.MISSING_MATERIAL_PRICE,
        ReasonCode.MISSING_OUTPUT_PRICE,
    }


def test_numeric_prices_without_provenance_are_never_actionable(recipe: Recipe) -> None:
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100, "ARTIFACT": 500},
        2_000,
        CraftingContext(craft_city="Bridgewatch", sell_city="Bridgewatch"),
    )
    assert not result.actionability.is_actionable
    assert any(
        reason.code is ReasonCode.UNTRUSTED_PROVENANCE for reason in result.actionability.reasons
    )


def test_hybrid_live_and_demo_quality_reason_blocks_actionability(recipe: Recipe) -> None:
    demo_quality = ActionabilityAssessment(
        (
            ActionabilityReason(
                ReasonCode.UNTRUSTED_PROVENANCE,
                "ARTIFACT material price uses demo_sample data.",
            ),
        )
    )
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100, "ARTIFACT": 500},
        2_000,
        CraftingContext(
            craft_city="Bridgewatch",
            sell_city="Bridgewatch",
            station_usage_fee_percent=0,
        ),
        data_quality=demo_quality,
    )
    assert result.is_calculable
    assert not result.actionability.is_actionable


def test_quality_is_a_scenario_dimension_not_static_item_identity() -> None:
    item = Item("T4_MAIN_SWORD", "Adept's Broadsword", 4, max_quality=5)
    context = CraftingContext(craft_city="Lymhurst", sell_city="Thetford", output_quality=4)
    assert not hasattr(item, "quality")
    assert context.output_quality == 4


def test_multi_craft_top_of_book_estimate_is_actionable_with_execution_warning(
    recipe: Recipe,
) -> None:
    result = CraftCalculator().calculate(
        recipe,
        {"RETURNABLE": 100, "ARTIFACT": 500},
        2_000,
        CraftingContext(
            craft_city="Bridgewatch",
            sell_city="Bridgewatch",
            crafts=2,
            station_usage_fee_percent=0,
        ),
        data_quality=ActionabilityAssessment(),
    )
    assert result.profit is not None
    depth = next(
        reason
        for reason in result.actionability.reasons
        if reason.code is ReasonCode.TOP_OF_BOOK_DEPTH_UNMODELED
    )
    assert depth.severity is ReasonSeverity.WARNING
    assert result.actionability.is_actionable


def test_unknown_returnability_is_not_guessed() -> None:
    uncertain = Recipe(
        output=Item("OUT", "Output", 4, crafting_category="bag"),
        output_quantity=1,
        materials=(MaterialRequirement("CAPPED_COMPONENT", 1, returnable=None),),
        item_value=10,
        provenance=Provenance.STATIC_GAME_DATA,
    )
    result = CraftCalculator().calculate(
        uncertain,
        {"CAPPED_COMPONENT": 100},
        200,
        CraftingContext(craft_city="Bridgewatch", sell_city="Bridgewatch"),
        data_quality=ActionabilityAssessment(),
    )
    assert not result.is_calculable
    assert ReasonCode.UNKNOWN_RETURNABILITY in {
        reason.code for reason in result.actionability.reasons
    }


def test_break_even_for_instant_sell_omits_setup_fee() -> None:
    value = calculate_break_even_price(900, 1, 0.1, 0.05, SaleMethod.INSTANT_SELL)
    assert value == pytest.approx(1_000)
