import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.mechanics import CURRENT_RULES
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database import PlanSnapshotRepository
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    HistoryCoverage,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.market.aodp import BatchFailure
from albion_crafter.market.history import (
    HistoryFetchResult,
    HistoryTimeScale,
    MarketHistoryInterval,
)
from albion_crafter.market.history_cache import CachedHistoryRefreshResult
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import MarketPrice, Region
from albion_crafter.planning.models import (
    FindMoneyConstraints,
    MinimumLiquidity,
    OptimizationStatus,
    PlanReasonCode,
    PlanStatus,
)
from albion_crafter.planning.preflight import FindMoneyPreflightPlanner
from albion_crafter.planning.quantity import QuantityCeilingSource
from albion_crafter.planning.service import FindMoneyService, PlanningStage
from albion_crafter.planning.validation import action_evidence_hook, validate_plan

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _stack(tmp_path, *, two_items: bool = False):
    database = Database(tmp_path / "planning-service.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    fees = StationFeeRepository(database)
    profiles = CraftingProfileRepository(database)
    history = MarketHistoryRepository(database)
    snapshots = PlanSnapshotRepository(database)

    output = Item("T4_MAIN_SWORD", "Broadsword", 4, crafting_category="sword")
    material = Item("T4_METALBAR", "Steel Bar", 4)
    recipe = Recipe(
        output,
        1,
        (MaterialRequirement(material.item_id, 16, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="service-static",
    )
    items = [
        CatalogItem(output, 100, True, Provenance.STATIC_GAME_DATA, "service-static"),
        CatalogItem(material, 10, False, Provenance.STATIC_GAME_DATA, "service-static"),
    ]
    recipes = [recipe]
    if two_items:
        second_output = Item("T4_MAIN_AXE", "Battleaxe", 4, crafting_category="axe")
        items.append(
            CatalogItem(
                second_output,
                100,
                True,
                Provenance.STATIC_GAME_DATA,
                "service-static",
            )
        )
        recipes.append(
            Recipe(
                second_output,
                1,
                (MaterialRequirement(material.item_id, 16, True),),
                item_value=100,
                base_focus_cost=200,
                provenance=Provenance.STATIC_GAME_DATA,
                source_version="service-static",
            )
        )
    catalog.replace_all(
        items,
        recipes,
        CatalogImport(
            "fixture",
            "memory://service",
            "service-static",
            NOW,
            NOW,
            len(items),
            len(recipes),
        ),
    )
    market.upsert_many(
        [
            _price(material.item_id, 100),
            _price(output.item_id, 2_500),
            *([_price("T4_MAIN_AXE", 2_100)] if two_items else []),
        ]
    )
    fees.set(
        StationFeeObservation(
            Region.AMERICAS.value,
            "Bridgewatch",
            StationType.WARRIORS_FORGE,
            500,
            NOW,
        )
    )
    profiles.save(
        CraftingSkillProfile(
            available_focus=10_000,
            assume_zero_for_unspecified=True,
        )
    )
    planner = FindMoneyPreflightPlanner(catalog, market, overrides, fees, profiles)
    service = FindMoneyService(
        planner,
        market,
        overrides,
        profiles,
        history,
        snapshots=snapshots,
        clock=lambda: NOW,
        identifier_factory=lambda _: "service-plan",
    )
    return service, history, snapshots


def _price(item_id: str, sell: int) -> MarketPrice:
    return MarketPrice(
        item_id,
        "Bridgewatch",
        1,
        Region.AMERICAS,
        sell,
        NOW,
        sell - 1,
        NOW,
        NOW,
        Provenance.AODP_CACHED,
    )


def _constraints(**changes) -> FindMoneyConstraints:
    values = dict(
        available_silver=1_000_000,
        available_focus=10_000,
        material_cities=("Bridgewatch",),
        craft_cities=("Bridgewatch",),
        sell_cities=("Bridgewatch",),
        use_focus=True,
        history_enabled=False,
    )
    values.update(changes)
    return FindMoneyConstraints(**values)


def test_service_requires_explicit_execute_and_persists_validated_snapshot(tmp_path) -> None:
    service, _, snapshots = _stack(tmp_path)
    progress = []
    preflight = service.preflight(_constraints(), as_of=NOW)

    assert snapshots.count() == 0
    assert not preflight.market_refresh.refresh_keys
    result = service.execute(
        preflight,
        refresh_current=False,
        refresh_history=False,
        progress=progress.append,
    )

    assert not result.cancelled
    assert result.snapshot is not None
    assert result.snapshot.plan_status is PlanStatus.DECISION_GRADE
    assert result.snapshot.optimizer.status is OptimizationStatus.EXACT
    assert result.snapshot.actions
    assert result.snapshot.total_pre_revenue_cash <= result.snapshot.constraints.silver_budget
    assert result.snapshot.total_focus <= result.snapshot.constraints.focus_budget
    assert result.validation is not None and result.validation.is_feasible
    assert snapshots.load("service-plan") == result.snapshot
    assert progress[0].stage is PlanningStage.PREFLIGHT
    assert progress[-1].stage is PlanningStage.COMPLETE


def test_independent_evidence_validation_rejects_tampered_accounting(tmp_path) -> None:
    service, _, _ = _stack(tmp_path)
    constraints = _constraints()
    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )
    assert result.optimization is not None and result.optimization.actions
    action = result.optimization.actions[0]
    evidence = dict(action.evidence)
    accounting = json.loads(evidence["accounting"])
    accounting["focused_per_craft"]["profit"] += 100
    evidence["accounting"] = json.dumps(accounting, sort_keys=True, separators=(",", ":"))
    tampered = replace(
        result.optimization,
        actions=(replace(action, evidence=tuple(sorted(evidence.items()))),),
    )

    validation = validate_plan(
        tampered,
        constraints,
        dict(result.ceilings),
        as_of=NOW,
        freshness_hooks=(action_evidence_hook(constraints, CURRENT_RULES),),
    )

    assert not validation.is_feasible
    assert PlanReasonCode.INVALID_ACTION_EVIDENCE in {reason.code for reason in validation.reasons}


def test_independent_evidence_validation_rejects_one_future_price_line(tmp_path) -> None:
    service, _, _ = _stack(tmp_path)
    constraints = _constraints()
    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )
    assert result.optimization is not None and result.optimization.actions
    action = result.optimization.actions[0]
    evidence = dict(action.evidence)
    prices = json.loads(evidence["prices"])
    output = next(value for value in prices if value["role"] == "output")
    output["observed_at"] = (NOW + timedelta(minutes=5)).isoformat()
    evidence["prices"] = json.dumps(prices, sort_keys=True, separators=(",", ":"))
    tampered = replace(
        result.optimization,
        actions=(replace(action, evidence=tuple(sorted(evidence.items()))),),
    )

    validation = validate_plan(
        tampered,
        constraints,
        dict(result.ceilings),
        as_of=NOW,
        freshness_hooks=(action_evidence_hook(constraints, CURRENT_RULES),),
    )

    assert not validation.is_feasible
    assert PlanReasonCode.FUTURE_MARKET_DATA in {reason.code for reason in validation.reasons}


def test_service_uses_reliable_24h_history_only_as_execution_ceiling(tmp_path) -> None:
    service, history, _ = _stack(tmp_path)
    intervals = tuple(
        MarketHistoryInterval(
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
            Region.AMERICAS,
            NOW - timedelta(hours=hours),
            50,
            2_400,
            HistoryTimeScale.SIX_HOURLY,
            NOW,
            Provenance.AODP_CACHED,
        )
        for hours in (3, 9)
    )
    history.upsert_many(intervals)
    history.set_coverage(
        HistoryCoverage(
            Region.AMERICAS,
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
            HistoryTimeScale.SIX_HOURLY,
            NOW - timedelta(days=7),
            NOW,
            NOW,
            "success",
            len(intervals),
        )
    )
    preflight = service.preflight(
        _constraints(history_enabled=True, historical_volume_share=0.20),
        as_of=NOW,
    )
    result = service.execute(
        preflight,
        refresh_current=False,
        refresh_history=False,
    )

    assert result.snapshot is not None
    ceiling = result.ceilings[0][1]
    assert ceiling.source is QuantityCeilingSource.HISTORICAL_VOLUME_SHARE
    assert ceiling.reported_24h_volume == 100
    assert ceiling.maximum_output_units == 20
    assert "not live order depth" in ceiling.explanation
    assert result.liquidity[0][1].reported_volume == 100


def test_cancellation_returns_no_snapshot_and_preserves_preflight(tmp_path) -> None:
    service, _, snapshots = _stack(tmp_path)
    preflight = service.preflight(_constraints(), as_of=NOW)
    result = service.execute(preflight, cancelled=lambda: True)

    assert result.cancelled
    assert result.snapshot is None
    assert result.preflight is preflight
    assert snapshots.count() == 0
    assert dict(result.rejection_counts)["cancelled"] == 1


class _FailingHistoryService:
    def __init__(self) -> None:
        self.client = SimpleNamespace(region=Region.AMERICAS)
        self.calls = []

    def refresh_outputs(
        self,
        output_item_ids,
        *,
        start_date,
        end_date,
        sell_cities,
        qualities,
        time_scale,
        is_cancelled,
    ) -> CachedHistoryRefreshResult:
        del is_cancelled
        self.calls.append((tuple(output_item_ids), tuple(sell_cities), tuple(qualities)))
        failure = BatchFailure(1, tuple(output_item_ids), "simulated history failure")
        fetch = HistoryFetchResult(
            intervals=(),
            failures=(failure,),
            record_failures=(),
            batch_count=1,
            items_requested=len(output_item_ids),
            successful_batches=0,
            elapsed_seconds=0.01,
            start_date=start_date,
            end_date=end_date,
            time_scale=time_scale,
            request_attempts=1,
            completed_batches=1,
        )
        return CachedHistoryRefreshResult(fetch, 1, 0, 0, 0, 1, 0, 0)


def test_history_failure_targets_only_shortlist_and_plan_continues(tmp_path) -> None:
    service, _, _ = _stack(tmp_path, two_items=True)
    failing = _FailingHistoryService()
    service.history_refresh = failing
    preflight = service.preflight(
        _constraints(history_enabled=True, history_shortlist_limit=1),
        as_of=NOW,
    )
    result = service.execute(preflight, refresh_current=False, refresh_history=True)

    assert failing.calls == [(("T4_MAIN_SWORD",), ("Bridgewatch",), (1,))]
    assert result.history_refresh is not None
    assert result.history_refresh.batches_failed == 1
    assert result.snapshot is not None and result.snapshot.actions
    assert result.snapshot.plan_status is PlanStatus.ADVISORY
    # History shortlisting enriches a bounded set; it no longer discards an
    # otherwise valid un-enriched route when Unknown liquidity is permitted.
    assert result.snapshot.actions[0].item_id in {"T4_MAIN_SWORD", "T4_MAIN_AXE"}
    assert result.snapshot.actions[0].liquidity is LiquidityLevel.UNKNOWN


def test_history_disabled_does_not_truncate_optimizer_to_history_shortlist(tmp_path) -> None:
    service, _, _ = _stack(tmp_path, two_items=True)
    constraints = _constraints(history_enabled=False, history_shortlist_limit=1)

    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )

    assert result.snapshot is not None
    metadata = dict(result.snapshot.metadata)
    assert metadata["history_groups_not_enriched"] == "0"
    assert metadata["optimizer_candidates"] == "2"
    assert result.snapshot.optimizer.status is OptimizationStatus.EXACT


def test_bounded_history_universe_is_explicitly_approximate_for_liquidity_filter(
    tmp_path,
) -> None:
    service, history, _ = _stack(tmp_path, two_items=True)
    intervals = tuple(
        MarketHistoryInterval(
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
            Region.AMERICAS,
            NOW - timedelta(hours=hours),
            50,
            2_400,
            HistoryTimeScale.SIX_HOURLY,
            NOW,
            Provenance.AODP_CACHED,
        )
        for hours in (3, 9)
    )
    history.upsert_many(intervals)
    history.set_coverage(
        HistoryCoverage(
            Region.AMERICAS,
            "T4_MAIN_SWORD",
            "Bridgewatch",
            1,
            HistoryTimeScale.SIX_HOURLY,
            NOW - timedelta(days=7),
            NOW,
            NOW,
            "success",
            len(intervals),
        )
    )
    constraints = _constraints(
        history_enabled=True,
        history_shortlist_limit=1,
        minimum_liquidity=MinimumLiquidity.LOW,
    )

    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )

    assert result.snapshot is not None
    assert result.snapshot.optimizer.status is OptimizationStatus.APPROXIMATE
    assert "bounded_history_shortlist" in result.snapshot.optimizer.method
    assert "bounded_history_shortlist" in result.snapshot.optimizer.approximation_reasons
    assert dict(result.snapshot.metadata)["history_groups_not_enriched"] == "1"
