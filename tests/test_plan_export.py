from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from albion_crafter.core.models import SaleMethod
from albion_crafter.market.liquidity import LiquidityLevel
from albion_crafter.market.models import Region
from albion_crafter.planning.export import export_plan_csv, export_plan_json
from albion_crafter.planning.models import (
    CandidateRoute,
    FindMoneyConstraints,
    OptimizationDiagnostics,
    OptimizationStatus,
    PlanAction,
    PlanDataHealth,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanSnapshot,
    PlanStatus,
    RefreshStatistics,
    TransportPolicy,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=timezone(timedelta(hours=-6)))


def _snapshot() -> PlanSnapshot:
    constraints = FindMoneyConstraints(
        available_silver=1_000_000,
        available_focus=10_000,
        silver_reserve=100_000,
        focus_reserve=1_000,
        item_query="sword",
        material_cities=("\tBridgewatch",),
        craft_cities=("\tBridgewatch",),
        sell_cities=("\tBridgewatch",),
        allow_stale_station_fees=True,
    )
    route = CandidateRoute(
        Region.AMERICAS,
        "\tBridgewatch",
        "\tBridgewatch",
        "\tBridgewatch",
        TransportPolicy.LOCAL_ONLY,
    )
    reason = PlanReason(
        PlanReasonCode.OTHER,
        "-Inspect the evidence before executing.",
        PlanReasonSeverity.WARNING,
    )
    first = PlanAction(
        candidate_id="+candidate-1",
        item_id="@T4_MAIN_SWORD",
        display_name='  =WEBSERVICE("https://example.invalid")',
        route=route,
        quantity=2,
        focused_quantity=1,
        nonfocused_quantity=1,
        output_units=2,
        quality=1,
        sale_method=SaleMethod.SELL_ORDER,
        pre_revenue_cash_required=200_000,
        focus_required=800,
        expected_profit=-500,
        liquidity=LiquidityLevel.MODERATE,
        execution_capacity_key=(Region.AMERICAS, "@T4_MAIN_SWORD", "\tBridgewatch", 1),
        quantity_ceiling=4,
        execution_ceiling_output_units=4,
        expected_revenue=230_000,
        effective_economic_cost=230_500,
        reasons=(reason,),
        evidence=(
            ("materials", '[{"item_id":"T4_BAR","quantity":32}]'),
            ("warning", "@untrusted spreadsheet text"),
        ),
        oldest_market_observed_at=NOW - timedelta(hours=1),
        station_fee_observed_at=NOW - timedelta(hours=2),
    )
    second = replace(
        first,
        candidate_id="candidate-2",
        display_name="Adept Sword (second route)",
        expected_profit=2_500,
    )
    return PlanSnapshot(
        snapshot_id="=snapshot-1",
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
        region=Region.AMERICAS,
        constraints=constraints,
        actions=(first, second),
        total_pre_revenue_cash=400_000,
        total_focus=1_600,
        total_expected_profit=2_000,
        silver_remaining=500_000,
        focus_remaining=7_400,
        plan_status=PlanStatus.ADVISORY,
        reasons=(reason,),
        optimizer=OptimizationDiagnostics(
            "@pareto-frontier-v1",
            OptimizationStatus.EXACT,
            candidate_count=2,
            group_count=1,
            quantity_decision_count=4,
            states_considered=8,
            states_pruned=3,
            state_limit=50_000,
            state_limit_reached=False,
            elapsed_seconds=0.01,
            quantization_policy="test-policy-v1",
        ),
        catalog_source_version="\rcatalog-v3",
        mechanics_ruleset_id="-rules-v4",
        assumptions=("Normal quality only",),
        data_health=PlanDataHealth(
            market_observations_used=6,
            market_fresh=6,
            station_fees_used=2,
            station_fees_fresh=2,
            mechanics_status="verified",
        ),
        current_refresh=RefreshStatistics(6, 1, 1, 0, 6, 0.1),
        history_refresh=RefreshStatistics(2, 1, 1, 0, 8, 0.2),
        oldest_market_observed_at=first.oldest_market_observed_at,
        oldest_station_observed_at=first.station_fee_observed_at,
        metadata=(("test", "complete export fixture"),),
    )


def test_json_export_is_complete_round_trippable_and_normalizes_timestamps(tmp_path) -> None:
    snapshot = _snapshot()
    destination = tmp_path / "plan.json"

    assert export_plan_json(snapshot, destination) == destination

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert PlanSnapshot.from_dict(payload) == snapshot
    assert payload == snapshot.to_dict()
    assert payload["created_at"] == "2026-08-18T18:00:00Z"
    assert payload["optimizer"]["quantization_policy"] == "test-policy-v1"
    assert payload["actions"][0]["evidence"] == [
        ["materials", '[{"item_id":"T4_BAR","quantity":32}]'],
        ["warning", "@untrusted spreadsheet text"],
    ]


def test_csv_export_has_one_formula_safe_row_per_action_and_complete_evidence(tmp_path) -> None:
    snapshot = _snapshot()
    destination = tmp_path / "plan.csv"

    assert export_plan_csv(snapshot, destination) == destination

    with destination.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(snapshot.actions) == 2
    first = rows[0]
    assert first["snapshot_id"] == "'=snapshot-1"
    assert first["candidate_id"] == "'+candidate-1"
    assert first["item_id"] == "'@T4_MAIN_SWORD"
    assert first["display_name"] == '\'  =WEBSERVICE("https://example.invalid")'
    assert first["material_city"] == "'\tBridgewatch"
    assert first["catalog_source_version"] == "'\rcatalog-v3"
    assert first["mechanics_ruleset_id"] == "'-rules-v4"
    assert first["optimizer_method"] == "'@pareto-frontier-v1"
    assert first["action_reason_messages"] == "'-Inspect the evidence before executing."
    assert first["plan_reason_messages"] == "'-Inspect the evidence before executing."
    assert first["expected_profit"] == "-500"
    assert json.loads(first["action_evidence_json"]) == dict(snapshot.actions[0].evidence)
    assert json.loads(first["action_json"]) == snapshot.to_dict()["actions"][0]
    assert json.loads(first["data_health_json"]) == snapshot.to_dict()["data_health"]
    assert json.loads(first["current_refresh_json"]) == snapshot.to_dict()["current_refresh"]
    assert json.loads(first["history_refresh_json"]) == snapshot.to_dict()["history_refresh"]
    assert json.loads(first["optimizer_json"]) == snapshot.to_dict()["optimizer"]
    assert json.loads(first["metadata_json"]) == snapshot.to_dict()["metadata"]


@pytest.mark.parametrize(
    ("exporter", "filename"),
    ((export_plan_json, "plan.json"), (export_plan_csv, "plan.csv")),
)
def test_export_replace_failure_preserves_destination_and_cleans_temp_file(
    tmp_path,
    monkeypatch,
    exporter,
    filename,
) -> None:
    destination = tmp_path / filename
    destination.write_bytes(b"healthy existing export")
    original_files = set(tmp_path.iterdir())

    def fail_replace(source, target) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("albion_crafter.planning.export.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        exporter(_snapshot(), destination)

    assert destination.read_bytes() == b"healthy existing export"
    assert set(tmp_path.iterdir()) == original_files


def test_export_does_not_create_an_unrequested_parent_directory(tmp_path) -> None:
    parent = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="export directory does not exist"):
        export_plan_json(_snapshot(), parent / "plan.json")
    assert not parent.exists()
