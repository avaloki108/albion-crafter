from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from albion_crafter.core.models import ActionKind

from .models import PlanSnapshot

CSV_HEADERS = (
    "snapshot_id",
    "created_at",
    "completed_at",
    "plan_status",
    "optimization_status",
    "optimizer_method",
    "region",
    "catalog_source_version",
    "mechanics_ruleset_id",
    "starting_silver",
    "silver_reserve",
    "silver_committed",
    "silver_remaining",
    "starting_focus",
    "focus_reserve",
    "focus_committed",
    "focus_remaining",
    "plan_expected_profit",
    "action_kind",
    "candidate_id",
    "item_id",
    "display_name",
    "action_quantity",
    "craft_quantity",
    "production_quantity",
    "focused_quantity",
    "nonfocused_quantity",
    "output_units",
    "source_city",
    "destination_city",
    "material_city",
    "craft_city",
    "production_city",
    "sell_city",
    "transport_policy",
    "transport_cost_per_craft",
    "transport_cost_per_action_unit",
    "quality",
    "sale_method",
    "pre_revenue_cash_required",
    "focus_required",
    "expected_revenue",
    "effective_economic_cost",
    "expected_profit",
    "roi",
    "margin",
    "silver_per_focus",
    "incremental_focus_profit",
    "liquidity",
    "quantity_ceiling",
    "execution_ceiling_output_units",
    "capacity_requirements_json",
    "oldest_market_observed_at",
    "station_fee_observed_at",
    "action_reason_codes",
    "action_reason_messages",
    "action_evidence_json",
    "action_json",
    "plan_reason_codes",
    "plan_reason_messages",
    "assumptions_json",
    "data_health_json",
    "current_refresh_json",
    "history_refresh_json",
    "optimizer_json",
    "metadata_json",
)


def export_plan_json(snapshot: PlanSnapshot, destination: str | Path) -> Path:
    """Atomically export the complete immutable snapshot envelope as JSON."""

    payload = json.dumps(
        snapshot.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    return _atomic_write(Path(destination), "utf-8", lambda stream: stream.write(payload + "\n"))


def export_plan_csv(snapshot: PlanSnapshot, destination: str | Path) -> Path:
    """Atomically export one spreadsheet-friendly row per immutable plan action."""

    envelope = snapshot.to_dict()
    serialized_actions = envelope["actions"]

    def write_csv(stream: TextIO) -> None:
        writer = csv.DictWriter(stream, fieldnames=CSV_HEADERS, extrasaction="raise")
        writer.writeheader()
        for action, serialized_action in zip(
            snapshot.actions,
            serialized_actions,
            strict=True,
        ):
            row = _action_row(snapshot, envelope, action, serialized_action)
            writer.writerow({key: _formula_safe(value) for key, value in row.items()})

    return _atomic_write(Path(destination), "utf-8-sig", write_csv)


# Explicit snapshot aliases make call sites readable without creating a second policy.
export_snapshot_json = export_plan_json
export_snapshot_csv = export_plan_csv


def _action_row(
    snapshot: PlanSnapshot,
    envelope: Mapping[str, Any],
    action,
    serialized_action: Mapping[str, Any],
) -> dict[str, Any]:
    plan_reason_codes = ";".join(reason.code.value for reason in snapshot.reasons)
    plan_reason_messages = " | ".join(reason.message for reason in snapshot.reasons)
    action_reason_codes = ";".join(reason.code.value for reason in action.reasons)
    action_reason_messages = " | ".join(reason.message for reason in action.reasons)
    route = action.route
    is_arbitrage = action.action_kind is ActionKind.ARBITRAGE
    return {
        "snapshot_id": snapshot.snapshot_id,
        "created_at": _datetime_text(snapshot.created_at),
        "completed_at": _datetime_text(snapshot.completed_at),
        "plan_status": snapshot.plan_status.value,
        "optimization_status": snapshot.optimizer.status.value,
        "optimizer_method": snapshot.optimizer.method,
        "region": snapshot.region.value,
        "catalog_source_version": snapshot.catalog_source_version,
        "mechanics_ruleset_id": snapshot.mechanics_ruleset_id,
        "starting_silver": snapshot.constraints.available_silver,
        "silver_reserve": snapshot.constraints.silver_reserve,
        "silver_committed": snapshot.total_pre_revenue_cash,
        "silver_remaining": snapshot.silver_remaining,
        "starting_focus": snapshot.constraints.available_focus,
        "focus_reserve": snapshot.constraints.focus_reserve,
        "focus_committed": snapshot.total_focus,
        "focus_remaining": snapshot.focus_remaining,
        "plan_expected_profit": snapshot.total_expected_profit,
        "action_kind": action.action_kind.value,
        "candidate_id": action.candidate_id,
        "item_id": action.item_id,
        "display_name": action.display_name,
        "action_quantity": action.quantity,
        "craft_quantity": action.quantity,
        "production_quantity": action.quantity,
        "focused_quantity": action.focused_quantity,
        "nonfocused_quantity": action.nonfocused_quantity,
        "output_units": action.output_units,
        "source_city": route.buy_city if is_arbitrage else "",
        "destination_city": route.sell_city if is_arbitrage else "",
        "material_city": route.material_city,
        "craft_city": "" if is_arbitrage else route.craft_city,
        "production_city": "" if is_arbitrage else route.production_city,
        "sell_city": route.sell_city,
        "transport_policy": route.transport_policy.value,
        "transport_cost_per_craft": route.transport_cost_per_craft,
        "transport_cost_per_action_unit": route.transport_cost_per_action_unit,
        "quality": action.quality,
        "sale_method": action.sale_method.value,
        "pre_revenue_cash_required": action.pre_revenue_cash_required,
        "focus_required": action.focus_required,
        "expected_revenue": action.expected_revenue,
        "effective_economic_cost": action.effective_economic_cost,
        "expected_profit": action.expected_profit,
        "roi": action.roi,
        "margin": action.margin,
        "silver_per_focus": action.silver_per_focus,
        "incremental_focus_profit": action.incremental_focus_profit,
        "liquidity": action.liquidity.value,
        "quantity_ceiling": action.quantity_ceiling,
        "execution_ceiling_output_units": action.execution_ceiling_output_units,
        "capacity_requirements_json": _compact_json(
            serialized_action.get("capacity_requirements", [])
        ),
        "oldest_market_observed_at": _optional_datetime_text(action.oldest_market_observed_at),
        "station_fee_observed_at": _optional_datetime_text(action.station_fee_observed_at),
        "action_reason_codes": action_reason_codes,
        "action_reason_messages": action_reason_messages,
        "action_evidence_json": _compact_json(dict(action.evidence)),
        "action_json": _compact_json(serialized_action),
        "plan_reason_codes": plan_reason_codes,
        "plan_reason_messages": plan_reason_messages,
        "assumptions_json": _compact_json(envelope["assumptions"]),
        "data_health_json": _compact_json(envelope["data_health"]),
        "current_refresh_json": _compact_json(envelope["current_refresh"]),
        "history_refresh_json": _compact_json(envelope["history_refresh"]),
        "optimizer_json": _compact_json(envelope["optimizer"]),
        "metadata_json": _compact_json(envelope["metadata"]),
    }


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _formula_safe(value: Any) -> Any:
    """Prevent user/static text from becoming a spreadsheet formula."""

    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" \t\r\n")
    if value[0] in "\t\r" or (stripped and stripped[0] in "=+-@"):
        return "'" + value
    return value


def _atomic_write(
    destination: Path,
    encoding: str,
    writer: Callable[[TextIO], object],
) -> Path:
    if not destination.name:
        raise ValueError("export destination must name a file")
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"export directory does not exist: {parent}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding=encoding, newline="")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_datetime_text(value: datetime | None) -> str:
    return "" if value is None else _datetime_text(value)
