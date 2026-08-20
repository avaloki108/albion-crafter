from dataclasses import replace
from datetime import UTC, datetime, timedelta

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import CraftingProfileRepository, StationFeeRepository
from albion_crafter.market.models import (
    Freshness,
    MarketPrice,
    MarketSide,
    Region,
    UserPriceOverride,
)
from albion_crafter.planning.models import (
    FindMoneyConstraints,
    PlanReasonCode,
    TransportPolicy,
)
from albion_crafter.planning.preflight import (
    FindMoneyPreflightPlanner,
    ObservationDisposition,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _price(item_id: str, city: str, price: int, observed_at: datetime) -> MarketPrice:
    return MarketPrice(
        item_id=item_id,
        city=city,
        quality=1,
        region=Region.AMERICAS,
        sell_price=price,
        sell_price_timestamp=observed_at,
        buy_price=price - 1,
        buy_price_timestamp=observed_at,
        fetched_at=NOW,
        provenance=Provenance.AODP_CACHED,
    )


def _repositories(tmp_path):
    database = Database(tmp_path / "preflight.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    fees = StationFeeRepository(database)
    profiles = CraftingProfileRepository(database)
    output = Item("T4_MAIN_SWORD", "Broadsword", 4, crafting_category="sword")
    material = Item("T4_METALBAR", "Steel Bar", 4)
    recipe = Recipe(
        output,
        1,
        (MaterialRequirement(material.item_id, 16, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="preflight-static",
    )
    catalog.replace_all(
        [
            CatalogItem(output, 100, True, Provenance.STATIC_GAME_DATA, "preflight-static"),
            CatalogItem(material, 10, False, Provenance.STATIC_GAME_DATA, "preflight-static"),
        ],
        [recipe],
        CatalogImport(
            "fixture",
            "memory://fixture",
            "preflight-static",
            NOW,
            NOW,
            2,
            1,
        ),
    )
    return catalog, market, overrides, fees, profiles, recipe


def _constraints(**changes) -> FindMoneyConstraints:
    base = FindMoneyConstraints(
        available_silver=1_000_000,
        available_focus=10_000,
        material_cities=("Bridgewatch", "Martlock"),
        craft_cities=("Bridgewatch",),
        sell_cities=("Thetford",),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
    )
    return replace(base, **changes)


def _planner(repositories) -> FindMoneyPreflightPlanner:
    catalog, market, overrides, fees, profiles, _ = repositories
    return FindMoneyPreflightPlanner(catalog, market, overrides, fees, profiles)


def _fresh_fee(repository: StationFeeRepository, city: str = "Bridgewatch") -> None:
    repository.set(
        StationFeeObservation(
            Region.AMERICAS.value,
            city,
            StationType.WARRIORS_FORGE,
            500,
            NOW,
        )
    )


def test_preflight_deduplicates_keys_and_reuses_only_acceptable_cache(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, market, _, fees, _, recipe = repositories
    _fresh_fee(fees)
    market.upsert_many(
        (
            _price(recipe.materials[0].item_id, "Bridgewatch", 100, NOW),
            _price(
                recipe.materials[0].item_id,
                "Martlock",
                90,
                NOW - timedelta(hours=5),
            ),
        )
    )
    preflight = _planner(repositories).build(_constraints(), as_of=NOW)

    assert preflight.summary.candidate_recipes == 1
    assert preflight.summary.eligible_recipe_routes == 2
    assert preflight.summary.required_current_price_keys == 3
    assert preflight.summary.fresh_cached_requirements == 1
    assert preflight.summary.refresh_requirements == 2
    assert preflight.summary.estimated_aodp_batches == 2
    dispositions = {value.disposition for value in preflight.market_refresh.assessments}
    assert dispositions >= {
        ObservationDisposition.FRESH,
        ObservationDisposition.STALE,
        ObservationDisposition.MISSING,
    }


def test_force_refresh_marks_every_required_key_without_fetching_optional_return_value(
    tmp_path,
) -> None:
    repositories = _repositories(tmp_path)
    _, market, _, fees, _, recipe = repositories
    _fresh_fee(fees)
    market.upsert_many(
        (
            _price(recipe.materials[0].item_id, "Bridgewatch", 100, NOW),
            _price(recipe.materials[0].item_id, "Martlock", 90, NOW),
            _price(recipe.output.item_id, "Thetford", 1_000, NOW),
        )
    )
    preflight = _planner(repositories).build(
        _constraints(force_current_price_refresh=True),
        as_of=NOW,
    )
    assert len(preflight.market_refresh.refresh_keys) == 3
    assert preflight.summary.refresh_requirements == 3


def test_force_refresh_never_adds_optional_override_to_sparse_execution_keys(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, _, overrides, fees, _, recipe = repositories
    _fresh_fee(fees)
    overrides.set(
        UserPriceOverride(
            recipe.materials[0].item_id,
            "Bridgewatch",
            1,
            Region.AMERICAS,
            MarketSide.SELL_ORDER,
            95,
            NOW,
        )
    )
    preflight = _planner(repositories).build(
        _constraints(
            material_cities=("Martlock",),
            force_current_price_refresh=True,
        ),
        as_of=NOW,
    )

    optional = next(
        value
        for value in preflight.market_refresh.assessments
        if not value.requirement.required_for_actionability
    )
    assert optional.effective_override
    assert not optional.needs_refresh
    assert optional.requirement.key not in preflight.market_refresh.refresh_keys
    planned_keys = {
        (batch.region, item_id, batch.city, batch.quality)
        for batch in preflight.market_refresh.batches
        for item_id in batch.item_ids
    }
    assert planned_keys == {
        (key.region, key.item_id, key.city, key.quality)
        for key in preflight.market_refresh.refresh_keys
    }


def test_stale_required_override_is_preflight_attention_not_a_useless_fetch(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, _, overrides, fees, _, recipe = repositories
    _fresh_fee(fees)
    overrides.set(
        UserPriceOverride(
            recipe.output.item_id,
            "Thetford",
            1,
            Region.AMERICAS,
            MarketSide.SELL_ORDER,
            2_500,
            NOW - timedelta(hours=5),
        )
    )
    preflight = _planner(repositories).build(_constraints(), as_of=NOW)

    assert dict(preflight.rejection_counts)["stale_user_override"] == 1
    assert any("update or remove" in reason.message for reason in preflight.blockers)
    output = next(
        value
        for value in preflight.market_refresh.assessments
        if value.requirement.key.item_id == recipe.output.item_id
    )
    assert output.effective_override
    assert not output.needs_refresh
    assert output.requirement.key not in preflight.market_refresh.refresh_keys


def test_future_required_override_is_attention_and_cannot_trigger_aodp_replacement(
    tmp_path,
) -> None:
    repositories = _repositories(tmp_path)
    _, _, overrides, fees, _, recipe = repositories
    _fresh_fee(fees)
    overrides.set(
        UserPriceOverride(
            recipe.output.item_id,
            "Thetford",
            1,
            Region.AMERICAS,
            MarketSide.SELL_ORDER,
            2_500,
            NOW + timedelta(minutes=5),
        )
    )
    preflight = _planner(repositories).build(_constraints(), as_of=NOW)

    assert dict(preflight.rejection_counts)["future_user_override"] == 1
    assert any(reason.code is PlanReasonCode.FUTURE_MARKET_DATA for reason in preflight.blockers)
    assert any("future-dated" in reason.message for reason in preflight.blockers)
    output = next(
        value
        for value in preflight.market_refresh.assessments
        if value.requirement.key.item_id == recipe.output.item_id
    )
    assert output.freshness is Freshness.FUTURE
    assert output.effective_override
    assert not output.needs_refresh


def test_future_cached_market_observation_is_explicitly_refreshable(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, market, _, fees, _, recipe = repositories
    _fresh_fee(fees)
    market.upsert_many(
        (_price(recipe.output.item_id, "Thetford", 2_500, NOW + timedelta(minutes=5)),)
    )
    preflight = _planner(repositories).build(_constraints(), as_of=NOW)

    output = next(
        value
        for value in preflight.market_refresh.assessments
        if value.requirement.key.item_id == recipe.output.item_id
    )
    assert output.freshness is Freshness.FUTURE
    assert output.disposition is ObservationDisposition.FUTURE
    assert output.needs_refresh
    assert output.requirement.key in preflight.market_refresh.refresh_keys


def test_future_station_fee_is_blocking_even_when_stale_fees_are_allowed(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, _, _, fees, _, _ = repositories
    fees.set(
        StationFeeObservation(
            Region.AMERICAS.value,
            "Bridgewatch",
            StationType.WARRIORS_FORGE,
            500,
            NOW + timedelta(minutes=5),
        )
    )
    preflight = _planner(repositories).build(
        _constraints(allow_stale_station_fees=True),
        as_of=NOW,
    )

    assert not preflight.eligible
    assert dict(preflight.rejection_counts)["future_station_fee"] == 2
    requirement = preflight.station_requirements[0]
    assert requirement.freshness is Freshness.FUTURE
    assert requirement.needs_attention


def test_missing_station_fee_blocks_only_routes_that_require_it(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, _, _, fees, _, _ = repositories
    _fresh_fee(fees, "Bridgewatch")
    constraints = _constraints(
        material_cities=("Bridgewatch",),
        craft_cities=("Bridgewatch", "Martlock"),
    )
    preflight = _planner(repositories).build(constraints, as_of=NOW)
    assert {value.route.craft_city for value in preflight.eligible} == {"Bridgewatch"}
    assert dict(preflight.rejection_counts)["missing_station_fee"] == 1
    attention = preflight.attention_station_fees
    assert len(attention) == 1
    assert attention[0].city == "Martlock"


def test_unknown_focus_profile_excludes_focus_but_preserves_nonfocus_route(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, _, _, fees, _, _ = repositories
    _fresh_fee(fees)
    preflight = _planner(repositories).build(_constraints(use_focus=True), as_of=NOW)
    assert preflight.eligible
    assert not any(value.focused_variant_eligible for value in preflight.eligible)
    assert preflight.summary.unknown_focus_profiles == 1
    assert dict(preflight.rejection_counts)["focused_variant_unknown_fce"] == 2


def test_large_craft_cap_is_not_clamped_and_has_named_workload_warning(tmp_path) -> None:
    repositories = _repositories(tmp_path)
    _, _, _, fees, profiles, _ = repositories
    _fresh_fee(fees)
    profiles.save(
        CraftingSkillProfile(
            available_focus=10_000,
            assume_zero_for_unspecified=True,
        )
    )

    preflight = _planner(repositories).build(
        _constraints(per_item_craft_cap=10_000),
        as_of=NOW,
    )

    assert preflight.constraints.per_item_craft_cap == 10_000
    assert preflight.workload.conceptual_quantity_states == 100_030_000
    assert preflight.workload.quantity_bundle_count == 56
    assert preflight.workload.likely_approximate
    warning = next(
        reason
        for reason in preflight.blockers
        if reason.code is PlanReasonCode.APPROXIMATE_OPTIMIZATION
    )
    assert warning.severity.value == "warning"
    assert "Narrow cities, items, or the per-market action-unit/batch cap" in warning.message


def test_local_transport_policy_prunes_cross_city_universe_before_price_planning(
    tmp_path,
) -> None:
    repositories = _repositories(tmp_path)
    _, _, _, fees, _, _ = repositories
    _fresh_fee(fees)
    preflight = _planner(repositories).build(
        _constraints(
            material_cities=("Bridgewatch", "Martlock"),
            craft_cities=("Bridgewatch",),
            sell_cities=("Bridgewatch",),
            transport_policy=TransportPolicy.LOCAL_ONLY,
        ),
        as_of=NOW,
    )
    assert preflight.routes.combinations_considered == 2
    assert preflight.routes.combinations_pruned == 1
    assert len(preflight.eligible) == 1
