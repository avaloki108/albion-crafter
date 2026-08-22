from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.core.freshness import Freshness
from albion_crafter.core.models import ActionKind, Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.database.v4 import (
    FindMoneyPreferencesRepository,
    PlanSnapshotRepository,
)
from albion_crafter.market.aodp import AODPClient
from albion_crafter.market.models import MarketPrice, MarketSide, Region
from albion_crafter.market.sync import RoyalMarketSyncService, RoyalMarketUniverseService
from albion_crafter.planning.current_refresh import CurrentMarketRefreshExecutor
from albion_crafter.planning.models import (
    ArbitrageScope,
    FindMoneyConstraints,
    MarketKey,
    MinimumLiquidity,
    PriceRequirement,
    PriceRole,
    TransportPolicy,
)
from albion_crafter.planning.preflight import (
    FindMoneyPreflightPlanner,
    ObservationDisposition,
    PriceRequirementAssessment,
)
from albion_crafter.planning.service import FindMoneyService, PlanningProgress, PlanningStage
from albion_crafter.ui.common import age_text
from albion_crafter.ui.find_money import (
    MAX_INLINE_PRICE_OVERRIDES,
    SIMPLE_MODE_OPTIMIZER_LIMITS,
    FindMoneyView,
    TrustPreset,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def test_future_age_text_is_never_rendered_as_newly_observed() -> None:
    assert age_text(NOW + timedelta(minutes=2), now=NOW) == "<1m"
    assert age_text(NOW + timedelta(minutes=5), now=NOW) == ("Future-dated by 5m (invalid)")


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class RecordingService:
    def __init__(self, service: FindMoneyService) -> None:
        self.service = service
        self.preflight_calls = 0
        self.execute_calls = 0
        self.last_execute_kwargs = None

    def preflight(self, constraints):
        self.preflight_calls += 1
        return self.service.preflight(constraints)

    def execute(self, preflight, **kwargs):
        self.execute_calls += 1
        self.last_execute_kwargs = kwargs
        return self.service.execute(preflight, **kwargs)


class CancellableService(RecordingService):
    def __init__(self, service: FindMoneyService) -> None:
        super().__init__(service)
        self.started = Event()

    def execute(self, preflight, **kwargs):
        self.execute_calls += 1
        cancelled = kwargs["cancelled"]
        self.started.set()
        deadline = time.monotonic() + 5
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.002)
        return self.service.execute(
            preflight,
            refresh_current=False,
            refresh_history=False,
            cancelled=lambda: True,
        )


def _stack(
    tmp_path,
    *,
    with_fee: bool = True,
    fee_observed_at: datetime = NOW,
    include_refining: bool = False,
    include_arbitrage: bool = False,
):
    database = Database(tmp_path / "find-money-ui.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    fees = StationFeeRepository(database)
    profiles = CraftingProfileRepository(database)
    history = MarketHistoryRepository(database)
    snapshots = PlanSnapshotRepository(database)
    preferences = FindMoneyPreferencesRepository(database)

    output = Item("T4_MAIN_SWORD", "Broadsword", 4, crafting_category="sword")
    material = Item("T4_METALBAR", "Steel Bar", 4)
    recipe = Recipe(
        output,
        1,
        (MaterialRequirement(material.item_id, 16, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="ui-static",
    )
    items = [
        CatalogItem(output, 100, True, Provenance.STATIC_GAME_DATA, "ui-static"),
        CatalogItem(material, 10, False, Provenance.STATIC_GAME_DATA, "ui-static"),
    ]
    recipes = [recipe]
    prices = [_price(material.item_id, 100), _price(output.item_id, 2_500)]
    if include_arbitrage:
        prices.append(_price(output.item_id, 4_000, city="Thetford"))
    if include_refining:
        refined = Item("T4_STONEBLOCK", "Travertine Block", 4, crafting_category="rock")
        raw = Item("T4_ROCK", "Travertine", 4)
        refining_recipe = Recipe(
            refined,
            1,
            (MaterialRequirement(raw.item_id, 2, True),),
            item_value=8,
            base_focus_cost=48,
            provenance=Provenance.STATIC_GAME_DATA,
            source_version="ui-static",
        )
        items.extend(
            (
                CatalogItem(refined, 8, True, Provenance.STATIC_GAME_DATA, "ui-static"),
                CatalogItem(raw, 4, False, Provenance.STATIC_GAME_DATA, "ui-static"),
            )
        )
        recipes.append(refining_recipe)
        prices.extend((_price(raw.item_id, 50), _price(refined.item_id, 500)))
    catalog.replace_all(
        tuple(items),
        tuple(recipes),
        CatalogImport(
            "fixture",
            "memory://ui",
            "ui-static",
            NOW,
            NOW,
            len(items),
            len(recipes),
        ),
    )
    market.upsert_many(tuple(prices))
    if with_fee:
        fees.set(
            StationFeeObservation(
                Region.AMERICAS.value,
                "Bridgewatch",
                StationType.WARRIORS_FORGE,
                500,
                fee_observed_at,
            )
        )
        if include_refining:
            fees.set(
                StationFeeObservation(
                    Region.AMERICAS.value,
                    "Bridgewatch",
                    StationType.STONEMASON,
                    500,
                    fee_observed_at,
                )
            )
    profiles.save(
        CraftingSkillProfile(
            available_focus=10_000,
            assume_zero_for_unspecified=True,
        )
    )
    planner = FindMoneyPreflightPlanner(catalog, market, overrides, fees, profiles)
    snapshot_ids = iter(range(1, 1_000))
    service = FindMoneyService(
        planner,
        market,
        overrides,
        profiles,
        history,
        snapshots=snapshots,
        clock=lambda: NOW,
        identifier_factory=lambda _created: f"ui-plan-{next(snapshot_ids)}",
    )
    constraints = FindMoneyConstraints(
        available_silver=1_000_000,
        available_focus=10_000,
        material_cities=("Bridgewatch",),
        craft_cities=("Bridgewatch",),
        sell_cities=("Bridgewatch",),
        history_enabled=False,
    )
    return service, snapshots, preferences, constraints


def _price(item_id: str, sell_price: int, *, city: str = "Bridgewatch") -> MarketPrice:
    return MarketPrice(
        item_id,
        city,
        1,
        Region.AMERICAS,
        sell_price,
        NOW,
        sell_price - 1,
        NOW,
        NOW,
        Provenance.AODP_CACHED,
    )


def _wait_until(app: QApplication, predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


def test_page_is_idle_until_two_explicit_stages_and_renders_plan_and_history(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    assert recording.preflight_calls == 0
    assert recording.execute_calls == 0
    assert view.preflight is None

    view.prepare_preflight()
    assert recording.preflight_calls == 1
    assert recording.execute_calls == 0
    assert view.run_button.isEnabled()
    assert view.preflight_counts.rowCount() > 0

    view.start_plan()
    _wait_until(qt_app, lambda: view._thread is None)

    assert recording.execute_calls == 1
    assert recording.last_execute_kwargs is not None
    assert recording.last_execute_kwargs["optimizer_limits"] == SIMPLE_MODE_OPTIMIZER_LIMITS
    assert view.displayed_snapshot is not None
    assert view.action_table.rowCount() > 0
    assert "PLAN STATUS" in view.plan_banner.text()
    detail = view.action_detail.toPlainText()
    assert "gross required" in detail
    assert "Transaction tax deducted on sale" in detail
    assert "Non-Focus production bonus" in detail
    assert "Production-city returned-material market value (informational only)" in detail
    assert "ASSUMPTIONS" in view.plan_explanation.toPlainText()
    assert "WHY RESOURCES REMAIN" in view.plan_explanation.toPlainText()
    assert snapshots.count() == 1

    view.tabs.setCurrentIndex(3)
    view.recent_table.selectRow(0)
    view.open_selected_snapshot()
    assert "HISTORICAL SNAPSHOT" in view.plan_banner.text()
    assert "prices have NOT been refreshed" in view.plan_banner.text()
    assert view.replan_button.isEnabled()
    view.close()


def test_find_money_page_scrolls_instead_of_compressing_results(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    view.resize(1_050, 600)
    view.show()
    qt_app.processEvents()

    vertical_scrollbar = view.scroll_area.verticalScrollBar()
    horizontal_scrollbar = view.scroll_area.horizontalScrollBar()
    assert view.scroll_area.widgetResizable()
    assert view.scroll_content.minimumWidth() == view.WORKSPACE_MIN_WIDTH
    assert view.tabs.minimumHeight() == 1_000
    assert view.action_splitter.minimumHeight() == 560
    assert not view.action_splitter.childrenCollapsible()
    assert view.action_table.minimumHeight() == 300
    assert view.action_detail.minimumHeight() == 220
    assert vertical_scrollbar.maximum() > 0
    assert horizontal_scrollbar.maximum() > 0
    assert horizontal_scrollbar.isVisible()
    # A live compositor can constrain this top-level test window over several geometry events.
    # Follow the changing maxima until that platform-driven resize has settled.
    for _ in range(10):
        vertical_scrollbar.setValue(vertical_scrollbar.maximum())
        horizontal_scrollbar.setValue(horizontal_scrollbar.maximum())
        qt_app.processEvents()
    assert vertical_scrollbar.value() == vertical_scrollbar.maximum()
    assert horizontal_scrollbar.value() == horizontal_scrollbar.maximum()

    view.close()


def test_missing_station_fee_preflight_is_structured_and_blocks_run(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path, with_fee=False)
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    view.prepare_preflight()

    assert recording.execute_calls == 0
    assert view.station_requirements.rowCount() == 1
    states = [
        view.station_requirements.item(row, 6).text()
        for row in range(view.station_requirements.rowCount())
    ]
    assert states == ["Needs update in Settings"]
    assert not view.run_button.isEnabled()
    assert "station fees need attention" in view.status.text()
    view.close()


def test_stale_station_fee_preflight_is_visible_before_refresh(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(
        tmp_path,
        fee_observed_at=NOW - timedelta(days=3),
    )
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    view.prepare_preflight()

    assert recording.execute_calls == 0
    assert view.station_requirements.item(0, 4).text().casefold() == "stale"
    assert view.station_requirements.item(0, 6).text() == "Needs update in Settings"
    assert not view.run_button.isEnabled()
    view.close()


def test_large_workload_warning_is_visible_but_does_not_silently_clamp(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    view.per_item_cap.setValue(10_000)

    view.prepare_preflight()

    assert view.preflight is not None
    assert view.preflight.constraints.per_item_craft_cap == 10_000
    assert view.preflight.workload.likely_approximate
    blocker_messages = [
        view.preflight_blockers.item(row, 2).text()
        for row in range(view.preflight_blockers.rowCount())
    ]
    assert any("very large optimization search" in value for value in blocker_messages)
    assert "may make the result Approximate" in view.status.text()
    assert view.run_button.isEnabled()
    view.close()


def test_cancellation_returns_page_to_usable_state(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    cancellable = CancellableService(service)
    view = FindMoneyView(
        cancellable,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    view.prepare_preflight()
    view.start_plan()
    _wait_until(qt_app, cancellable.started.is_set)
    assert not view.action_inputs.isEnabled()
    assert not view.core_inputs.isEnabled()
    assert not view.advanced_toggle.isEnabled()
    assert not view.cancel_button.isHidden()
    assert view.cancel_button.isEnabled()

    view.cancel_plan()
    _wait_until(qt_app, lambda: view._thread is None)

    assert cancellable.execute_calls == 1
    assert view.run_result is not None and view.run_result.cancelled
    assert view.action_inputs.isEnabled()
    assert view.core_inputs.isEnabled()
    assert view.advanced_toggle.isEnabled()
    assert view.preflight_button.isEnabled()
    assert not view.cancel_button.isEnabled()
    assert view.cancel_button.isHidden()
    assert "cancelled safely" in view.status.text()
    view.close()


def test_simple_mode_shows_exact_network_batch_progress(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    worker = object()
    view._worker = worker  # type: ignore[assignment]
    progress = PlanningProgress(
        PlanningStage.CURRENT_REFRESH,
        "Current-price batches: 7 of 80 complete · 2 failed; saved prices retained.",
        7,
        80,
    )

    view._worker_progress(worker, progress)  # type: ignore[arg-type]

    assert view.stage_label.text() == progress.message
    assert view.status.text() == progress.message
    assert view.progress.value() > 0
    view._worker = None
    view.close()


def test_worker_failure_keeps_completed_progress_visible(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    worker = object()
    view._worker = worker  # type: ignore[assignment]
    view.progress.setValue(18)

    view._worker_failed(worker, "ValueError: simulated failure")  # type: ignore[arg-type]

    assert view.progress.value() == 18
    assert view.progress.format() == "Stopped after 18%"
    assert view.stage_label.text() == "Stopped after partial progress"
    assert "Completed market-data writes were kept" in view.status.text()

    view._reset_stage_progress()
    assert view.progress.value() == 0
    assert view.progress.format() == "%p%"
    view._worker = None
    view.close()


def test_unified_controls_mixed_counts_refining_summary_evidence_and_exports(
    qt_app,
    tmp_path,
    monkeypatch,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path, include_refining=True)
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    assert view.craft_actions.isChecked()
    assert view.refine_actions.isChecked()
    assert all(widget.isChecked() for widget in view.refining_family_checks.values())
    view.craft_actions.setChecked(False)
    view.refine_actions.setChecked(False)
    view.prepare_preflight()
    assert recording.preflight_calls == 0
    assert "at least one action kind" in view.status.text().casefold()

    view.craft_actions.setChecked(True)
    view.refine_actions.setChecked(True)
    view.prepare_preflight()
    assert view.preflight is not None
    assert view.preflight.summary.crafting_recipes == 1
    assert view.preflight.summary.refining_recipes == 1
    view.start_plan()
    _wait_until(qt_app, lambda: view._thread is None)

    action_rows = {
        view.action_table.item(row, 0).text(): row for row in range(view.action_table.rowCount())
    }
    assert set(action_rows) == {"Craft", "Refine"}
    view.action_table.selectRow(action_rows["Refine"])
    qt_app.processEvents()
    detail = view.action_detail.toPlainText()
    assert detail.startswith("REFINE — Travertine Block")
    assert "production group=rock" in detail
    assert "Stonemason" in detail
    assert "albion-city-refining-bonuses-2026-08-v1" in detail
    assert "refine/rock/t4" in detail
    assert "1 crafting · 1 refining" in view.plan_totals.text()

    json_path = tmp_path / "mixed-plan.json"
    csv_path = tmp_path / "mixed-plan.csv"
    destinations = iter(((str(json_path), "JSON"), (str(csv_path), "CSV")))
    monkeypatch.setattr(
        "albion_crafter.ui.find_money.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: next(destinations),
    )
    view.export_displayed_json()
    view.export_displayed_csv()
    assert '"action_kind": "refine"' in json_path.read_text()
    assert "action_kind" in csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
    view.close()


def test_opening_legacy_snapshot_labels_it_historical_without_rewriting_it(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    result = service.execute(
        service.preflight(constraints, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )
    assert result.snapshot is not None
    legacy = replace(
        result.snapshot,
        snapshot_id="legacy-ui-plan",
        snapshot_format_version=1,
    )
    snapshots.save(legacy)
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    legacy_row = next(
        row
        for row in range(view.recent_table.rowCount())
        if view.recent_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == "legacy-ui-plan"
    )
    view.recent_table.selectRow(legacy_row)
    view.open_selected_snapshot()

    assert "HISTORICAL SNAPSHOT" in view.plan_banner.text()
    assert view.displayed_snapshot is not None
    assert view.displayed_snapshot.snapshot_format_version == 1
    assert all(action.action_kind.value == "craft" for action in view.displayed_snapshot.actions)
    with snapshots.database.connection() as connection:
        stored_version = connection.execute(
            "SELECT snapshot_format_version FROM plan_snapshots WHERE snapshot_id=?",
            ("legacy-ui-plan",),
        ).fetchone()[0]
    assert stored_version == 1
    view.close()


def test_arbitrage_controls_preflight_row_and_execution_detail_are_action_aware(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(
        tmp_path,
        with_fee=False,
        include_arbitrage=True,
    )
    constraints = replace(
        constraints,
        action_kinds=frozenset({ActionKind.ARBITRAGE}),
        transport_policy=TransportPolicy.EXPLICIT_COST,
        transport_cost_per_craft=5,
        arbitrage_scope=ArbitrageScope.CRAFTED_OUTPUTS,
        arbitrage_source_cities=("Bridgewatch",),
        arbitrage_destination_cities=("Thetford",),
        per_item_craft_cap=2,
    )
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    assert recording.preflight_calls == 0
    assert view.arbitrage_actions.isChecked()
    assert not view.craft_actions.isChecked()
    assert not view.refine_actions.isChecked()
    assert view.arbitrage_scope.currentData() == ArbitrageScope.CRAFTED_OUTPUTS.value
    assert view.arbitrage_source_cities.text() == "Bridgewatch"
    assert view.arbitrage_destination_cities.text() == "Thetford"

    view.prepare_preflight()
    assert view.preflight is not None
    assert view.preflight.summary.arbitrage_items == 1
    assert view.preflight.summary.arbitrage_routes == 1
    assert view.station_requirements.rowCount() == 0
    assert view.run_button.isEnabled()
    view.start_plan()
    _wait_until(qt_app, lambda: view._thread is None)

    assert view.action_table.rowCount() == 1
    assert view.action_table.item(0, 0).text() == "Arbitrage"
    assert view.action_table.item(0, 6).text() == "N/A"
    assert view.action_table.item(0, 8).text() == "N/A"
    detail = view.action_detail.toPlainText()
    assert detail.startswith("ARBITRAGE — Broadsword")
    assert "Bridgewatch → Thetford" in detail
    assert "minimum sell orders" in detail
    assert "Focus: N/A · station fee: N/A" in detail
    assert "top-of-book snapshot, not order depth" in detail
    assert "SHARED SOURCE + DESTINATION CAPACITY" in detail
    assert "Source acquisition liquidity:" in detail
    assert "Destination liquidation liquidity:" in detail
    assert "Expected unit purchase from minimum sell orders:" in detail
    assert "Expected unit sale:" in detail
    assert "0 crafting · 0 refining · 1 arbitrage" in view.plan_totals.text()
    view.close()


def test_simple_mode_one_click_searches_crafting_refining_and_arbitrage(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(
        tmp_path,
        include_refining=True,
        include_arbitrage=True,
    )
    constraints = replace(
        constraints,
        action_kinds=frozenset(ActionKind),
        transport_policy=TransportPolicy.ACKNOWLEDGED_UNCOSTED,
        arbitrage_source_cities=("Bridgewatch",),
        arbitrage_destination_cities=("Thetford",),
    )
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    assert not view.advanced_toggle.isChecked()
    assert view.action_inputs.isHidden()
    assert not view.simple_run_button.isHidden()
    assert all(
        checkbox.isChecked()
        for checkbox in (view.craft_actions, view.refine_actions, view.arbitrage_actions)
    )

    view.find_money()
    _wait_until(qt_app, lambda: view._thread is None)

    assert recording.preflight_calls == 1
    assert recording.execute_calls == 1
    assert view.preflight is not None
    assert view.preflight.summary.crafting_recipes == 1
    assert view.preflight.summary.refining_recipes == 1
    assert view.preflight.summary.arbitrage_routes > 0
    assert view.action_table.rowCount() > 0
    assert view.plan_banner.text().startswith("BEST PLAN")
    assert "SEARCH CHECKED" in view.simple_result_summary.text()
    assert "Matching recipes      2" in view.simple_result_summary.text()
    assert "Fully priced routes" in view.simple_result_summary.text()
    assert "Profitable routes" in view.simple_result_summary.text()
    view.close()


def test_simple_mode_one_click_refreshes_stale_prices_only_after_press(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    stale = (NOW - timedelta(hours=8)).isoformat()
    with service.market_prices.database.connection() as connection:
        connection.execute(
            "UPDATE market_prices SET sell_price_timestamp=?, buy_price_timestamp=?",
            (stale, stale),
        )
    requests: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        requests.append(url)
        parsed = urlparse(url)
        item_ids = parsed.path.partition("/prices/")[2].removesuffix(".json").split(",")
        query = parse_qs(parsed.query)
        city = query["locations"][0]
        quality = int(query["qualities"][0])
        return json.dumps(
            [
                {
                    "item_id": item_id,
                    "city": city,
                    "quality": quality,
                    "sell_price_min": 2_500 if item_id == "T4_MAIN_SWORD" else 100,
                    "sell_price_min_date": NOW.isoformat(),
                    "buy_price_max": 2_400 if item_id == "T4_MAIN_SWORD" else 90,
                    "buy_price_max_date": NOW.isoformat(),
                }
                for item_id in item_ids
            ]
        ).encode()

    service.current_refresh = CurrentMarketRefreshExecutor(
        service.market_prices,
        client_factory=lambda region: AODPClient(
            region,
            transport=transport,
            wall_clock=lambda: NOW,
        ),
    )
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    assert requests == []
    view.find_money()
    _wait_until(qt_app, lambda: view._thread is None)

    assert len(requests) == 1
    assert view.run_result is not None
    assert view.run_result.current_refresh is not None
    assert view.run_result.current_refresh.keys_requested == 2
    assert view.run_result.current_refresh.batches_failed == 0
    assert view.plan_banner.text().startswith("BEST PLAN")
    view.close()


def test_full_sync_populates_blank_search_and_eliminates_targeted_refresh_keys(
    tmp_path,
) -> None:
    service, _snapshots, _preferences, constraints = _stack(tmp_path)
    baseline_preflight = service.preflight(constraints, as_of=NOW)
    baseline_result = service.execute(
        baseline_preflight,
        refresh_current=False,
        refresh_history=False,
    )
    assert baseline_preflight.constraints.item_query == ""
    assert baseline_preflight.market_refresh.refresh_keys == ()
    assert baseline_result.snapshot is not None

    with service.market_prices.database.connection() as connection:
        connection.execute("DELETE FROM market_prices")
    empty_preflight = service.preflight(constraints, as_of=NOW)
    assert len(empty_preflight.market_refresh.refresh_keys) == 2

    def transport(url: str, _timeout: float) -> bytes:
        parsed = urlparse(url)
        item_ids = parsed.path.partition("/prices/")[2].removesuffix(".json").split(",")
        cities = parse_qs(parsed.query)["locations"][0].split(",")
        return json.dumps(
            [
                {
                    "item_id": item_id,
                    "city": city,
                    "quality": 1,
                    "sell_price_min": 2_500 if item_id == "T4_MAIN_SWORD" else 100,
                    "sell_price_min_date": NOW.isoformat(),
                    "buy_price_max": 2_499 if item_id == "T4_MAIN_SWORD" else 99,
                    "buy_price_max_date": NOW.isoformat(),
                }
                for item_id in item_ids
                for city in cities
            ]
        ).encode()

    def client_factory(region: Region, **kwargs) -> AODPClient:
        return AODPClient(
            region,
            **kwargs,
            transport=transport,
            wall_clock=lambda: NOW,
        )

    database = service.market_prices.database
    sync_result = RoyalMarketSyncService(
        RoyalMarketUniverseService(CatalogRepository(database)),
        service.market_prices,
        client_factory=client_factory,
        wall_clock=lambda: NOW,
    ).synchronize(Region.AMERICAS, ("Bridgewatch",))
    populated_preflight = service.preflight(constraints, as_of=NOW)
    populated_result = service.execute(
        populated_preflight,
        refresh_current=True,
        refresh_history=False,
    )

    assert sync_result.status == "complete"
    assert sync_result.item_count == 2
    assert populated_preflight.market_refresh.refresh_keys == ()
    assert populated_result.current_refresh is None
    assert populated_result.snapshot is not None
    assert (
        populated_result.snapshot.total_expected_profit
        == baseline_result.snapshot.total_expected_profit
    )


def test_simple_mode_collects_station_fee_inline_then_continues(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path, with_fee=False)
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    view.find_money()

    assert recording.preflight_calls == 1
    assert recording.execute_calls == 0
    assert view.plan_banner.text() == "SETUP REQUIRED"
    assert not view.station_setup.isHidden()
    assert view.station_setup_table.rowCount() == 1

    view.station_setup_table.item(0, 3).setText("500")
    view.save_station_fees_and_continue()
    _wait_until(qt_app, lambda: view._thread is None)

    assert recording.preflight_calls == 2
    assert recording.execute_calls == 1
    assert view.plan_banner.text().startswith("BEST PLAN")
    assert view.action_table.rowCount() > 0
    view.close()


def test_simple_mode_distinguishes_missing_prices_and_accepts_inline_overrides(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    with service.market_prices.database.connection() as connection:
        connection.execute("DELETE FROM market_prices")
    recording = RecordingService(service)
    view = FindMoneyView(
        recording,  # type: ignore[arg-type]
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    view.find_money()
    _wait_until(qt_app, lambda: view._thread is None)

    assert view.plan_banner.text() == "NOT ENOUGH DATA TO KNOW"
    assert not view.price_setup.isHidden()
    assert view.price_setup_table.rowCount() == 2
    assert all(
        view.price_setup_table.item(row, 4).text() == "Missing"
        and view.price_setup_table.item(row, 5).text() == ""
        for row in range(view.price_setup_table.rowCount())
    )
    for row in range(view.price_setup_table.rowCount()):
        item_id = view.price_setup_table.item(row, 0).text()
        value = "2500" if item_id == "T4_MAIN_SWORD" else "100"
        view.price_setup_table.item(row, 5).setText(value)

    view.save_price_overrides_and_continue()
    _wait_until(qt_app, lambda: view._thread is None)

    assert recording.preflight_calls == 2
    assert recording.execute_calls == 2
    assert view.plan_banner.text().startswith("BEST PLAN")
    assert view.action_table.rowCount() > 0
    view.close()


def _missing_assessments(count: int) -> tuple[PriceRequirementAssessment, ...]:
    return tuple(
        PriceRequirementAssessment(
            PriceRequirement(
                MarketKey(Region.AMERICAS, f"ITEM_{index:03d}", "Bridgewatch", 1),
                MarketSide.SELL_ORDER,
                PriceRole.MATERIAL,
            ),
            ObservationDisposition.MISSING,
            None,
            None,
            Provenance.UNKNOWN,
            Freshness.UNKNOWN,
            True,
        )
        for index in range(count)
    )


def test_many_unresolved_prices_recommend_broad_sync_before_manual_form(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    view = FindMoneyView(service, snapshots, preferences, default_constraints=constraints)
    requested = []
    view.market_sync_requested.connect(lambda: requested.append(True))

    view._render_price_setup(_missing_assessments(MAX_INLINE_PRICE_OVERRIDES + 1))

    assert view.price_setup.title() == "MARKET COVERAGE TOO LOW"
    assert view.price_setup_table.isHidden()
    assert view.price_setup_save_button.isHidden()
    assert not view.price_setup_manual_button.isHidden()
    assert not view.price_setup_fast_preset.isHidden()
    assert "11 required prices" in view.price_setup_note.text()
    view.price_setup_refresh_markets.click()
    assert requested == [True]

    view.price_setup_manual_button.click()
    assert not view.price_setup_table.isHidden()
    assert view.price_setup_table.rowCount() == 11
    assert "USER_OVERRIDE" in view.price_setup_status.text()

    view.find_money = lambda: None  # type: ignore[method-assign]
    view._use_fast_price_setup_preset()
    assert view.trust_preset.currentData() == TrustPreset.FAST.value
    view.close()


def test_small_unresolved_price_count_keeps_inline_entry_available(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    view = FindMoneyView(service, snapshots, preferences, default_constraints=constraints)

    view._render_price_setup(_missing_assessments(2))

    assert view.price_setup.title() == "MARKET DATA UNAVAILABLE"
    assert not view.price_setup_table.isHidden()
    assert view.price_setup_table.rowCount() == 2
    assert not view.price_setup_save_button.isHidden()
    assert view.price_setup_manual_button.isHidden()
    view.close()


def test_simple_mode_reports_no_profit_only_after_complete_pricing(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    service.market_prices.upsert_many((_price("T4_MAIN_SWORD", 100),))
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    view.find_money()
    _wait_until(qt_app, lambda: view._thread is None)

    assert view.plan_banner.text() == "NO PROFIT FOUND"
    assert "FULLY-PRICED OPPORTUNITIES" in view.simple_result_summary.text()
    assert view.price_setup.isHidden()
    view.close()


def test_simple_mode_applies_visible_careful_preset_to_saved_advanced_settings(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    saved = replace(
        constraints,
        history_enabled=True,
        minimum_liquidity=MinimumLiquidity.HIGH,
        allow_stale_station_fees=True,
    )
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=saved,
    )

    assert view.trust_preset.currentData() == TrustPreset.CAREFUL.value
    assert view.minimum_liquidity.currentData() == MinimumLiquidity.HIGH.value

    view.find_money()
    assert view.preflight is not None
    assert view.preflight.constraints.minimum_liquidity is MinimumLiquidity.ANY
    assert not view.preflight.constraints.allow_stale_station_fees
    _wait_until(qt_app, lambda: view._thread is None)
    view.close()


def test_fully_priced_routes_are_not_invalidated_by_other_missing_prices(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    service.market_prices.upsert_many((_price("T4_MAIN_SWORD", 100),))
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    view._unresolved_price_requirements = lambda _preflight: _missing_assessments(1)  # type: ignore[method-assign]

    view.find_money()
    _wait_until(qt_app, lambda: view._thread is None)

    assert view.plan_banner.text() == "NO PROFIT FOUND"
    assert "did not invalidate this completed search" in view.simple_result_summary.text()
    assert view.plan_banner.property("freshness") == "aging"
    assert view.status.property("freshness") == "aging"
    assert view.price_setup.isHidden()
    view.close()


def test_positive_fully_priced_routes_filtered_by_settings_are_not_data_errors(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    strict = replace(constraints, minimum_liquidity=MinimumLiquidity.HIGH)
    result = service.execute(
        service.preflight(strict, as_of=NOW),
        refresh_current=False,
        refresh_history=False,
    )
    assert result.snapshot is not None and not result.snapshot.actions
    assert result.initial_evaluation is not None and result.initial_evaluation.candidates
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )

    view._render_no_result(result, unresolved=True)

    assert view.plan_banner.text() == "NO PLAN MATCHED YOUR SETTINGS"
    assert "did not invalidate this completed search" in view.simple_result_summary.text()
    assert view.plan_banner.property("freshness") == "aging"
    assert view.status.property("freshness") == "aging"
    view.close()


def test_simple_mode_disables_focus_without_profile_but_still_plans(qt_app, tmp_path) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    service.crafting_profiles.remove()
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=replace(constraints, use_focus=True),
    )

    assert not view.use_focus.isEnabled()
    assert not view.use_focus.isChecked()
    assert not view.focus_setup_button.isHidden()

    view.find_money()
    _wait_until(qt_app, lambda: view._thread is None)

    assert view.plan_banner.text().startswith("BEST PLAN")
    assert view.displayed_snapshot is not None
    assert view.displayed_snapshot.total_focus == 0
    view.close()


def test_simple_counts_follow_item_filter_and_advanced_mode_preserves_controls(
    qt_app,
    tmp_path,
) -> None:
    service, snapshots, preferences, constraints = _stack(tmp_path)
    view = FindMoneyView(
        service,
        snapshots,
        preferences,
        default_constraints=constraints,
    )
    before = view.constraints()

    view.advanced_toggle.setChecked(True)
    qt_app.processEvents()
    assert not view.action_inputs.isHidden()
    assert not view.advanced.isHidden()
    assert not view.advanced_run_controls.isHidden()
    assert view.constraints() == before

    view.advanced_toggle.setChecked(False)
    view.item_query.setText("No Such Item")
    assert view.prepare_preflight()
    assert view.preflight is not None
    assert view.preflight.summary.supported_catalog_recipes == 1
    assert view.preflight.summary.matched_recipes == 0
    assert "Matched 'No Such Item': 0" in view.simple_result_summary.text()
    view.close()
