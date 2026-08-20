from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.database.catalog import CatalogImport, CatalogItem, CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
)
from albion_crafter.database.v3 import StationFeeRepository
from albion_crafter.market.models import MarketPrice, MarketSide, Region
from albion_crafter.market.pricing import PriceResolver
from albion_crafter.market.recipe_refresh import RecipePriceAvailabilityStatus
from albion_crafter.ui.calculator_view import CalculatorView


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class RecordingResolver:
    def __init__(self, resolver: PriceResolver) -> None:
        self.resolver = resolver
        self.repository = resolver.repository
        self.calls = 0

    def resolve(self, *args, **kwargs):
        self.calls += 1
        return self.resolver.resolve(*args, **kwargs)


class RecordingRefreshService:
    def __init__(self, result=None, *, error: str | None = None) -> None:
        self.result = result or _refresh_result()
        self.error = error
        self.calls = []

    def refresh(self, request, *, is_cancelled=None, on_progress=None):
        self.calls.append(request)
        if self.error is not None:
            raise RuntimeError(self.error)
        if on_progress is not None:
            on_progress(
                SimpleNamespace(
                    batches_completed=1,
                    batches_planned=2,
                    records_loaded=1,
                )
            )
        return self.result


class BlockingRefreshService:
    def __init__(self) -> None:
        self.calls = []
        self.started = Event()
        self.release = Event()
        self.cancelled = Event()

    def refresh(self, request, *, is_cancelled=None, on_progress=None):
        self.calls.append(request)
        self.started.set()
        while not self.release.wait(0.005):
            if is_cancelled is not None and is_cancelled():
                self.cancelled.set()
                return _refresh_result(complete=False, cancelled=True)
        if is_cancelled is not None and is_cancelled():
            self.cancelled.set()
            return _refresh_result(complete=False, cancelled=True)
        return _refresh_result()


class PersistThenFailRefreshService:
    def __init__(self, repository: MarketPriceRepository) -> None:
        self.repository = repository

    def refresh(self, request, *, is_cancelled=None, on_progress=None):
        self.repository.upsert_many(
            (
                _price(
                    request.recipe.output.item_id,
                    request.output_quality,
                    9_999,
                    datetime.now(UTC),
                ),
            )
        )
        raise RuntimeError("later batch failed")


@dataclass
class CalculatorStack:
    resolver: RecordingResolver
    settings: SettingsRepository
    catalog: CatalogRepository
    fees: StationFeeRepository
    recipe: Recipe

    def view(self, refresh_service: RecordingRefreshService) -> CalculatorView:
        return CalculatorView(
            self.resolver,  # type: ignore[arg-type]
            self.settings,
            self.catalog,
            self.fees,
            refresh_service=refresh_service,  # type: ignore[arg-type]
        )


def _refresh_result(
    *,
    complete: bool = True,
    partial: bool = False,
    batches_succeeded: int = 2,
    batches_failed: int = 0,
    available: int = 2,
    missing: int = 0,
    cancelled: bool = False,
):
    statuses = [RecipePriceAvailabilityStatus.UPDATED]
    statuses.extend(RecipePriceAvailabilityStatus.RETAINED for _ in range(max(available - 1, 0)))
    statuses.extend(RecipePriceAvailabilityStatus.MISSING for _ in range(missing))
    return SimpleNamespace(
        availability=tuple(SimpleNamespace(status=value) for value in statuses),
        selected_sides_available=available,
        requirements_requested=available + missing,
        selected_sides_missing=missing,
        batches_succeeded=batches_succeeded,
        batches_failed=batches_failed,
        record_failures=0,
        cancelled=cancelled,
        is_complete=complete,
        is_partial=partial,
    )


def _wait_until(app: QApplication, predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


def _stack(tmp_path, *, with_fee: bool = True) -> CalculatorStack:
    database = Database(tmp_path / "calculator-ui.db")
    database.initialize()
    catalog = CatalogRepository(database)
    market = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    settings = SettingsRepository(database)
    fees = StationFeeRepository(database)
    settings.set_many(
        {
            "region": Region.AMERICAS.value,
            "default_material_buy_city": "Bridgewatch",
            "default_craft_city": "Bridgewatch",
            "default_sell_city": "Bridgewatch",
            "max_market_age_hours": 4,
            "max_station_fee_age_hours": 24,
            "focus_enabled": False,
            "premium": True,
            "available_focus": 10_000,
        }
    )
    output = Item(
        "T4_MAIN_SWORD",
        "Broadsword",
        4,
        crafting_category="sword",
        max_quality=5,
    )
    material = Item("T4_METALBAR", "Steel Bar", 4)
    recipe = Recipe(
        output,
        2,
        (MaterialRequirement(material.item_id, 4, True),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="calculator-ui-static",
    )
    now = datetime.now(UTC)
    catalog.replace_all(
        [
            CatalogItem(output, 100, True, Provenance.STATIC_GAME_DATA, "calculator-ui-static"),
            CatalogItem(material, 10, False, Provenance.STATIC_GAME_DATA, "calculator-ui-static"),
        ],
        [recipe],
        CatalogImport(
            "calculator-ui",
            "memory://calculator-ui",
            "calculator-ui-static",
            now,
            now,
            2,
            1,
        ),
    )
    market.upsert_many(
        (
            _price(material.item_id, 1, 100, now),
            _price(output.item_id, 1, 2_500, now),
            _price(output.item_id, 2, 2_750, now),
        )
    )
    if with_fee:
        fees.set(
            StationFeeObservation(
                Region.AMERICAS.value,
                "Bridgewatch",
                StationType.WARRIORS_FORGE,
                500,
                now,
            )
        )
    return CalculatorStack(
        RecordingResolver(PriceResolver(market, overrides)),
        settings,
        catalog,
        fees,
        recipe,
    )


def _price(item_id: str, quality: int, sell_price: int, observed_at: datetime) -> MarketPrice:
    return MarketPrice(
        item_id,
        "Bridgewatch",
        quality,
        Region.AMERICAS,
        sell_price,
        observed_at,
        sell_price - 25,
        observed_at,
        observed_at,
        Provenance.AODP_CACHED,
    )


def test_default_is_four_answer_summary_with_total_quantities_and_collapsed_evidence(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    refresh = RecordingRefreshService()
    view = stack.view(refresh)

    assert view.details.isHidden()
    assert view.summary_labels["purchase"].text() == "400 silver"
    assert view.summary_labels["crafting_fee"].text().endswith(" silver")
    assert view.summary_labels["sale_proceeds"].text() == "4,675 silver"
    assert view.summary_labels["final_profit"].text().endswith(" silver")
    assert "#54d68c" in view.summary_labels["final_profit"].styleSheet()
    assert view.output_count_label.text() == "1 batch produces 2 items."
    assert "4 T4_METALBAR" in view.materials_needed_label.text()
    assert "returns do not reduce" in view.materials_needed_label.text()
    assert view.material_table.horizontalHeaderItem(1).text() == (
        "Total needed for selected batches"
    )
    assert view.material_table.item(0, 1).text() == "4"
    assert "Focus is off" in view.focus_profile_hint.text()
    assert refresh.calls == []

    view.quality.setValue(2)
    assert view.summary_labels["final_profit"].text().endswith(" silver")
    assert "#ffb454" in view.summary_labels["final_profit"].styleSheet()
    assert "Estimate only" in view.summary_hints["final_profit"].text()
    view.close()


def test_all_economic_controls_recalculate_only_from_cache_and_never_autorefresh(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    refresh = RecordingRefreshService()
    view = stack.view(refresh)
    prior = stack.resolver.calls

    view.quantity.setValue(3)
    assert stack.resolver.calls > prior
    prior = stack.resolver.calls
    assert view.output_count_label.text() == "3 batches produce 6 items."
    assert "12 T4_METALBAR" in view.materials_needed_label.text()
    assert view.material_table.item(0, 1).text() == "12"

    for control, value in (
        (view.material_buy_city, "Martlock"),
        (view.craft_city, "Martlock"),
        (view.sell_city, "Martlock"),
    ):
        control.setCurrentText(value)
        assert stack.resolver.calls > prior
        prior = stack.resolver.calls
    view.sale_method.setCurrentIndex(1)
    assert stack.resolver.calls > prior
    prior = stack.resolver.calls
    view.focus.setChecked(True)
    assert stack.resolver.calls > prior
    assert "profile is incomplete" in view.focus_profile_hint.text()
    prior = stack.resolver.calls
    view.premium.setChecked(False)
    assert stack.resolver.calls > prior
    prior = stack.resolver.calls
    view.quality.setValue(2)
    assert stack.resolver.calls > prior

    view._search_catalog()
    assert refresh.calls == []
    view.close()


def test_missing_station_fee_keeps_partial_totals_and_explicit_zero_can_be_saved(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path, with_fee=False)
    view = stack.view(RecordingRefreshService())
    saved = []
    view.station_fee_saved.connect(lambda: saved.append(True))

    assert view.summary_labels["purchase"].text() == "400 silver"
    assert view.summary_labels["crafting_fee"].text() == "Needs station fee"
    assert view.summary_labels["sale_proceeds"].text() == "4,675 silver"
    assert view.summary_labels["final_profit"].text() == "Add station fee to finish"
    assert not view.station_fee_prompt.isHidden()
    assert "will not assume 0" in view.station_fee_prompt_text.text()
    assert (
        stack.fees.get(
            Region.AMERICAS,
            "Bridgewatch",
            StationType.WARRIORS_FORGE,
        )
        is None
    )

    view.station_fee_input.setValue(0)
    view.save_station_fee()

    observation = stack.fees.get(
        Region.AMERICAS,
        "Bridgewatch",
        StationType.WARRIORS_FORGE,
    )
    assert observation is not None
    assert observation.displayed_fee == 0
    assert saved == [True]
    assert view.summary_labels["crafting_fee"].text() == "0 silver"
    assert view.summary_labels["final_profit"].text().endswith(" silver")
    assert view.station_fee_prompt.isHidden()
    view.close()


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (_refresh_result(), "Refresh complete: 2/2"),
        (
            _refresh_result(
                complete=False,
                partial=True,
                batches_succeeded=1,
                batches_failed=1,
                available=1,
                missing=1,
            ),
            "Refresh partial: 1/2",
        ),
    ),
)
def test_explicit_refresh_uses_sparse_recipe_request_and_reports_result_truthfully(
    qt_app,
    tmp_path,
    result,
    expected,
) -> None:
    stack = _stack(tmp_path)
    refresh = RecordingRefreshService(result)
    view = stack.view(refresh)
    emitted = []
    view.prices_refreshed.connect(lambda: emitted.append(True))

    assert refresh.calls == []
    view.refresh_required_prices()
    _wait_until(qt_app, lambda: not view._workers)

    assert len(refresh.calls) == 1
    request = refresh.calls[0]
    assert request.recipe.output.item_id == stack.recipe.output.item_id
    assert request.region is Region.AMERICAS
    assert request.material_city == "Bridgewatch"
    assert request.sell_city == "Bridgewatch"
    assert request.output_quality == 1
    assert request.material_side is MarketSide.SELL_ORDER
    assert request.output_side is MarketSide.SELL_ORDER
    assert expected in view.refresh_status.text()
    assert "retained" in view.refresh_status.text() or "preserved" in view.refresh_status.text()
    assert emitted == [True]
    view.close()


def test_successful_refresh_explains_when_aodp_has_no_selected_order(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    missing_requirement = SimpleNamespace(
        item_id=stack.recipe.output.item_id,
        side=MarketSide.SELL_ORDER,
        city="Bridgewatch",
    )
    result = _refresh_result(
        complete=False,
        partial=True,
        batches_succeeded=1,
        batches_failed=0,
        available=1,
        missing=1,
    )
    result.availability[-1].requirement = missing_requirement
    refresh = RecordingRefreshService(result)
    view = stack.view(refresh)

    view.refresh_required_prices()
    _wait_until(qt_app, lambda: not view._workers)

    assert "AODP check succeeded" in view.refresh_status.text()
    assert "no usable order was reported" in view.refresh_status.text()
    assert stack.recipe.output.item_id in view.refresh_status.text()
    assert "Refreshing again cannot create a market order" in view.refresh_status.text()
    view.close()


def test_refresh_exception_is_visible_and_does_not_emit_prices_refreshed(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    refresh = RecordingRefreshService(error="offline")
    view = stack.view(refresh)
    emitted = []
    view.prices_refreshed.connect(lambda: emitted.append(True))

    view.refresh_required_prices()
    _wait_until(qt_app, lambda: not view._workers)

    assert "stopped unexpectedly" in view.refresh_status.text().lower()
    assert "Successful earlier batches may already be saved" in view.refresh_status.text()
    assert "all other saved prices were preserved" in view.refresh_status.text()
    assert "offline" in view.refresh_status.text()
    assert emitted == []
    view.close()


def test_unexpected_late_failure_reports_that_earlier_cache_writes_remain(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    service = PersistThenFailRefreshService(stack.resolver.repository)
    view = stack.view(service)  # type: ignore[arg-type]

    view.refresh_required_prices()
    _wait_until(qt_app, lambda: not view._workers)

    persisted = stack.resolver.repository.get(
        stack.recipe.output.item_id,
        "Bridgewatch",
        1,
        Region.AMERICAS,
    )
    assert persisted is not None
    assert persisted.sell_price == 9_999
    assert "Successful earlier batches may already be saved" in view.refresh_status.text()
    assert "later batch failed" in view.refresh_status.text()
    view.close()


def test_refresh_locks_request_controls_until_the_captured_scenario_finishes(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    refresh = BlockingRefreshService()
    view = stack.view(refresh)  # type: ignore[arg-type]

    view.refresh_required_prices()
    _wait_until(qt_app, refresh.started.is_set)

    for control in (
        view.item_search,
        view.item_results,
        view.material_buy_city,
        view.sell_city,
        view.sale_method,
        view.quality,
    ):
        assert not control.isEnabled()

    # A settings/catalog signal can still change a disabled control
    # programmatically. Completion must never claim it refreshed this new key.
    view.sell_city.setCurrentText("Martlock")
    refresh.release.set()
    _wait_until(qt_app, lambda: not view._workers)
    assert view.sell_city.isEnabled()
    assert "previous selection" in view.refresh_status.text()
    assert "refresh again for this selection" in view.refresh_status.text()
    view.close()


def test_closing_calculator_cancels_and_disconnects_an_active_refresh(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    refresh = BlockingRefreshService()
    view = stack.view(refresh)  # type: ignore[arg-type]
    view.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    view.refresh_required_prices()
    _wait_until(qt_app, refresh.started.is_set)
    view.close()

    _wait_until(qt_app, refresh.cancelled.is_set)


def test_refresh_completion_detects_same_output_recipe_replacement(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    refresh = BlockingRefreshService()
    view = stack.view(refresh)  # type: ignore[arg-type]

    view.refresh_required_prices()
    _wait_until(qt_app, refresh.started.is_set)

    replacement_material = Item("T4_LEATHER", "Worked Leather", 4)
    replacement = Recipe(
        stack.recipe.output,
        stack.recipe.output_quantity,
        (MaterialRequirement(replacement_material.item_id, 8, True),),
        item_value=stack.recipe.item_value,
        base_focus_cost=stack.recipe.base_focus_cost,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="calculator-ui-static-v2",
    )
    now = datetime.now(UTC)
    stack.catalog.replace_all(
        [
            CatalogItem(
                stack.recipe.output,
                100,
                True,
                Provenance.STATIC_GAME_DATA,
                "calculator-ui-static-v2",
            ),
            CatalogItem(
                replacement_material,
                10,
                False,
                Provenance.STATIC_GAME_DATA,
                "calculator-ui-static-v2",
            ),
        ],
        [replacement],
        CatalogImport(
            "calculator-ui-v2",
            "memory://calculator-ui-v2",
            "calculator-ui-static-v2",
            now,
            now,
            2,
            1,
        ),
    )
    view.refresh_catalog()
    assert view._current_recipe == replacement

    refresh.release.set()
    _wait_until(qt_app, lambda: not view._workers)

    assert "previous selection" in view.refresh_status.text()
    assert "refresh again for this selection" in view.refresh_status.text()
    assert "Price data needed" in view.data_banner.text()
    view.close()


def test_clean_catalog_cta_is_public_functional_and_does_not_refresh(
    qt_app,
    tmp_path,
) -> None:
    database = Database(tmp_path / "empty-calculator-ui.db")
    database.initialize()
    market = MarketPriceRepository(database)
    refresh = RecordingRefreshService()
    view = CalculatorView(
        PriceResolver(market),
        SettingsRepository(database),
        CatalogRepository(database),
        StationFeeRepository(database),
        refresh_service=refresh,  # type: ignore[arg-type]
    )
    requested = []
    view.open_market_data_requested.connect(lambda: requested.append(True))

    assert not view.clean_catalog_button.isHidden()
    assert view.clean_catalog_button.text() == "Open Market Data to install recipes"
    assert not view.refresh_button.isEnabled()
    assert refresh.calls == []
    view.clean_catalog_button.click()
    assert requested == [True]
    assert refresh.calls == []
    view.close()


def test_unknown_acid_item_value_is_labeled_static_and_not_user_enterable(
    qt_app,
    tmp_path,
) -> None:
    stack = _stack(tmp_path)
    acid = Item(
        "T5_POTION_ACID",
        "Acid Potion",
        5,
        crafting_category="potion",
    )
    rare = Item("T5_ALCHEMY_RARE_DIREBEAR", "Direbear Remains", 5)
    recipe = Recipe(
        acid,
        10,
        (MaterialRequirement(rare.item_id, 1, False),),
        item_value=None,
        base_focus_cost=294,
        provenance=Provenance.STATIC_GAME_DATA,
        source_version="acid-pinned-fixture",
    )
    now = datetime.now(UTC)
    stack.catalog.replace_all(
        [
            CatalogItem(acid, None, True, Provenance.STATIC_GAME_DATA, "acid-pinned-fixture"),
            CatalogItem(rare, None, False, Provenance.STATIC_GAME_DATA, "acid-pinned-fixture"),
        ],
        [recipe],
        CatalogImport(
            "acid-fixture",
            "memory://acid",
            "acid-pinned-fixture",
            now,
            now,
            2,
            1,
        ),
    )
    stack.resolver.repository.upsert_many(
        (
            _price(rare.item_id, 1, 1_000, now),
            _price(acid.item_id, 1, 5_000, now),
        )
    )
    stack.fees.set(
        StationFeeObservation(
            Region.AMERICAS.value,
            "Bridgewatch",
            StationType.ALCHEMIST_LAB,
            500,
            now,
        )
    )
    view = stack.view(RecordingRefreshService())

    assert "Unsupported static recipe" in view.data_banner.text()
    assert "verified static Item Value" in view.whats_missing.text()
    assert "not user-enterable" in view.whats_missing.text()
    assert "Pinned upstream data does not provide" in view.summary_hints["final_profit"].text()
    view.close()
