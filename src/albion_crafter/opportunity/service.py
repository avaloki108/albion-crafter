from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from albion_crafter.core.crafting_profile import CraftingSkillProfile
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import (
    MarketPriceRepository,
    PriceOverrideRepository,
)
from albion_crafter.database.v3 import (
    CraftingProfileRepository,
    MarketHistoryRepository,
    StationFeeRepository,
)
from albion_crafter.market.history import HistoryTimeScale

from .models import CancellationToken, ScanConstraints, ScanProgress, ScanSnapshot
from .scanner import OpportunityScanner


class OpportunityScannerService:
    """Bounded repository loader followed by the pure opportunity engine.

    The service intentionally performs no network refresh. It consumes the
    current/history cache exactly as it existed at ``as_of``; users refresh
    those caches through the explicit Market Data workflow.
    """

    def __init__(
        self,
        catalog: CatalogRepository,
        market_prices: MarketPriceRepository,
        overrides: PriceOverrideRepository,
        station_fees: StationFeeRepository,
        crafting_profiles: CraftingProfileRepository,
        history: MarketHistoryRepository | None = None,
        scanner: OpportunityScanner | None = None,
    ) -> None:
        self.catalog = catalog
        self.market_prices = market_prices
        self.overrides = overrides
        self.station_fees = station_fees
        self.crafting_profiles = crafting_profiles
        self.history = history
        self.scanner = scanner or OpportunityScanner()

    def scan(
        self,
        constraints: ScanConstraints,
        *,
        progress: Callable[[ScanProgress], None] | None = None,
        cancellation: CancellationToken | None = None,
        as_of: datetime | None = None,
    ) -> ScanSnapshot:
        scan_time = as_of or datetime.now(UTC)
        if scan_time.tzinfo is None:
            raise ValueError("scan as_of must be timezone-aware")
        recipes = ()
        prices = ()
        manual_prices = ()
        fees = ()
        profile = CraftingSkillProfile(available_focus=constraints.available_focus)
        history_by_key: dict[tuple, tuple] = {}
        history_status_by_key: dict[tuple, str] = {}
        price_history = ()
        database_reads = 0

        def cancelled_snapshot() -> ScanSnapshot:
            self._report(
                progress,
                "cancelled",
                "Scan cancelled during bounded data loading; no additional queries will run.",
            )
            return self.scanner.scan(
                recipes,
                prices,
                manual_prices,
                fees,
                profile,
                constraints,
                history_by_key=history_by_key,
                history_status_by_key=history_status_by_key,
                price_history=price_history,
                as_of=scan_time,
                progress=progress,
                cancellation=cancellation,
                database_load_operations=database_reads,
            )

        if self._is_cancelled(cancellation):
            return cancelled_snapshot()
        self._report(progress, "recipes", "Loading candidate recipes...")
        recipes = self.catalog.list_recipes(
            constraints.text,
            tier_min=constraints.tier_min,
            tier_max=constraints.tier_max,
            enchantments=constraints.enchantments,
            crafting_categories=constraints.crafting_categories,
        )
        database_reads += 2
        if self._is_cancelled(cancellation):
            return cancelled_snapshot()
        item_ids = tuple(
            dict.fromkeys(
                item_id
                for recipe in recipes
                for item_id in (
                    recipe.output.item_id,
                    *(material.item_id for material in recipe.materials),
                )
            )
        )
        output_ids = tuple(recipe.output.item_id for recipe in recipes)
        material_cities = (
            (constraints.material_city,)
            if constraints.material_city is not None
            else constraints.craft_cities
        )
        market_cities = tuple(
            dict.fromkeys((*material_cities, *constraints.craft_cities, *constraints.sell_cities))
        )
        qualities = tuple(dict.fromkeys((1, constraints.output_quality)))

        self._report(
            progress,
            "prices",
            f"Loading cached prices for {len(item_ids):,} distinct market IDs...",
        )
        prices = self.market_prices.list_for_scan(
            constraints.region,
            cities=market_cities,
            qualities=qualities,
            item_ids=item_ids,
        )
        database_reads += self._chunk_count(
            len(item_ids),
            fixed_parameters=1 + len(market_cities) + len(qualities),
        )
        if self._is_cancelled(cancellation):
            return cancelled_snapshot()
        manual_prices = self.overrides.list_for_scan(
            constraints.region,
            cities=market_cities,
            qualities=qualities,
            item_ids=item_ids,
        )
        database_reads += self._chunk_count(
            len(item_ids),
            fixed_parameters=1 + len(market_cities) + len(qualities),
        )
        if self._is_cancelled(cancellation):
            return cancelled_snapshot()

        self._report(progress, "scenario-data", "Loading station fees and crafting profile...")
        fees = self.station_fees.list_all(constraints.region)
        database_reads += 1
        if self._is_cancelled(cancellation):
            return cancelled_snapshot()
        stored_profile = self.crafting_profiles.load()
        database_reads += 1 if stored_profile is None else 3
        profile = stored_profile or profile
        if self._is_cancelled(cancellation):
            return cancelled_snapshot()

        if self.history is not None and item_ids:
            self._report(
                progress,
                "price-history",
                "Loading cached daily history for missing SELL-price fallback...",
            )
            daily_rows = self.history.list_for_items(
                constraints.region,
                item_ids,
                market_cities,
                1,
                scan_time - timedelta(days=30),
                time_scale=HistoryTimeScale.DAILY,
            )
            database_reads += self._chunk_count(
                len(item_ids),
                fixed_parameters=4 + len(market_cities),
            )
            if constraints.output_quality != 1 and output_ids:
                daily_rows.extend(
                    self.history.list_for_items(
                        constraints.region,
                        output_ids,
                        constraints.sell_cities,
                        constraints.output_quality,
                        scan_time - timedelta(days=30),
                        time_scale=HistoryTimeScale.DAILY,
                    )
                )
                database_reads += self._chunk_count(
                    len(output_ids),
                    fixed_parameters=4 + len(constraints.sell_cities),
                )
            price_history = tuple(daily_rows)
            if self._is_cancelled(cancellation):
                return cancelled_snapshot()

        if self.history is not None and output_ids:
            self._report(progress, "history", "Loading optional cached market history...")
            intervals = self.history.list_for_outputs(
                constraints.region,
                output_ids,
                constraints.sell_cities,
                constraints.output_quality,
                scan_time - timedelta(days=7),
                time_scale=HistoryTimeScale.SIX_HOURLY,
            )
            database_reads += self._chunk_count(
                len(output_ids),
                fixed_parameters=4 + len(constraints.sell_cities),
            )
            grouped: dict[tuple, list] = {}
            for interval in intervals:
                key = (
                    interval.region,
                    interval.item_id,
                    interval.city,
                    interval.quality,
                )
                grouped.setdefault(key, []).append(interval)
            history_by_key = {key: tuple(values) for key, values in grouped.items()}
            if self._is_cancelled(cancellation):
                return cancelled_snapshot()
            list_coverage = getattr(self.history, "list_coverage", None)
            if list_coverage is not None:
                coverage_rows = list_coverage(
                    constraints.region,
                    output_ids,
                    constraints.sell_cities,
                    constraints.output_quality,
                    HistoryTimeScale.SIX_HOURLY,
                )
                database_reads += self._chunk_count(
                    len(output_ids),
                    fixed_parameters=3 + len(constraints.sell_cities),
                )
                for coverage in coverage_rows:
                    coverage_is_complete = (
                        coverage.status in {"success", "empty"}
                        and coverage.window_start <= scan_time - timedelta(days=7)
                        and coverage.window_end
                        >= scan_time - timedelta(hours=int(HistoryTimeScale.SIX_HOURLY))
                    )
                    history_status_by_key[
                        (
                            coverage.region,
                            coverage.item_id,
                            coverage.city,
                            coverage.quality,
                        )
                    ] = coverage.status if coverage_is_complete else "partial"
            else:
                # Existing cached rows prove history was fetched, but an absent coverage
                # API cannot prove a successful empty response.
                history_status_by_key.update({key: "success" for key in history_by_key})
            if self._is_cancelled(cancellation):
                return cancelled_snapshot()

        self._report(
            progress,
            "evaluate",
            f"Evaluating {len(recipes):,} recipes from preloaded data...",
        )
        return self.scanner.scan(
            recipes,
            prices,
            manual_prices,
            fees,
            profile,
            constraints,
            history_by_key=history_by_key,
            history_status_by_key=history_status_by_key,
            price_history=price_history,
            as_of=scan_time,
            progress=progress,
            cancellation=cancellation,
            database_load_operations=database_reads,
        )

    @staticmethod
    def _is_cancelled(cancellation: CancellationToken | None) -> bool:
        return cancellation is not None and cancellation.is_cancelled

    @staticmethod
    def _chunk_count(item_count: int, *, fixed_parameters: int) -> int:
        if item_count <= 0:
            return 0
        chunk_size = max(1, 900 - fixed_parameters)
        return (item_count + chunk_size - 1) // chunk_size

    @staticmethod
    def _report(
        callback: Callable[[ScanProgress], None] | None,
        stage: str,
        message: str,
    ) -> None:
        if callback is not None:
            callback(ScanProgress(stage, message))
