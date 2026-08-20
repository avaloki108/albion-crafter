from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillProfile,
    ManualFocusEfficiencyOverride,
)
from albion_crafter.core.freshness import DEFAULT_CLOCK_SKEW_TOLERANCE
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationFeeObservation, StationType
from albion_crafter.market.history import HistoryTimeScale, MarketHistoryInterval
from albion_crafter.market.models import Region

from .database import Database, _parse_datetime, _serialize_datetime

DEFAULT_HISTORY_RETENTION = timedelta(days=30)


def _region_value(region: Region | str) -> str:
    return region.value if isinstance(region, Region) else str(region)


class StationFeeRepository:
    def __init__(
        self,
        database: Database,
        *,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))

    def set(self, observation: StationFeeObservation) -> None:
        now = self._wall_clock()
        if now.tzinfo is None:
            raise ValueError("station-fee repository wall clock must be timezone-aware")
        future_boundary = now.astimezone(UTC) + DEFAULT_CLOCK_SKEW_TOLERANCE
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO station_fees (
                    region, city, station_type, displayed_fee, observed_at, provenance
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(region, city, station_type) DO UPDATE SET
                    displayed_fee=excluded.displayed_fee,
                    observed_at=excluded.observed_at,
                    provenance=excluded.provenance
                WHERE excluded.observed_at >= station_fees.observed_at
                   OR station_fees.observed_at > ?
                """,
                (
                    observation.region,
                    observation.city,
                    observation.station_type.value,
                    observation.displayed_fee,
                    _serialize_datetime(observation.observed_at),
                    observation.provenance.value,
                    _serialize_datetime(future_boundary),
                ),
            )

    def get(
        self,
        region: Region | str,
        city: str,
        station_type: StationType,
    ) -> StationFeeObservation | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM station_fees
                   WHERE region=? AND city=? AND station_type=?""",
                (_region_value(region), city, station_type.value),
            ).fetchone()
        return self._to_model(row) if row else None

    def remove(self, region: Region | str, city: str, station_type: StationType) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                """DELETE FROM station_fees
                   WHERE region=? AND city=? AND station_type=?""",
                (_region_value(region), city, station_type.value),
            )
            return cursor.rowcount > 0

    def list_all(self, region: Region | str | None = None) -> list[StationFeeObservation]:
        with self.database.connection() as connection:
            if region is None:
                rows = connection.execute(
                    "SELECT * FROM station_fees ORDER BY region, city, station_type"
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM station_fees WHERE region=?
                       ORDER BY city, station_type""",
                    (_region_value(region),),
                ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row) -> StationFeeObservation:
        observed_at = _parse_datetime(row["observed_at"])
        assert observed_at is not None
        return StationFeeObservation(
            region=row["region"],
            city=row["city"],
            station_type=StationType(row["station_type"]),
            displayed_fee=float(row["displayed_fee"]),
            observed_at=observed_at,
            provenance=Provenance(row["provenance"]),
        )


class CraftingProfileRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(
        self,
        profile: CraftingSkillProfile,
        profile_id: str = "default",
        *,
        name: str = "Default",
    ) -> None:
        if not profile_id.strip():
            raise ValueError("profile_id is required")
        now = datetime.now(UTC)
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO crafting_profiles (
                    profile_id, name, available_focus, complete_groups_json,
                    assume_zero_for_unspecified, updated_at, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    name=excluded.name,
                    available_focus=excluded.available_focus,
                    complete_groups_json=excluded.complete_groups_json,
                    assume_zero_for_unspecified=excluded.assume_zero_for_unspecified,
                    updated_at=excluded.updated_at,
                    provenance=excluded.provenance
                """,
                (
                    profile_id,
                    name,
                    profile.available_focus,
                    _json_groups(profile.complete_groups),
                    int(profile.assume_zero_for_unspecified),
                    _serialize_datetime(now),
                    Provenance.USER_PROFILE.value,
                ),
            )
            connection.execute(
                "DELETE FROM crafting_skill_levels WHERE profile_id=?", (profile_id,)
            )
            connection.execute(
                "DELETE FROM focus_efficiency_overrides WHERE profile_id=?", (profile_id,)
            )
            connection.executemany(
                """
                INSERT INTO crafting_skill_levels (
                    profile_id, skill_key, crafting_group, level,
                    mutual_fce_per_level, updated_at, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        profile_id,
                        value.skill_key,
                        value.crafting_group,
                        value.level,
                        value.mutual_fce_per_level,
                        _serialize_datetime(now),
                        value.provenance.value,
                    )
                    for value in profile.skill_levels
                ],
            )
            connection.executemany(
                """
                INSERT INTO focus_efficiency_overrides (
                    profile_id, mapping_key, focus_cost_efficiency, entered_at, provenance
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        profile_id,
                        value.mapping_key,
                        value.focus_cost_efficiency,
                        _serialize_datetime(value.entered_at),
                        value.provenance.value,
                    )
                    for value in profile.manual_fce_overrides
                ],
            )

    def load(self, profile_id: str = "default") -> CraftingSkillProfile | None:
        with self.database.connection() as connection:
            profile_row = connection.execute(
                "SELECT * FROM crafting_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if profile_row is None:
                return None
            skill_rows = connection.execute(
                """SELECT * FROM crafting_skill_levels WHERE profile_id=?
                   ORDER BY skill_key""",
                (profile_id,),
            ).fetchall()
            override_rows = connection.execute(
                """SELECT * FROM focus_efficiency_overrides WHERE profile_id=?
                   ORDER BY mapping_key""",
                (profile_id,),
            ).fetchall()

        skills = tuple(
            CraftingSkillLevel(
                skill_key=row["skill_key"],
                crafting_group=row["crafting_group"],
                level=row["level"],
                mutual_fce_per_level=float(row["mutual_fce_per_level"]),
                provenance=Provenance(row["provenance"]),
            )
            for row in skill_rows
        )
        overrides: list[ManualFocusEfficiencyOverride] = []
        for row in override_rows:
            entered_at = _parse_datetime(row["entered_at"])
            assert entered_at is not None
            overrides.append(
                ManualFocusEfficiencyOverride(
                    mapping_key=row["mapping_key"],
                    focus_cost_efficiency=float(row["focus_cost_efficiency"]),
                    entered_at=entered_at,
                    provenance=Provenance(row["provenance"]),
                )
            )
        return CraftingSkillProfile(
            available_focus=float(profile_row["available_focus"]),
            skill_levels=skills,
            manual_fce_overrides=tuple(overrides),
            complete_groups=frozenset(_parse_groups(profile_row["complete_groups_json"])),
            assume_zero_for_unspecified=bool(profile_row["assume_zero_for_unspecified"]),
        )

    def remove(self, profile_id: str = "default") -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM crafting_profiles WHERE profile_id=?", (profile_id,)
            )
            return cursor.rowcount > 0


def _json_groups(groups: Iterable[str]) -> str:
    import json

    return json.dumps(sorted(set(groups)), separators=(",", ":"))


def _parse_groups(payload: str) -> list[str]:
    import json

    decoded = json.loads(payload)
    if not isinstance(decoded, list) or any(not isinstance(value, str) for value in decoded):
        raise ValueError("crafting profile complete_groups_json is malformed")
    return decoded


@dataclass(frozen=True, slots=True)
class HistoryCoverage:
    region: Region
    item_id: str
    city: str
    quality: int
    time_scale: HistoryTimeScale
    window_start: datetime
    window_end: datetime
    fetched_at: datetime
    status: str
    record_count: int
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.item_id or not self.city or not self.status:
            raise ValueError("history coverage identity and status are required")
        if not 1 <= self.quality <= 5 or self.record_count < 0:
            raise ValueError("history coverage quality/count is invalid")
        if any(
            value.tzinfo is None for value in (self.window_start, self.window_end, self.fetched_at)
        ):
            raise ValueError("history coverage timestamps must be timezone-aware")
        if self.window_end < self.window_start:
            raise ValueError("history coverage window end precedes its start")


class MarketHistoryRepository:
    def __init__(
        self,
        database: Database,
        *,
        retention: timedelta = DEFAULT_HISTORY_RETENTION,
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("history retention must be positive")
        self.database = database
        self.retention = retention

    def upsert_many(self, records: Iterable[MarketHistoryInterval]) -> None:
        records = list(records)
        invalid = [
            value.provenance for value in records if not value.provenance.is_production_market_data
        ]
        if invalid:
            rejected = ", ".join(sorted({value.value for value in invalid}))
            raise ValueError(f"History cache accepts only AODP observations; rejected {rejected}")
        rows = [
            (
                value.region.value,
                value.item_id,
                value.city,
                value.quality,
                int(value.time_scale),
                _serialize_datetime(value.observed_at),
                value.item_count,
                value.average_price,
                None,
                None,
                _serialize_datetime(value.fetched_at),
                Provenance.AODP_CACHED.value,
            )
            for value in records
        ]
        if not rows:
            return
        with self.database.connection() as connection:
            connection.executemany(
                """
                INSERT INTO market_history_intervals (
                    region, item_id, city, quality, time_scale_hours, observed_at,
                    item_count, average_price, minimum_price, maximum_price,
                    fetched_at, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    region, item_id, city, quality, time_scale_hours, observed_at
                ) DO UPDATE SET
                    item_count=CASE WHEN excluded.fetched_at >=
                        market_history_intervals.fetched_at
                        THEN excluded.item_count ELSE market_history_intervals.item_count END,
                    average_price=CASE WHEN excluded.fetched_at >=
                        market_history_intervals.fetched_at
                        THEN excluded.average_price ELSE market_history_intervals.average_price END,
                    minimum_price=CASE WHEN excluded.fetched_at >=
                        market_history_intervals.fetched_at
                        THEN excluded.minimum_price ELSE market_history_intervals.minimum_price END,
                    maximum_price=CASE WHEN excluded.fetched_at >=
                        market_history_intervals.fetched_at
                        THEN excluded.maximum_price ELSE market_history_intervals.maximum_price END,
                    fetched_at=MAX(market_history_intervals.fetched_at, excluded.fetched_at),
                    provenance=excluded.provenance
                """,
                rows,
            )
            newest_fetch = max(value.fetched_at for value in records)
            connection.execute(
                "DELETE FROM market_history_intervals WHERE observed_at < ?",
                (_serialize_datetime(newest_fetch - self.retention),),
            )

    def list_for_items(
        self,
        region: Region,
        item_ids: Iterable[str],
        cities: Iterable[str],
        quality: int,
        since: datetime,
        *,
        time_scale: HistoryTimeScale = HistoryTimeScale.HOURLY,
    ) -> list[MarketHistoryInterval]:
        ids = tuple(dict.fromkeys(item_ids))
        city_values = tuple(dict.fromkeys(cities))
        if not ids or not city_values:
            return []
        fixed = 4 + len(city_values)
        chunk_size = max(1, 900 - fixed)
        rows = []
        with self.database.connection() as connection:
            for offset in range(0, len(ids), chunk_size):
                chunk = ids[offset : offset + chunk_size]
                rows.extend(
                    connection.execute(
                        f"""SELECT * FROM market_history_intervals
                            WHERE region=? AND quality=? AND time_scale_hours=?
                              AND observed_at>=?
                              AND city IN ({",".join("?" for _ in city_values)})
                              AND item_id IN ({",".join("?" for _ in chunk)})
                            ORDER BY item_id, city, observed_at""",  # noqa: S608
                        (
                            region.value,
                            quality,
                            int(time_scale),
                            _serialize_datetime(since),
                            *city_values,
                            *chunk,
                        ),
                    ).fetchall()
                )
        return [self._interval_from_row(row) for row in rows]

    def list_for_outputs(
        self,
        region: Region,
        item_ids: Iterable[str],
        cities: Iterable[str],
        quality: int,
        since: datetime,
        *,
        time_scale: HistoryTimeScale = HistoryTimeScale.HOURLY,
    ) -> list[MarketHistoryInterval]:
        """Backward-compatible output-oriented name for the generic history query."""

        return self.list_for_items(
            region,
            item_ids,
            cities,
            quality,
            since,
            time_scale=time_scale,
        )

    def set_coverage(self, coverage: HistoryCoverage) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO market_history_coverage (
                    region, item_id, city, quality, time_scale_hours,
                    window_start, window_end, fetched_at, status, record_count, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region, item_id, city, quality, time_scale_hours) DO UPDATE SET
                    window_start=excluded.window_start,
                    window_end=excluded.window_end,
                    fetched_at=excluded.fetched_at,
                    status=excluded.status,
                    record_count=excluded.record_count,
                    error_message=excluded.error_message
                WHERE excluded.fetched_at >= market_history_coverage.fetched_at
                """,
                (
                    coverage.region.value,
                    coverage.item_id,
                    coverage.city,
                    coverage.quality,
                    int(coverage.time_scale),
                    _serialize_datetime(coverage.window_start),
                    _serialize_datetime(coverage.window_end),
                    _serialize_datetime(coverage.fetched_at),
                    coverage.status,
                    coverage.record_count,
                    coverage.error_message,
                ),
            )

    def list_coverage(
        self,
        region: Region,
        item_ids: Iterable[str],
        cities: Iterable[str],
        quality: int,
        time_scale: HistoryTimeScale,
    ) -> list[HistoryCoverage]:
        ids = tuple(dict.fromkeys(item_ids))
        city_values = tuple(dict.fromkeys(cities))
        if not ids or not city_values:
            return []
        fixed = 3 + len(city_values)
        chunk_size = max(1, 900 - fixed)
        rows = []
        with self.database.connection() as connection:
            for offset in range(0, len(ids), chunk_size):
                chunk = ids[offset : offset + chunk_size]
                rows.extend(
                    connection.execute(
                        f"""SELECT * FROM market_history_coverage
                            WHERE region=? AND quality=? AND time_scale_hours=?
                              AND city IN ({",".join("?" for _ in city_values)})
                              AND item_id IN ({",".join("?" for _ in chunk)})
                            ORDER BY item_id, city""",  # noqa: S608
                        (
                            region.value,
                            quality,
                            int(time_scale),
                            *city_values,
                            *chunk,
                        ),
                    ).fetchall()
                )
        return [self._coverage_from_row(row) for row in rows]

    def prune_before(self, cutoff: datetime) -> int:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM market_history_intervals WHERE observed_at < ?",
                (_serialize_datetime(cutoff),),
            )
            return cursor.rowcount

    @staticmethod
    def _interval_from_row(row) -> MarketHistoryInterval:
        observed_at = _parse_datetime(row["observed_at"])
        fetched_at = _parse_datetime(row["fetched_at"])
        assert observed_at is not None and fetched_at is not None
        provenance = Provenance(row["provenance"])
        if provenance is not Provenance.AODP_CACHED:
            raise ValueError(f"Unexpected cached history provenance: {provenance.value}")
        return MarketHistoryInterval(
            region=Region(row["region"]),
            item_id=row["item_id"],
            city=row["city"],
            quality=row["quality"],
            time_scale=HistoryTimeScale(row["time_scale_hours"]),
            observed_at=observed_at,
            item_count=row["item_count"],
            average_price=row["average_price"],
            fetched_at=fetched_at,
            provenance=provenance,
        )

    @staticmethod
    def _coverage_from_row(row) -> HistoryCoverage:
        window_start = _parse_datetime(row["window_start"])
        window_end = _parse_datetime(row["window_end"])
        fetched_at = _parse_datetime(row["fetched_at"])
        assert window_start is not None and window_end is not None and fetched_at is not None
        return HistoryCoverage(
            region=Region(row["region"]),
            item_id=row["item_id"],
            city=row["city"],
            quality=row["quality"],
            time_scale=HistoryTimeScale(row["time_scale_hours"]),
            window_start=window_start,
            window_end=window_end,
            fetched_at=fetched_at,
            status=row["status"],
            record_count=row["record_count"],
            error_message=row["error_message"],
        )
