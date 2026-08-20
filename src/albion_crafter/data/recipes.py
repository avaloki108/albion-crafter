from albion_crafter.core.models import MaterialRequirement, Recipe

from .items import ITEMS

# MVP demonstration recipes only. Quantities, item values, and Focus costs are
# intentionally not presented as verified live-game metadata.
RECIPES: tuple[Recipe, ...] = (
    Recipe(
        output=ITEMS["T4_MAIN_SWORD"],
        output_quantity=1,
        materials=(
            MaterialRequirement("T4_METALBAR", 16, returnable=True),
            MaterialRequirement("T4_PLANKS", 8, returnable=True),
        ),
        item_value=96,
        base_focus_cost=180,
    ),
    Recipe(
        output=ITEMS["T5_MAIN_AXE"],
        output_quantity=1,
        materials=(
            MaterialRequirement("T5_METALBAR", 16, returnable=True),
            MaterialRequirement("T5_PLANKS", 8, returnable=True),
            MaterialRequirement("T5_ARTEFACT_MAIN_AXE_KEEPER", 1, returnable=False),
        ),
        item_value=192,
        base_focus_cost=360,
    ),
    Recipe(
        output=ITEMS["T5_BAG"],
        output_quantity=1,
        materials=(
            MaterialRequirement("T5_LEATHER", 8, returnable=True),
            MaterialRequirement("T5_CLOTH", 8, returnable=True),
        ),
        item_value=128,
        base_focus_cost=270,
    ),
)


def recipe_by_id(item_id: str) -> Recipe:
    for recipe in RECIPES:
        if recipe.output.item_id == item_id:
            return recipe
    raise KeyError(item_id)


def all_market_item_ids() -> tuple[str, ...]:
    ids: set[str] = set()
    for recipe in RECIPES:
        ids.add(recipe.output.item_id)
        ids.update(material.item_id for material in recipe.materials)
    return tuple(sorted(ids))
