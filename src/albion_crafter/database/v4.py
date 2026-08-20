from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from albion_crafter.market.models import Region
from albion_crafter.planning.models import (
    LEGACY_SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    V2_SNAPSHOT_FORMAT_VERSION,
    FindMoneyConstraints,
    OptimizationStatus,
    PlanSnapshot,
    PlanStatus,
)

from .database import Database, SettingsRepository, _parse_datetime, _serialize_datetime

DEFAULT_PLAN_RETENTION = 20
MAX_PLAN_RETENTION = 100
MAX_SNAPSHOT_PAYLOAD_BYTES = 10 * 1024 * 1024
LEGACY_FIND_MONEY_PREFERENCES_KEY = "find_money.preferences.v1"
V2_FIND_MONEY_PREFERENCES_KEY = "find_money.preferences.v2"
FIND_MONEY_PREFERENCES_KEY = "find_money.preferences.v3"
FIND_MONEY_PREFERENCES_VERSION = 3


class PlanSnapshotError(ValueError):
    """Base class for a persisted plan that cannot be used safely."""


class PlanSnapshotIntegrityError(PlanSnapshotError):
    """Raised when a snapshot payload or its indexed metadata is inconsistent."""


class UnsupportedPlanSnapshotVersion(PlanSnapshotError):
    """Raised when a snapshot was written by an unsupported format version."""


class PlanSnapshotAlreadyExists(PlanSnapshotError):
    """Raised rather than mutating an existing immutable snapshot."""


class FindMoneyPreferencesError(ValueError):
    """Raised when persisted Find Me Money preferences are malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class PlanSnapshotSummary:
    snapshot_id: str
    created_at: datetime
    completed_at: datetime
    region: Region
    plan_status: PlanStatus
    optimization_status: OptimizationStatus
    catalog_source_version: str
    mechanics_ruleset_id: str
    action_count: int
    total_pre_revenue_cash: int
    total_focus: int
    total_expected_profit: int
    silver_remaining: int
    focus_remaining: int
    oldest_market_observed_at: datetime | None
    oldest_station_observed_at: datetime | None


def canonical_snapshot_json(snapshot: PlanSnapshot) -> str:
    """Serialize a snapshot deterministically for hashing, persistence, and JSON export."""

    return json.dumps(
        snapshot.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class PlanSnapshotRepository:
    """Immutable plan persistence with deterministic, bounded newest-first retention."""

    def __init__(
        self,
        database: Database,
        *,
        retention_limit: int = DEFAULT_PLAN_RETENTION,
    ) -> None:
        if (
            isinstance(retention_limit, bool)
            or not isinstance(retention_limit, int)
            or not 1 <= retention_limit <= MAX_PLAN_RETENTION
        ):
            raise ValueError(
                f"retention_limit must be an integer between 1 and {MAX_PLAN_RETENTION}"
            )
        self.database = database
        self.retention_limit = retention_limit

    def save(self, snapshot: PlanSnapshot) -> PlanSnapshot:
        payload = canonical_snapshot_json(snapshot)
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > MAX_SNAPSHOT_PAYLOAD_BYTES:
            raise ValueError(
                f"plan snapshot exceeds the {MAX_SNAPSHOT_PAYLOAD_BYTES:,}-byte safety limit"
            )
        digest = hashlib.sha256(payload_bytes).hexdigest()
        with self.database.connection() as connection:
            # Serialize writers before checking the immutable key so concurrent
            # saves cannot race the duplicate check or retention pruning.
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM plan_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if exists is not None:
                raise PlanSnapshotAlreadyExists(
                    f"plan snapshot {snapshot.snapshot_id!r} already exists"
                )
            connection.execute(
                """
                INSERT INTO plan_snapshots (
                    snapshot_id, snapshot_format_version, created_at, completed_at,
                    region, plan_status, optimization_status, catalog_source_version,
                    mechanics_ruleset_id, action_count, total_pre_revenue_cash,
                    total_focus, total_expected_profit, silver_remaining,
                    focus_remaining, oldest_market_observed_at,
                    oldest_station_observed_at, payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.snapshot_format_version,
                    _serialize_datetime(snapshot.created_at),
                    _serialize_datetime(snapshot.completed_at),
                    snapshot.region.value,
                    snapshot.plan_status.value,
                    snapshot.optimizer.status.value,
                    snapshot.catalog_source_version,
                    snapshot.mechanics_ruleset_id,
                    len(snapshot.actions),
                    snapshot.total_pre_revenue_cash,
                    snapshot.total_focus,
                    snapshot.total_expected_profit,
                    snapshot.silver_remaining,
                    snapshot.focus_remaining,
                    _serialize_datetime(snapshot.oldest_market_observed_at),
                    _serialize_datetime(snapshot.oldest_station_observed_at),
                    payload,
                    digest,
                ),
            )
            connection.execute(
                """
                DELETE FROM plan_snapshots
                WHERE snapshot_id IN (
                    SELECT snapshot_id FROM plan_snapshots
                    ORDER BY created_at DESC, snapshot_id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.retention_limit,),
            )
        return snapshot

    def load(self, snapshot_id: str) -> PlanSnapshot | None:
        if not snapshot_id.strip():
            raise ValueError("snapshot_id is required")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM plan_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        return None if row is None else self._snapshot_from_row(row)

    def list_recent(self, limit: int | None = None) -> list[PlanSnapshot]:
        selected_limit = self._validated_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM plan_snapshots
                   ORDER BY created_at DESC, snapshot_id DESC LIMIT ?""",
                (selected_limit,),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def list_summaries(self, limit: int | None = None) -> list[PlanSnapshotSummary]:
        selected_limit = self._validated_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM plan_snapshots
                   ORDER BY created_at DESC, snapshot_id DESC LIMIT ?""",
                (selected_limit,),
            ).fetchall()
        return [self._summary_from_snapshot(self._snapshot_from_row(row)) for row in rows]

    def remove(self, snapshot_id: str) -> bool:
        if not snapshot_id.strip():
            raise ValueError("snapshot_id is required")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM plan_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            )
            return cursor.rowcount > 0

    def count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM plan_snapshots").fetchone()[0])

    def _validated_limit(self, limit: int | None) -> int:
        selected = self.retention_limit if limit is None else limit
        if (
            isinstance(selected, bool)
            or not isinstance(selected, int)
            or not 1 <= selected <= MAX_PLAN_RETENTION
        ):
            raise ValueError(f"limit must be an integer between 1 and {MAX_PLAN_RETENTION}")
        return selected

    @staticmethod
    def _snapshot_from_row(row) -> PlanSnapshot:
        payload = str(row["payload_json"])
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > MAX_SNAPSHOT_PAYLOAD_BYTES:
            raise PlanSnapshotIntegrityError(
                f"plan snapshot exceeds the {MAX_SNAPSHOT_PAYLOAD_BYTES:,}-byte safety limit"
            )
        actual_digest = hashlib.sha256(payload_bytes).hexdigest()
        if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
            raise PlanSnapshotIntegrityError(
                f"plan snapshot {row['snapshot_id']!r} failed its SHA-256 integrity check"
            )
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlanSnapshotIntegrityError("plan snapshot payload is not valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise PlanSnapshotIntegrityError("plan snapshot payload must be a JSON object")

        payload_version = decoded.get("snapshot_format_version")
        stored_version = row["snapshot_format_version"]
        if isinstance(payload_version, bool) or not isinstance(payload_version, int):
            raise PlanSnapshotIntegrityError("plan snapshot format version is malformed")
        if payload_version != stored_version:
            raise PlanSnapshotIntegrityError(
                "plan snapshot payload and indexed format versions disagree"
            )
        if payload_version not in {
            LEGACY_SNAPSHOT_FORMAT_VERSION,
            V2_SNAPSHOT_FORMAT_VERSION,
            SNAPSHOT_FORMAT_VERSION,
        }:
            raise UnsupportedPlanSnapshotVersion(
                f"unsupported plan snapshot format version {payload_version}"
            )
        try:
            snapshot = PlanSnapshot.from_dict(decoded)
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise PlanSnapshotIntegrityError(f"plan snapshot payload is invalid: {exc}") from exc
        if payload != canonical_snapshot_json(snapshot):
            raise PlanSnapshotIntegrityError(
                "plan snapshot payload is not in its canonical serialized form"
            )
        try:
            PlanSnapshotRepository._verify_indexed_metadata(row, snapshot)
        except PlanSnapshotIntegrityError:
            raise
        except (OverflowError, TypeError, ValueError) as exc:
            raise PlanSnapshotIntegrityError("plan snapshot indexed metadata is invalid") from exc
        return snapshot

    @staticmethod
    def _verify_indexed_metadata(row, snapshot: PlanSnapshot) -> None:
        expected: dict[str, Any] = {
            "snapshot_id": snapshot.snapshot_id,
            "region": snapshot.region.value,
            "plan_status": snapshot.plan_status.value,
            "optimization_status": snapshot.optimizer.status.value,
            "catalog_source_version": snapshot.catalog_source_version,
            "mechanics_ruleset_id": snapshot.mechanics_ruleset_id,
            "action_count": len(snapshot.actions),
            "total_pre_revenue_cash": snapshot.total_pre_revenue_cash,
            "total_focus": snapshot.total_focus,
            "total_expected_profit": snapshot.total_expected_profit,
            "silver_remaining": snapshot.silver_remaining,
            "focus_remaining": snapshot.focus_remaining,
        }
        mismatches = [name for name, value in expected.items() if row[name] != value]
        for name, value in (
            ("created_at", snapshot.created_at),
            ("completed_at", snapshot.completed_at),
            ("oldest_market_observed_at", snapshot.oldest_market_observed_at),
            ("oldest_station_observed_at", snapshot.oldest_station_observed_at),
        ):
            if _parse_datetime(row[name]) != value:
                mismatches.append(name)
        if mismatches:
            raise PlanSnapshotIntegrityError(
                "plan snapshot indexed metadata disagrees with its payload: "
                + ", ".join(sorted(mismatches))
            )

    @staticmethod
    def _summary_from_snapshot(snapshot: PlanSnapshot) -> PlanSnapshotSummary:
        return PlanSnapshotSummary(
            snapshot_id=snapshot.snapshot_id,
            created_at=snapshot.created_at,
            completed_at=snapshot.completed_at,
            region=snapshot.region,
            plan_status=snapshot.plan_status,
            optimization_status=snapshot.optimizer.status,
            catalog_source_version=snapshot.catalog_source_version,
            mechanics_ruleset_id=snapshot.mechanics_ruleset_id,
            action_count=len(snapshot.actions),
            total_pre_revenue_cash=snapshot.total_pre_revenue_cash,
            total_focus=snapshot.total_focus,
            total_expected_profit=snapshot.total_expected_profit,
            silver_remaining=snapshot.silver_remaining,
            focus_remaining=snapshot.focus_remaining,
            oldest_market_observed_at=snapshot.oldest_market_observed_at,
            oldest_station_observed_at=snapshot.oldest_station_observed_at,
        )


class FindMoneyPreferencesRepository:
    """Typed, versioned façade over the existing namespaced settings storage."""

    def __init__(self, settings: SettingsRepository | Database) -> None:
        self.settings = (
            settings if isinstance(settings, SettingsRepository) else SettingsRepository(settings)
        )

    def save(self, constraints: FindMoneyConstraints) -> FindMoneyConstraints:
        if not isinstance(constraints, FindMoneyConstraints):
            raise TypeError("constraints must be a FindMoneyConstraints instance")
        self.settings.set(
            FIND_MONEY_PREFERENCES_KEY,
            {
                "format_version": FIND_MONEY_PREFERENCES_VERSION,
                "constraints": constraints.to_dict(format_version=FIND_MONEY_PREFERENCES_VERSION),
            },
        )
        return constraints

    def load(
        self,
        default: FindMoneyConstraints | None = None,
    ) -> FindMoneyConstraints | None:
        if default is not None and not isinstance(default, FindMoneyConstraints):
            raise TypeError("default must be a FindMoneyConstraints instance or None")
        missing = object()
        try:
            payload = self.settings.get(FIND_MONEY_PREFERENCES_KEY, missing)
            source_version = FIND_MONEY_PREFERENCES_VERSION
            if payload is missing:
                payload = self.settings.get(V2_FIND_MONEY_PREFERENCES_KEY, missing)
                source_version = V2_SNAPSHOT_FORMAT_VERSION
            if payload is missing:
                payload = self.settings.get(LEGACY_FIND_MONEY_PREFERENCES_KEY, missing)
                source_version = LEGACY_SNAPSHOT_FORMAT_VERSION
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FindMoneyPreferencesError("Find Me Money preferences are not valid JSON") from exc
        if payload is missing:
            return default
        if not isinstance(payload, Mapping):
            raise FindMoneyPreferencesError("Find Me Money preferences must be a JSON object")
        unknown_envelope = sorted(
            str(key) for key in payload if key not in {"format_version", "constraints"}
        )
        if unknown_envelope:
            raise FindMoneyPreferencesError(
                "Find Me Money preference envelope contains unknown fields: "
                + ", ".join(unknown_envelope)
            )
        version = payload.get("format_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise FindMoneyPreferencesError("Find Me Money preference version is malformed")
        if version != source_version:
            raise FindMoneyPreferencesError(
                f"unsupported Find Me Money preference version {version}"
            )
        raw_constraints = payload.get("constraints")
        if not isinstance(raw_constraints, Mapping):
            raise FindMoneyPreferencesError("Find Me Money constraints must be a JSON object")
        self._validate_json_types(raw_constraints, format_version=source_version)
        try:
            constraints = FindMoneyConstraints.from_dict(
                raw_constraints,
                format_version=source_version,
            )
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise FindMoneyPreferencesError(
                f"Find Me Money preferences are invalid: {exc}"
            ) from exc
        if source_version < FIND_MONEY_PREFERENCES_VERSION:
            # Preserve older envelopes verbatim and write a separate V3 value.
            # V1 remains crafting-only; V2 retains Craft/Refine and leaves
            # Arbitrage disabled until the user explicitly enables it.
            self.save(constraints)
        return constraints

    @staticmethod
    def _validate_json_types(
        value: Mapping[str, Any],
        *,
        format_version: int = FIND_MONEY_PREFERENCES_VERSION,
    ) -> None:
        known_keys = {
            "available_silver",
            "available_focus",
            "region",
            "silver_reserve",
            "focus_reserve",
            "premium",
            "item_query",
            "tiers",
            "enchantments",
            "categories",
            "material_cities",
            "sell_cities",
            "use_focus",
            "max_market_age_seconds",
            "max_station_fee_age_seconds",
            "allow_stale_station_fees",
            "minimum_profit",
            "minimum_roi",
            "minimum_liquidity",
            "sale_method",
            "transport_policy",
            "per_item_craft_cap",
            "historical_volume_share",
            "history_enabled",
            "history_shortlist_limit",
            "force_current_price_refresh",
        }
        if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION:
            known_keys.add("craft_cities")
        else:
            known_keys.update({"production_cities", "action_kinds", "refining_families"})
        if format_version <= V2_SNAPSHOT_FORMAT_VERSION:
            known_keys.add("transport_cost_per_craft")
        else:
            known_keys.update(
                {
                    "transport_cost_per_action_unit",
                    "arbitrage_scope",
                    "arbitrage_source_cities",
                    "arbitrage_destination_cities",
                }
            )
        unknown = sorted(str(key) for key in value if key not in known_keys)
        if unknown:
            raise FindMoneyPreferencesError(
                "Find Me Money preferences contain unknown fields: " + ", ".join(unknown)
            )

        for key in (
            "premium",
            "use_focus",
            "allow_stale_station_fees",
            "history_enabled",
            "force_current_price_refresh",
        ):
            if key in value and not isinstance(value[key], bool):
                raise FindMoneyPreferencesError(f"{key} preference must be a boolean")

        for key in (
            "available_silver",
            "available_focus",
            "silver_reserve",
            "focus_reserve",
            "per_item_craft_cap",
            "history_shortlist_limit",
        ):
            if key in value and (isinstance(value[key], bool) or not isinstance(value[key], int)):
                raise FindMoneyPreferencesError(f"{key} preference must be an integer")
        for key in (
            "minimum_profit",
            (
                "transport_cost_per_craft"
                if format_version <= V2_SNAPSHOT_FORMAT_VERSION
                else "transport_cost_per_action_unit"
            ),
        ):
            item = value.get(key)
            if item is not None and (isinstance(item, bool) or not isinstance(item, int)):
                raise FindMoneyPreferencesError(f"{key} preference must be an integer or null")

        for key in ("max_market_age_seconds", "max_station_fee_age_seconds"):
            item = value.get(key)
            if key in value and (isinstance(item, bool) or not isinstance(item, (int, float))):
                raise FindMoneyPreferencesError(f"{key} preference must be numeric")
        for key in ("minimum_roi", "historical_volume_share"):
            item = value.get(key)
            if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float))):
                raise FindMoneyPreferencesError(f"{key} preference must be numeric or null")

        for key in (
            "region",
            "item_query",
            "minimum_liquidity",
            "sale_method",
            "transport_policy",
            *(("arbitrage_scope",) if format_version >= SNAPSHOT_FORMAT_VERSION else ()),
        ):
            if key in value and not isinstance(value[key], str):
                raise FindMoneyPreferencesError(f"{key} preference must be a string")

        for key in (
            "tiers",
            "enchantments",
            "categories",
            "material_cities",
            (
                "craft_cities"
                if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION
                else "production_cities"
            ),
            "sell_cities",
            *(
                ()
                if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION
                else ("action_kinds", "refining_families")
            ),
            *(
                ("arbitrage_source_cities", "arbitrage_destination_cities")
                if format_version >= SNAPSHOT_FORMAT_VERSION
                else ()
            ),
        ):
            if key in value and not isinstance(value[key], list):
                raise FindMoneyPreferencesError(f"{key} preference must be a JSON list")
        for key in ("tiers", "enchantments"):
            item = value.get(key)
            if isinstance(item, list) and any(
                isinstance(member, bool) or not isinstance(member, int) for member in item
            ):
                raise FindMoneyPreferencesError(f"{key} preference must contain only integers")
        for key in (
            "categories",
            "material_cities",
            (
                "craft_cities"
                if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION
                else "production_cities"
            ),
            "sell_cities",
            *(
                ()
                if format_version == LEGACY_SNAPSHOT_FORMAT_VERSION
                else ("action_kinds", "refining_families")
            ),
            *(
                ("arbitrage_source_cities", "arbitrage_destination_cities")
                if format_version >= SNAPSHOT_FORMAT_VERSION
                else ()
            ),
        ):
            item = value.get(key)
            if isinstance(item, list) and any(not isinstance(member, str) for member in item):
                raise FindMoneyPreferencesError(f"{key} preference must contain only strings")
