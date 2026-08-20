import pytest

from albion_crafter.core.focus import calculate_focus_cost
from albion_crafter.core.mechanics import CURRENT_RULES
from albion_crafter.core.models import Item, SaleMethod


@pytest.mark.parametrize(
    ("city", "category", "expected"),
    [
        ("Lymhurst", "sword", 0.33),
        ("Bridgewatch", "crossbow", 0.33),
        ("Thetford", "firestaff", 0.33),
        ("Martlock", "sword", 0.18),
        ("Fort Sterling", "bag", 0.18),
    ],
)
def test_royal_city_baseline_and_known_specialties(
    city: str, category: str, expected: float
) -> None:
    item = Item("ITEM", "Item", 4, crafting_category=category)
    assert CURRENT_RULES.production_bonus(item, city, use_focus=False) == pytest.approx(expected)


def test_focus_adds_59_percentage_points() -> None:
    sword = Item("SWORD", "Sword", 4, crafting_category="sword")
    assert CURRENT_RULES.production_bonus(sword, "Lymhurst", use_focus=True) == pytest.approx(0.92)


def test_unverified_refining_category_stays_unknown() -> None:
    ore = Item("BAR", "Bar", 4, crafting_category="ore")
    assert CURRENT_RULES.production_bonus(ore, "Bridgewatch", use_focus=False) is None


@pytest.mark.parametrize(
    ("efficiency", "expected"),
    [(0, 1_000), (10_000, 500), (20_000, 250), (5_000, 1_000 / 2**0.5)],
)
def test_focus_cost_efficiency_reference_cases(efficiency: float, expected: float) -> None:
    assert calculate_focus_cost(1_000, efficiency) == pytest.approx(expected)


def test_market_fee_rules_are_explicit_by_sale_method_and_premium() -> None:
    assert CURRENT_RULES.total_market_fee_rate(
        premium=True, sale_method=SaleMethod.SELL_ORDER
    ) == pytest.approx(0.065)
    assert CURRENT_RULES.total_market_fee_rate(
        premium=False, sale_method=SaleMethod.SELL_ORDER
    ) == pytest.approx(0.105)
    assert CURRENT_RULES.total_market_fee_rate(
        premium=True, sale_method=SaleMethod.INSTANT_SELL
    ) == pytest.approx(0.04)
