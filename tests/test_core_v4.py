from datetime import UTC, datetime, timedelta

import pytest

from albion_crafter.core.actionability import ActionabilityAssessment, ReasonCode
from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.freshness import Freshness, FreshnessPolicy
from albion_crafter.core.mechanics import CURRENT_RULES
from albion_crafter.core.models import (
    CraftingContext,
    Item,
    MaterialRequirement,
    Recipe,
    SaleMethod,
)
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import (
    StationFeeObservation,
    StationType,
    resolve_station_fee,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


@pytest.fixture
def recipe() -> Recipe:
    return Recipe(
        Item("T4_MAIN_SWORD", "Broadsword", 4, crafting_category="sword"),
        1,
        (MaterialRequirement("T4_METALBAR", 10, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
    )


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=2), Freshness.FRESH),
        (timedelta(hours=13), Freshness.AGING),
        (timedelta(hours=25), Freshness.STALE),
    ],
)
def test_station_fee_freshness_uses_configurable_policy(age, expected, recipe) -> None:
    observation = StationFeeObservation(
        "americas",
        "Bridgewatch",
        StationType.WARRIORS_FORGE,
        500,
        NOW - age,
    )
    result = resolve_station_fee(
        recipe.output,
        region="americas",
        city="Bridgewatch",
        observations=(observation,),
        freshness_policy=FreshnessPolicy(timedelta(hours=24)),
        as_of=NOW,
    )
    assert result.freshness is expected


def test_future_station_fee_beyond_clock_skew_is_explicitly_non_actionable(recipe) -> None:
    observation = StationFeeObservation(
        "americas",
        "Bridgewatch",
        StationType.WARRIORS_FORGE,
        500,
        NOW + timedelta(minutes=5),
    )
    policy = FreshnessPolicy(timedelta(hours=24))
    resolution = resolve_station_fee(
        recipe.output,
        region="americas",
        city="Bridgewatch",
        observations=(observation,),
        freshness_policy=policy,
        as_of=NOW,
    )
    result = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            station_fee_observation=observation,
            station_fee_freshness_policy=policy,
            as_of=NOW,
        ),
        ActionabilityAssessment(),
    )

    assert resolution.freshness is Freshness.FUTURE
    assert ReasonCode.FUTURE_STATION_FEE_TIMESTAMP in {
        reason.code for reason in result.actionability.blocking_reasons
    }


@pytest.mark.parametrize("bad_fee", [float("nan"), float("inf"), float("-inf")])
def test_station_fee_rejects_non_finite_values(bad_fee: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        StationFeeObservation(
            "americas",
            "Bridgewatch",
            StationType.WARRIORS_FORGE,
            bad_fee,
            NOW,
        )


def test_stale_station_fee_blocks_then_new_observation_restores_actionability(recipe) -> None:
    policy = FreshnessPolicy(timedelta(hours=24))
    stale = StationFeeObservation(
        "americas",
        "Bridgewatch",
        StationType.WARRIORS_FORGE,
        500,
        NOW - timedelta(hours=31),
    )
    fresh = StationFeeObservation(
        "americas",
        "Bridgewatch",
        StationType.WARRIORS_FORGE,
        500,
        NOW,
    )

    def calculate(observation):
        return CraftCalculator().calculate(
            recipe,
            {"T4_METALBAR": 100},
            2_000,
            CraftingContext(
                "Bridgewatch",
                "Bridgewatch",
                station_fee_observation=observation,
                station_fee_freshness_policy=policy,
                as_of=NOW,
            ),
            ActionabilityAssessment(),
        )

    stale_result = calculate(stale)
    fresh_result = calculate(fresh)
    assert ReasonCode.STALE_STATION_FEE in {
        reason.code for reason in stale_result.actionability.blocking_reasons
    }
    assert fresh_result.actionability.is_actionable


def test_timestamp_free_fee_is_unknown_under_age_policy(recipe) -> None:
    result = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            station_usage_fee_percent=500,
            station_fee_freshness_policy=FreshnessPolicy(timedelta(hours=24)),
            as_of=NOW,
        ),
        ActionabilityAssessment(),
    )
    assert ReasonCode.UNKNOWN_STATION_FEE_TIMESTAMP in {
        reason.code for reason in result.actionability.blocking_reasons
    }


def test_sell_order_cash_timing_separates_setup_from_transaction_tax(recipe) -> None:
    sell_order = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            station_usage_fee_percent=0,
            sale_method=SaleMethod.SELL_ORDER,
        ),
        ActionabilityAssessment(),
    )
    instant = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Bridgewatch",
            "Bridgewatch",
            station_usage_fee_percent=0,
            sale_method=SaleMethod.INSTANT_SELL,
        ),
        ActionabilityAssessment(),
    )
    assert sell_order.gross_material_purchase_cash == 1_000
    assert sell_order.listing_setup_cash == 50
    assert sell_order.transaction_tax == 80
    assert sell_order.total_pre_revenue_cash_required == 1_050
    assert instant.listing_setup_cash == 0
    assert instant.total_pre_revenue_cash_required == 1_000
    assert instant.transaction_tax == 80


def test_cross_city_return_value_is_informational_and_not_double_counted(recipe) -> None:
    base = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext("Thetford", "Bridgewatch", station_usage_fee_percent=0),
        ActionabilityAssessment(),
    )
    valued = CraftCalculator().calculate(
        recipe,
        {"T4_METALBAR": 100},
        2_000,
        CraftingContext(
            "Thetford",
            "Bridgewatch",
            material_buy_city="Martlock",
            station_usage_fee_percent=0,
        ),
        ActionabilityAssessment(),
        returned_material_craft_city_prices={"T4_METALBAR": 250},
    )
    assert valued.returned_material_cost_basis_value == base.returned_material_cost_basis_value
    assert valued.returned_material_craft_city_market_value is not None
    assert valued.returned_material_craft_city_market_value > (
        valued.returned_material_cost_basis_value or 0
    )
    assert valued.profit == base.profit


def test_mechanics_health_warns_without_invalidating_verified_ruleset() -> None:
    health = CURRENT_RULES.verification_health(as_of=datetime(2027, 1, 1, tzinfo=UTC))
    assert health.is_aging
    assert health.warning == "Mechanics rules were last verified 135 days ago."
    assert health.verification_status is CURRENT_RULES.verification_status
    assert health.source_references
