from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from inspect import signature

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QHeaderView

from albion_crafter.core.provenance import Provenance
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    SettingsRepository,
)
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.market.aodp import BatchFailure
from albion_crafter.market.history import (
    HistoryFetchResult,
    HistoryRecordFailure,
    HistoryTimeScale,
    MarketHistoryInterval,
)
from albion_crafter.market.models import FreshnessPolicy, MarketPrice, Region
from albion_crafter.ui.market_data import (
    HISTORY_RETENTION_DAYS,
    HistoryRefreshWorker,
    MarketDataView,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _view(tmp_path) -> MarketDataView:
    database = Database(tmp_path / "market-data-ui.db")
    database.initialize()
    return MarketDataView(
        MarketPriceRepository(database),
        PriceOverrideRepository(database),
        CatalogRepository(database),
        SettingsRepository(database),
        MarketHistoryRepository(database),
    )


class RecordingHistoryRepository:
    def __init__(self) -> None:
        self.intervals: list[MarketHistoryInterval] = []
        self.coverage = []
        self.prune_cutoff: datetime | None = None

    def upsert_many(self, intervals) -> None:
        self.intervals.extend(intervals)

    def set_coverage(self, coverage) -> None:
        self.coverage.append(coverage)

    def prune_before(self, cutoff: datetime) -> int:
        self.prune_cutoff = cutoff
        return 4


def _result(
    *,
    fetched_at: datetime,
    intervals: tuple[MarketHistoryInterval, ...] = (),
    failures: tuple[BatchFailure, ...] = (),
    record_failures: tuple[HistoryRecordFailure, ...] = (),
) -> HistoryFetchResult:
    return HistoryFetchResult(
        intervals=intervals,
        failures=failures,
        record_failures=record_failures,
        batch_count=1,
        items_requested=3,
        successful_batches=0 if failures else 1,
        elapsed_seconds=0.25,
        start_date=(fetched_at - timedelta(days=7)).date(),
        end_date=fetched_at.date(),
        time_scale=HistoryTimeScale.SIX_HOURLY,
    )


def _interval(item_id: str, city: str, fetched_at: datetime) -> MarketHistoryInterval:
    return MarketHistoryInterval(
        item_id=item_id,
        city=city,
        quality=1,
        region=Region.AMERICAS,
        observed_at=fetched_at - timedelta(hours=6),
        item_count=12,
        average_price=1_000,
        time_scale=HistoryTimeScale.SIX_HOURLY,
        fetched_at=fetched_at,
        provenance=Provenance.AODP_LIVE,
    )


def test_history_coverage_distinguishes_successful_empty_from_failure() -> None:
    fetched_at = datetime(2026, 8, 18, 12, tzinfo=UTC)
    repository = RecordingHistoryRepository()
    worker = HistoryRefreshWorker(
        Region.AMERICAS,
        repository,  # type: ignore[arg-type]
        ("T4_A", "T4_B", "T4_C"),
        ("Bridgewatch", "Martlock"),
        7,
    )
    result = _result(
        fetched_at=fetched_at,
        intervals=(_interval("T4_A", "Bridgewatch", fetched_at),),
        failures=(BatchFailure(1, ("T4_B",), "offline"),),
    )

    counts = worker._store_coverage(result, fetched_at)

    statuses = {(row.item_id, row.city): row.status for row in repository.coverage}
    assert statuses == {
        ("T4_A", "Bridgewatch"): "success",
        ("T4_A", "Martlock"): "empty",
        ("T4_B", "Bridgewatch"): "failed",
        ("T4_B", "Martlock"): "failed",
        ("T4_C", "Bridgewatch"): "empty",
        ("T4_C", "Martlock"): "empty",
    }
    assert counts == {"success": 1, "empty": 3, "failed": 2}
    failed = next(row for row in repository.coverage if row.status == "failed")
    assert failed.error_message == "offline"


def test_history_row_failure_marks_unkeyed_coverage_partial() -> None:
    fetched_at = datetime(2026, 8, 18, 12, tzinfo=UTC)
    repository = RecordingHistoryRepository()
    worker = HistoryRefreshWorker(
        Region.AMERICAS,
        repository,  # type: ignore[arg-type]
        ("T4_A",),
        ("Bridgewatch", "Martlock"),
        7,
    )
    result = _result(
        fetched_at=fetched_at,
        intervals=(_interval("T4_A", "Bridgewatch", fetched_at),),
        record_failures=(HistoryRecordFailure(1, 1, 2, "bad point"),),
    )

    counts = worker._store_coverage(result, fetched_at)

    assert counts == {"partial": 2}
    assert [row.record_count for row in repository.coverage] == [1, 0]
    assert all(row.error_message for row in repository.coverage)


def test_history_worker_is_explicit_bounded_and_prunes_retention() -> None:
    repository = RecordingHistoryRepository()
    calls = []

    class EmptyClient:
        def fetch_history(
            self,
            item_ids,
            *,
            start_date,
            end_date,
            cities,
            qualities,
            time_scale,
        ) -> HistoryFetchResult:
            calls.append((item_ids, start_date, end_date, cities, qualities, time_scale))
            return HistoryFetchResult(
                intervals=(),
                failures=(),
                record_failures=(),
                batch_count=1,
                items_requested=len(item_ids),
                successful_batches=1,
                elapsed_seconds=0.5,
                start_date=start_date,
                end_date=end_date,
                time_scale=time_scale,
            )

    client = EmptyClient()
    worker = HistoryRefreshWorker(
        Region.AMERICAS,
        repository,  # type: ignore[arg-type]
        ("T4_A",),
        ("Bridgewatch", "Martlock"),
        7,
        client_factory=lambda _region: client,  # type: ignore[arg-type,return-value]
    )
    summaries = []
    errors = []
    worker.signals.finished.connect(summaries.append)
    worker.signals.error.connect(errors.append)

    before = datetime.now(UTC)
    worker.run()
    after = datetime.now(UTC)

    assert not errors
    assert len(summaries) == 1
    assert summaries[0].empty_coverage == 2
    assert summaries[0].pruned_intervals == 4
    assert calls[0][2] - calls[0][1] == timedelta(days=7)
    assert calls[0][5] is HistoryTimeScale.SIX_HOURLY
    assert repository.prune_cutoff is not None
    assert before - timedelta(days=HISTORY_RETENTION_DAYS) <= repository.prune_cutoff
    assert repository.prune_cutoff <= after - timedelta(days=HISTORY_RETENTION_DAYS)


def test_market_health_counts_each_side_and_constructor_remains_compatible() -> None:
    now = datetime.now(UTC)
    records = (
        MarketPrice(
            "T4_A",
            "Bridgewatch",
            1,
            Region.AMERICAS,
            100,
            now,
            90,
            now - timedelta(hours=3),
            now,
        ),
        MarketPrice(
            "T4_B",
            "Martlock",
            1,
            Region.AMERICAS,
            200,
            now - timedelta(hours=6),
            None,
            None,
            now,
        ),
        MarketPrice(
            "T4_C",
            "Thetford",
            1,
            Region.AMERICAS,
            300,
            now + timedelta(minutes=5),
            None,
            None,
            now,
        ),
    )

    text = MarketDataView._market_health_text(
        records,
        FreshnessPolicy(timedelta(hours=4), aging_after=timedelta(hours=2)),
    )

    assert "1 fresh" in text
    assert "1 aging" in text
    assert "1 stale" in text
    assert "1 future-dated (invalid)" in text
    assert "2 missing" in text
    assert signature(MarketDataView.__init__).parameters["history"].default is None


def test_market_table_uses_stable_interactive_column_widths(qt_app, tmp_path) -> None:
    view = _view(tmp_path)
    header = view.table.horizontalHeader()

    assert all(
        header.sectionResizeMode(column) is QHeaderView.ResizeMode.Interactive
        for column in range(len(view.HEADERS) - 1)
    )


def test_history_completion_does_not_rebuild_current_price_table(
    qt_app,
    tmp_path,
    monkeypatch,
) -> None:
    view = _view(tmp_path)
    fetched_at = datetime.now(UTC)
    summary = HistoryRefreshSummary(
        result=_result(fetched_at=fetched_at),
        coverage_total=1,
        success_coverage=0,
        empty_coverage=1,
        partial_coverage=0,
        failed_coverage=0,
        pruned_intervals=0,
    )
    worker = HistoryRefreshWorker(
        Region.AMERICAS,
        RecordingHistoryRepository(),  # type: ignore[arg-type]
        ("T4_A",),
        ("Bridgewatch",),
        7,
    )
    view._workers.add(worker)
    monkeypatch.setattr(
        view,
        "reload",
        lambda **_kwargs: pytest.fail("history completion rebuilt the current-price table"),
    )

    view._history_finished(worker, summary)

    assert worker not in view._workers
    assert view.history_button.isEnabled()
    assert "successful-empty" in view.status.text()
