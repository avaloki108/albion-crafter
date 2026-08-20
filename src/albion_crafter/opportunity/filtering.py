from __future__ import annotations

from collections.abc import Iterable

from albion_crafter.core.models import Recipe

from .models import CraftOpportunity, OpportunitySort, ScanConstraints


def filter_recipes(
    recipes: Iterable[Recipe],
    constraints: ScanConstraints,
) -> tuple[Recipe, ...]:
    """Apply deterministic catalog filters without imposing an analysis limit."""

    needle = constraints.text.strip().casefold()
    enchantments = set(constraints.enchantments)
    categories = {value.casefold() for value in constraints.crafting_categories}
    selected: list[Recipe] = []
    for recipe in recipes:
        item = recipe.output
        if (
            needle
            and needle not in item.item_id.casefold()
            and needle not in item.display_name.casefold()
        ):
            continue
        if constraints.tier_min is not None and (
            item.tier is None or item.tier < constraints.tier_min
        ):
            continue
        if constraints.tier_max is not None and (
            item.tier is None or item.tier > constraints.tier_max
        ):
            continue
        if enchantments and item.enchantment not in enchantments:
            continue
        if categories and item.crafting_category.casefold() not in categories:
            continue
        selected.append(recipe)
    return tuple(selected)


def opportunity_passes_filters(
    opportunity: CraftOpportunity,
    constraints: ScanConstraints,
) -> bool:
    result = opportunity.calculation
    if constraints.actionable_only and not result.actionability.is_actionable:
        return False
    if constraints.minimum_profit is not None and (
        result.profit is None or result.profit < constraints.minimum_profit
    ):
        return False
    if constraints.minimum_roi is not None and (
        result.roi is None or result.roi < constraints.minimum_roi
    ):
        return False
    if constraints.maximum_upfront_capital is not None and (
        opportunity.upfront_capital_required is None
        or opportunity.upfront_capital_required > constraints.maximum_upfront_capital
    ):
        return False
    if constraints.liquidity_levels:
        level = (
            getattr(opportunity.liquidity, "level", None)
            if opportunity.liquidity is not None
            else None
        )
        value = getattr(level, "value", level) or "Unknown"
        if str(value) not in constraints.liquidity_levels:
            return False
    return True


def sort_opportunities(
    opportunities: Iterable[CraftOpportunity],
    metric: OpportunitySort,
    *,
    descending: bool = True,
) -> tuple[CraftOpportunity, ...]:
    attribute = {
        OpportunitySort.PROFIT: "profit",
        OpportunitySort.ROI: "roi",
        OpportunitySort.MARGIN: "margin",
        OpportunitySort.SILVER_PER_FOCUS: "silver_per_focus",
    }[metric]

    with_values: list[tuple[float, CraftOpportunity]] = []
    without_values: list[CraftOpportunity] = []
    for opportunity in opportunities:
        value = getattr(opportunity, attribute)
        if value is None:
            without_values.append(opportunity)
        else:
            with_values.append((float(value), opportunity))
    with_values.sort(
        key=lambda entry: (
            entry[0],
            entry[1].item_id,
            entry[1].craft_city,
            entry[1].sell_city,
        ),
        reverse=descending,
    )
    without_values.sort(key=lambda value: (value.item_id, value.craft_city, value.sell_city))
    return tuple(value for _, value in with_values) + tuple(without_values)
