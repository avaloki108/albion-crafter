from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from albion_crafter.core.freshness import future_offset_beyond_tolerance
from albion_crafter.database.database import MarketPriceRepository

from .models import MarketPrice, Region


@dataclass(frozen=True, slots=True)
class CityMarketCoverage:
    city: str
    expected_rows: int
    market_rows: int
    observed_within_2h: int
    observed_within_4h: int
    observed_within_24h: int
    observed_older_than_24h: int
    no_usable_price: int

    @property
    def coverage_4h_percent(self) -> float:
        if not self.expected_rows:
            return 0.0
        return 100 * self.observed_within_4h / self.expected_rows

    @property
    def cached_rows(self) -> int:
        return self.market_rows


@dataclass(frozen=True, slots=True)
class MarketCoverageSummary:
    region: Region
    cities: tuple[str, ...]
    item_ids: tuple[str, ...]
    as_of: datetime
    expected_rows: int
    market_rows: int
    observed_within_2h: int
    observed_within_4h: int
    observed_within_24h: int
    observed_older_than_24h: int
    no_usable_price: int
    oldest_observation_at: datetime | None
    newest_observation_at: datetime | None
    per_city: tuple[CityMarketCoverage, ...]
    observations_le_2h: int
    observations_le_4h: int
    observations_le_24h: int
    observations_older_24h: int
    missing_sides: int

    @property
    def item_count(self) -> int:
        return len(self.item_ids)

    @property
    def city_count(self) -> int:
        return len(self.cities)

    @property
    def coverage_24h_percent(self) -> float:
        if not self.expected_rows:
            return 0.0
        return 100 * self.observed_within_24h / self.expected_rows

    @property
    def cached_rows(self) -> int:
        return self.market_rows

    @property
    def rows_with_no_usable_price(self) -> int:
        return self.no_usable_price

    @property
    def city_coverage(self) -> tuple[CityMarketCoverage, ...]:
        return self.per_city


class MarketCoverageService:
    """Calculate timestamp-honest cache coverage outside any desktop UI."""

    def __init__(self, repository: MarketPriceRepository) -> None:
        self.repository = repository

    def summary(
        self,
        region: Region,
        cities: tuple[str, ...],
        item_ids: tuple[str, ...],
        as_of: datetime,
        *,
        quality: int = 1,
    ) -> MarketCoverageSummary:
        if as_of.tzinfo is None:
            raise ValueError("market coverage as_of must be timezone-aware")
        if not 1 <= quality <= 5:
            raise ValueError("market coverage quality must be between 1 and 5")
        selected_cities = _deduplicate(cities, label="city")
        selected_items = _deduplicate(item_ids, label="item ID")
        current_time = as_of.astimezone(UTC)
        rows = self.repository.list_for_scan(
            region,
            cities=selected_cities,
            qualities=(quality,),
            item_ids=selected_items,
        )
        index = {
            (_fold(row.item_id), _fold_city(row.city)): row
            for row in rows
            if row.quality == quality
        }
        city_counts: list[CityMarketCoverage] = []
        all_observations: list[datetime] = []
        totals = [0, 0, 0, 0, 0]
        side_totals = [0, 0, 0, 0, 0]
        for city in selected_cities:
            counts = [0, 0, 0, 0, 0]
            market_rows = 0
            for item_id in selected_items:
                row = index.get((_fold(item_id), _fold_city(city)))
                if row is not None:
                    market_rows += 1
                side_observations = _usable_observation_timestamps(row, current_time)
                side_totals[4] += 2 - len(side_observations)
                for side_observed_at in side_observations:
                    all_observations.append(side_observed_at)
                    side_age = max(current_time - side_observed_at, timedelta(0))
                    if side_age <= timedelta(hours=2):
                        side_totals[0] += 1
                    if side_age <= timedelta(hours=4):
                        side_totals[1] += 1
                    if side_age <= timedelta(hours=24):
                        side_totals[2] += 1
                    else:
                        side_totals[3] += 1
                observed_at = max(side_observations, default=None)
                if observed_at is None:
                    counts[4] += 1
                    continue
                age = max(current_time - observed_at, timedelta(0))
                if age <= timedelta(hours=2):
                    counts[0] += 1
                if age <= timedelta(hours=4):
                    counts[1] += 1
                if age <= timedelta(hours=24):
                    counts[2] += 1
                else:
                    counts[3] += 1
            totals = [left + right for left, right in zip(totals, counts, strict=True)]
            city_counts.append(
                CityMarketCoverage(
                    city=city,
                    expected_rows=len(selected_items),
                    market_rows=market_rows,
                    observed_within_2h=counts[0],
                    observed_within_4h=counts[1],
                    observed_within_24h=counts[2],
                    observed_older_than_24h=counts[3],
                    no_usable_price=counts[4],
                )
            )
        return MarketCoverageSummary(
            region=region,
            cities=selected_cities,
            item_ids=selected_items,
            as_of=current_time,
            expected_rows=len(selected_items) * len(selected_cities),
            market_rows=len(index),
            observed_within_2h=totals[0],
            observed_within_4h=totals[1],
            observed_within_24h=totals[2],
            observed_older_than_24h=totals[3],
            no_usable_price=totals[4],
            oldest_observation_at=min(all_observations, default=None),
            newest_observation_at=max(all_observations, default=None),
            per_city=tuple(city_counts),
            observations_le_2h=side_totals[0],
            observations_le_4h=side_totals[1],
            observations_le_24h=side_totals[2],
            observations_older_24h=side_totals[3],
            missing_sides=side_totals[4],
        )


def _usable_observation_timestamps(
    row: MarketPrice | None,
    as_of: datetime,
) -> tuple[datetime, ...]:
    if row is None:
        return ()
    return tuple(
        timestamp.astimezone(UTC)
        for price, timestamp in (
            (row.sell_price, row.sell_price_timestamp),
            (row.buy_price, row.buy_price_timestamp),
        )
        if price is not None
        and price > 0
        and timestamp is not None
        and not future_offset_beyond_tolerance(timestamp, now=as_of)
    )


def _deduplicate(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"market coverage {label}s must be non-empty strings")
        clean = value.strip()
        key = _fold_city(clean) if label == "city" else _fold(clean)
        unique.setdefault(key, clean)
    return tuple(unique.values())


def _fold(value: str) -> str:
    return value.casefold()


def _fold_city(value: str) -> str:
    return value.replace(" ", "").replace("'", "").casefold()
