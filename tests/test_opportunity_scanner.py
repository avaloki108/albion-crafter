from dataclasses import replace
from datetime import UTC, datetime, timedelta

from albion_crafter.core.actionability import ReasonCode, ReasonSeverity
from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillProfile,
)
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import (
    StationFeeObservation,
    StationType,
)
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import MarketPrice, Region
from albion_crafter.opportunity.models import (
    CancellationToken,
    OpportunitySort,
    ScanConstraints,
)
from albion_crafter.opportunity.scanner import OpportunityScanner

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _recipe(index: int = 0) -> Recipe:
    return Recipe(
        output=Item(
            f"T4_MAIN_SWORD{index or ''}",
            f"Broadsword {index}",
            4,
            crafting_category="sword",
        ),
        output_quantity=1,
        materials=(MaterialRequirement(f"T4_METALBAR{index or ''}", 2, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="test-static",
    )


def _price(item_id: str, city: str, price: int, *, quality: int = 1) -> MarketPrice:
    return MarketPrice(
        item_id=item_id,
        city=city,
        quality=quality,
        region=Region.AMERICAS,
        sell_price=price,
        sell_price_timestamp=NOW - timedelta(minutes=5),
        buy_price=price - 10,
        buy_price_timestamp=NOW - timedelta(minutes=5),
        fetched_at=NOW,
        provenance=Provenance.AODP_CACHED,
    )


def _fee(city: str = "Lymhurst", displayed_fee: float = 500) -> StationFeeObservation:
    return StationFeeObservation(
        Region.AMERICAS.value,
        city,
        StationType.WARRIORS_FORGE,
        displayed_fee,
        NOW,
    )


def _constraints(**changes) -> ScanConstraints:
    base = ScanConstraints(
        region=Region.AMERICAS,
        craft_cities=("Lymhurst",),
        sell_cities=("Bridgewatch",),
        actionable_only=False,
        available_focus=10_000,
    )
    return replace(base, **changes)


def _prices(recipe: Recipe, material_city: str = "Lymhurst") -> tuple[MarketPrice, ...]:
    return (
        _price(recipe.materials[0].item_id, material_city, 100),
        _price(recipe.output.item_id, "Bridgewatch", 1_000),
    )


def _scan(
    *,
    recipe: Recipe | None = None,
    profile: CraftingSkillProfile | None = None,
    fees: tuple[StationFeeObservation, ...] | None = None,
    constraints: ScanConstraints | None = None,
    prices: tuple[MarketPrice, ...] | None = None,
):
    selected_recipe = recipe or _recipe()
    return OpportunityScanner().scan(
        (selected_recipe,),
        prices or _prices(selected_recipe),
        (),
        (_fee(),) if fees is None else fees,
        profile or CraftingSkillProfile(),
        constraints or _constraints(),
        as_of=NOW,
    )


def test_scanner_evaluates_full_city_matrix_from_preloaded_data() -> None:
    recipe = _recipe()
    constraints = _constraints(
        material_city="Thetford",
        craft_cities=("Lymhurst", "Thetford"),
        sell_cities=("Bridgewatch", "Martlock"),
    )
    prices = (
        _price(recipe.materials[0].item_id, "Thetford", 100),
        _price(recipe.output.item_id, "Bridgewatch", 1_000),
        _price(recipe.output.item_id, "Martlock", 1_100),
    )
    snapshot = OpportunityScanner().scan(
        (recipe,),
        prices,
        (),
        (_fee("Lymhurst"), _fee("Thetford")),
        CraftingSkillProfile(),
        constraints,
        as_of=NOW,
        database_load_operations=6,
    )

    assert snapshot.recipes_considered == 1
    assert snapshot.scenarios_evaluated == 4
    assert len(snapshot.opportunities) == 4
    assert {value.material_city for value in snapshot.opportunities} == {"Thetford"}
    assert snapshot.database_load_operations == 6


def test_unknown_station_fee_is_visible_but_never_actionable() -> None:
    snapshot = _scan(fees=())
    opportunity = snapshot.opportunities[0]
    assert opportunity.calculation.raw_material_cost == 200
    assert opportunity.calculation.profit is None
    assert opportunity.station_displayed_fee is None
    assert ReasonCode.UNKNOWN_STATION_FEE in {
        reason.code for reason in opportunity.calculation.actionability.reasons
    }


def test_scanner_calculates_estimated_profit_from_cached_daily_sell_history() -> None:
    recipe = _recipe()
    history = tuple(
        MarketHistoryInterval(
            item_id=recipe.output.item_id,
            city="Bridgewatch",
            quality=1,
            region=Region.AMERICAS,
            observed_at=NOW - timedelta(days=day),
            item_count=25,
            average_price=1_000 + day,
            time_scale=HistoryTimeScale.DAILY,
            fetched_at=NOW,
        )
        for day in range(1, 8)
    )
    snapshot = OpportunityScanner().scan(
        (recipe,),
        (_price(recipe.materials[0].item_id, "Lymhurst", 100),),
        (),
        (_fee(),),
        CraftingSkillProfile(),
        _constraints(actionable_only=True),
        price_history=history,
        as_of=NOW,
    )

    assert len(snapshot.opportunities) == 1
    opportunity = snapshot.opportunities[0]
    assert opportunity.profit is not None
    assert opportunity.pricing.live_price_count == 1
    assert opportunity.pricing.historical_estimate_count == 1
    output = next(line for line in opportunity.pricing.evidence if line.role == "output")
    assert output.source.value == "HISTORICAL_ESTIMATE"
    estimate_reasons = [
        reason
        for reason in opportunity.calculation.actionability.reasons
        if reason.code is ReasonCode.HISTORICAL_PRICE_ESTIMATE
    ]
    assert len(estimate_reasons) == 1
    assert estimate_reasons[0].severity is ReasonSeverity.WARNING


def test_scanner_capital_includes_sell_order_setup_cash_and_drives_filter() -> None:
    low = _scan(fees=(_fee(displayed_fee=100),)).opportunities[0]
    high = _scan(fees=(_fee(displayed_fee=900),)).opportunities[0]
    assert low.profit is not None and high.profit is not None
    assert low.profit > high.profit
    assert low.upfront_capital_required is not None
    assert high.upfront_capital_required is not None
    assert low.upfront_capital_required < high.upfront_capital_required
    assert low.upfront_capital_required > low.calculation.effective_material_cost
    assert low.calculation.listing_setup_cash is not None
    assert low.calculation.listing_setup_cash > 0
    assert low.upfront_capital_required == low.calculation.total_pre_revenue_cash_required
    assert low.calculation.upfront_capital_required is not None
    assert low.upfront_capital_required == (
        low.calculation.upfront_capital_required + low.calculation.listing_setup_cash
    )

    excluded = _scan(
        fees=(_fee(displayed_fee=100),),
        constraints=_constraints(
            maximum_upfront_capital=low.upfront_capital_required - 1,
        ),
    )
    assert excluded.opportunities == ()


def test_scanner_values_returns_at_optional_craft_city_price() -> None:
    recipe = _recipe()
    result = _scan(
        recipe=recipe,
        constraints=_constraints(material_city="Thetford"),
        prices=(
            _price(recipe.materials[0].item_id, "Thetford", 100),
            _price(recipe.materials[0].item_id, "Lymhurst", 250),
            _price(recipe.output.item_id, "Bridgewatch", 1_000),
        ),
    ).opportunities[0]

    assert result.pricing.returned_material_craft_city_prices == {
        recipe.materials[0].item_id: 250.0
    }
    assert result.calculation.returned_material_cost_basis_value is not None
    assert result.calculation.returned_material_craft_city_market_value is not None
    assert result.calculation.returned_material_craft_city_market_value > (
        result.calculation.returned_material_cost_basis_value
    )
    informational = next(
        line for line in result.pricing.evidence if line.role == "returned_material_informational"
    )
    assert informational.city == "Lymhurst"


def test_focus_scan_uses_recipe_specific_profile_and_capacity() -> None:
    profile = CraftingSkillProfile(
        skill_levels=(
            CraftingSkillLevel("sword:main_sword", "sword", 50, 30),
            CraftingSkillLevel("bow:longbow", "bow", 100, 30),
        ),
        complete_groups=frozenset({"sword", "bow"}),
    )
    snapshot = _scan(
        profile=profile,
        constraints=_constraints(use_focus=True, available_focus=250),
    )
    opportunity = snapshot.opportunities[0]
    assert opportunity.focus_efficiency == 14_000
    assert opportunity.focus_efficiency_source == "derived_profile"
    assert opportunity.calculation.focus_used is not None
    assert opportunity.maximum_focus_crafts == int(250 // opportunity.calculation.focus_used)
    assert ReasonCode.INSUFFICIENT_FOCUS not in {
        reason.code for reason in opportunity.calculation.actionability.reasons
    }


def test_unknown_focus_profile_blocks_only_focused_scenario() -> None:
    focused = _scan(constraints=_constraints(use_focus=True)).opportunities[0]
    unfocused = _scan(constraints=_constraints(use_focus=False)).opportunities[0]
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION in {
        reason.code for reason in focused.calculation.actionability.reasons
    }
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION not in {
        reason.code for reason in unfocused.calculation.actionability.reasons
    }


def test_artifact_recipe_does_not_reuse_ordinary_derived_specialization() -> None:
    ordinary = _recipe()
    recipe = replace(
        ordinary,
        materials=(
            ordinary.materials[0],
            MaterialRequirement("T4_ARTIFACT", 1, False),
        ),
    )
    profile = CraftingSkillProfile(
        skill_levels=(CraftingSkillLevel("sword:main_sword", "sword", 100, 30),),
        complete_groups=frozenset({"sword"}),
    )
    prices = (
        _price(recipe.materials[0].item_id, "Lymhurst", 100),
        _price("T4_ARTIFACT", "Lymhurst", 500),
        _price(recipe.output.item_id, "Bridgewatch", 2_000),
    )

    result = _scan(
        recipe=recipe,
        profile=profile,
        constraints=_constraints(use_focus=True),
        prices=prices,
    ).opportunities[0]

    assert result.focus_efficiency is None
    assert result.focus_efficiency_source == "unknown"
    assert ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION in {
        reason.code for reason in result.calculation.actionability.reasons
    }


def test_non_normal_quality_is_explicitly_hypothetical() -> None:
    recipe = _recipe()
    prices = _prices(recipe) + (_price(recipe.output.item_id, "Bridgewatch", 2_000, quality=5),)
    result = _scan(
        recipe=recipe,
        constraints=_constraints(output_quality=5),
        prices=prices,
    ).opportunities[0]
    assert result.profit is not None
    assert ReasonCode.UNSUPPORTED_OUTPUT_QUALITY in {
        reason.code for reason in result.calculation.actionability.reasons
    }


def test_ranking_and_actionable_filter_are_backend_concerns() -> None:
    recipes = (_recipe(1), _recipe(2), _recipe(3))
    prices = tuple(
        row
        for index, recipe in enumerate(recipes, 1)
        for row in (
            _price(recipe.materials[0].item_id, "Lymhurst", 100),
            _price(recipe.output.item_id, "Bridgewatch", index * 500),
        )
    )
    snapshot = OpportunityScanner().scan(
        recipes,
        prices,
        (),
        (_fee(),),
        CraftingSkillProfile(),
        _constraints(sort_by=OpportunitySort.PROFIT, actionable_only=True),
        as_of=NOW,
    )
    assert [value.item_id for value in snapshot.opportunities] == [
        recipes[2].output.item_id,
        recipes[1].output.item_id,
        recipes[0].output.item_id,
    ]


def test_cancellation_returns_an_honest_partial_snapshot() -> None:
    token = CancellationToken()

    def cancel_after_first(progress) -> None:
        if progress.completed:
            token.cancel()

    recipe = _recipe()
    snapshot = OpportunityScanner().scan(
        (recipe,),
        _prices(recipe),
        (),
        (_fee(),),
        CraftingSkillProfile(),
        _constraints(sell_cities=tuple(f"Sell city {index}" for index in range(25))),
        as_of=NOW,
        progress=cancel_after_first,
        cancellation=token,
    )
    assert snapshot.cancelled
    assert 0 < snapshot.scenarios_evaluated < 25


def test_liquidity_history_enriches_but_does_not_control_actionability() -> None:
    recipe = _recipe()
    key = (Region.AMERICAS, recipe.output.item_id, "Bridgewatch", 1)
    intervals = tuple(
        MarketHistoryInterval(
            item_id=recipe.output.item_id,
            city="Bridgewatch",
            quality=1,
            region=Region.AMERICAS,
            observed_at=NOW - timedelta(hours=6 * offset),
            item_count=25,
            average_price=1_000,
            time_scale=HistoryTimeScale.SIX_HOURLY,
            fetched_at=NOW,
            provenance=Provenance.AODP_CACHED,
        )
        for offset in range(5)
    )
    result = (
        OpportunityScanner()
        .scan(
            (recipe,),
            _prices(recipe),
            (),
            (_fee(),),
            CraftingSkillProfile(),
            _constraints(),
            history_by_key={key: intervals},
            history_status_by_key={key: "success"},
            as_of=NOW,
        )
        .opportunities[0]
    )

    assert result.liquidity is not None
    assert result.liquidity.level is LiquidityLevel.HIGH
    assert result.liquidity.reported_volume == 125
    assert result.calculation.actionability.is_actionable


def test_no_history_is_unknown_without_inventing_volume() -> None:
    result = _scan().opportunities[0]
    assert result.liquidity is not None
    assert result.liquidity.level is LiquidityLevel.UNKNOWN
    assert result.liquidity.reported_volume is None
    warning = next(
        reason
        for reason in result.calculation.actionability.reasons
        if reason.code is ReasonCode.UNKNOWN_LIQUIDITY
    )
    assert warning.severity is ReasonSeverity.WARNING
    assert result.calculation.actionability.is_actionable


def test_low_liquidity_is_an_explicit_warning_not_an_automatic_blocker() -> None:
    recipe = _recipe()
    key = (Region.AMERICAS, recipe.output.item_id, "Bridgewatch", 1)
    interval = MarketHistoryInterval(
        item_id=recipe.output.item_id,
        city="Bridgewatch",
        quality=1,
        region=Region.AMERICAS,
        observed_at=NOW - timedelta(hours=6),
        item_count=1,
        average_price=1_000,
        time_scale=HistoryTimeScale.SIX_HOURLY,
        fetched_at=NOW,
        provenance=Provenance.AODP_CACHED,
    )
    result = (
        OpportunityScanner()
        .scan(
            (recipe,),
            _prices(recipe),
            (),
            (_fee(),),
            CraftingSkillProfile(),
            _constraints(),
            history_by_key={key: (interval,)},
            history_status_by_key={key: "success"},
            as_of=NOW,
        )
        .opportunities[0]
    )

    assert result.liquidity is not None
    assert result.liquidity.level is LiquidityLevel.LOW
    warning = next(
        reason
        for reason in result.calculation.actionability.reasons
        if reason.code is ReasonCode.LOW_LIQUIDITY
    )
    assert warning.severity is ReasonSeverity.WARNING
    assert result.calculation.actionability.is_actionable


def test_pre_cancelled_scan_does_no_scenario_work_or_ranking() -> None:
    token = CancellationToken()
    token.cancel()
    recipe = _recipe()

    snapshot = OpportunityScanner().scan(
        (recipe,),
        _prices(recipe),
        (),
        (_fee(),),
        CraftingSkillProfile(),
        _constraints(),
        as_of=NOW,
        cancellation=token,
    )

    assert snapshot.cancelled
    assert snapshot.recipes_considered == 1
    assert snapshot.scenarios_evaluated == 0
    assert snapshot.opportunities == ()
