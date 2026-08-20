from dataclasses import replace
from datetime import timedelta

import pytest

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.market.models import Region
from albion_crafter.opportunity.filtering import filter_recipes
from albion_crafter.opportunity.models import ScanConstraints


def _recipe(item_id: str, name: str, tier: int, enchantment: int, category: str) -> Recipe:
    return Recipe(
        output=Item(item_id, name, tier, enchantment, crafting_category=category),
        output_quantity=1,
        materials=(MaterialRequirement("MAT", 1, True),),
    )


def _constraints(**changes) -> ScanConstraints:
    base = ScanConstraints(
        region=Region.AMERICAS,
        craft_cities=("Thetford",),
        sell_cities=("Bridgewatch",),
        maximum_price_age=timedelta(hours=4),
    )
    return replace(base, **changes)


def test_recipe_filters_are_composable_and_have_no_hidden_limit() -> None:
    recipes = tuple(
        _recipe(f"T{i}_MAIN_SWORD@1", f"Sword {i}", i, 1, "sword") for i in range(1, 151)
    )
    selected = filter_recipes(
        recipes,
        _constraints(
            text="sword",
            tier_min=4,
            tier_max=8,
            enchantments=(1,),
            crafting_categories=("sword",),
        ),
    )
    assert len(selected) == 5
    assert [recipe.output.tier for recipe in selected] == [4, 5, 6, 7, 8]


def test_recipe_filter_matches_canonical_id_and_rejects_unknown_tier() -> None:
    recipes = (
        _recipe("T4_MAIN_SWORD", "Broadsword", 4, 0, "sword"),
        Recipe(
            output=Item("UNIQUE_THING", "Mystery", None, crafting_category="sword"),
            output_quantity=1,
            materials=(MaterialRequirement("MAT", 1, True),),
        ),
    )
    assert filter_recipes(recipes, _constraints(text="main_sword")) == (recipes[0],)
    assert filter_recipes(recipes, _constraints(tier_min=1)) == (recipes[0],)


@pytest.mark.parametrize(
    "changes",
    (
        {"craft_cities": ("Thetford", "theTFord")},
        {"sell_cities": ("Bridgewatch", "Bridgewatch")},
        {"enchantments": (1, 1)},
        {"enchantments": (5,)},
        {"crafting_categories": ("sword", "Sword")},
        {"available_focus": float("nan")},
        {"minimum_profit": float("inf")},
        {"minimum_roi": float("-inf")},
        {"maximum_upfront_capital": float("nan")},
        {"liquidity_levels": ("Very liquid",)},
        {"crafts": True},
        {"sort_by": "profit"},
    ),
)
def test_scan_constraints_reject_ambiguous_or_nonfinite_inputs(changes) -> None:
    with pytest.raises(ValueError):
        replace(_constraints(), **changes)
