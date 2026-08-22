from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from albion_crafter.core.freshness import FreshnessPolicy
from albion_crafter.core.loadouts import (
    LoadoutIpSummary,
    LoadoutSelection,
    LoadoutSlot,
    LoadoutSlotChoice,
    SavedLoadout,
    calculate_loadout_ip,
    is_two_handed,
    item_matches_loadout_slot,
)
from albion_crafter.core.models import Item
from albion_crafter.database.catalog import CatalogRepository
from albion_crafter.database.database import MarketPriceRepository

from .aodp import AODPClient, BatchFetchResult, BatchProgress, CancellationCheck
from .cache import CachedMarketService
from .models import MarketSide, Region
from .pricing import PriceResolver, ResolvedPrice


@dataclass(frozen=True, slots=True)
class LoadoutPriceLine:
    slot: LoadoutSlot
    selection: LoadoutSelection
    item: Item
    resolved_price: ResolvedPrice

    @property
    def line_total(self) -> float | None:
        price = self.resolved_price.price
        return None if price is None else price * self.selection.quantity


@dataclass(frozen=True, slots=True)
class LoadoutEvaluation:
    lines: tuple[LoadoutPriceLine, ...]
    ip: LoadoutIpSummary
    known_total: float
    selected_count: int
    missing_count: int
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.selected_count > 0 and self.missing_count == 0


class LoadoutEvaluationService:
    """Evaluate selected build items from saved evidence without network access."""

    def __init__(self, catalog: CatalogRepository, resolver: PriceResolver) -> None:
        self.catalog = catalog
        self.resolver = resolver

    def evaluate(
        self,
        loadout: SavedLoadout,
        *,
        region: Region,
        freshness_policy: FreshnessPolicy,
        as_of: datetime | None = None,
    ) -> LoadoutEvaluation:
        resolution_time = as_of or datetime.now(UTC)
        item_ids = {
            selection.item_id
            for choice in loadout.slots.values()
            for selection in ((choice.main,) if choice.main is not None else ())
        }
        items_by_id = {item.item_id: item for item in self.catalog.list_items(item_ids)}
        warnings: list[str] = []
        main = loadout.slots.get(LoadoutSlot.MAIN_HAND, LoadoutSlotChoice()).main
        main_item = items_by_id.get(main.item_id) if main else None
        two_handed = main_item is not None and is_two_handed(main_item)
        lines: list[LoadoutPriceLine] = []
        for slot in LoadoutSlot:
            choice = loadout.slots.get(slot)
            selection = choice.main if choice else None
            if selection is None:
                continue
            item = items_by_id.get(selection.item_id)
            if item is None:
                warnings.append(f"{selection.item_id} is no longer in the installed catalog.")
                continue
            if not item_matches_loadout_slot(item, slot):
                warnings.append(f"{item.display_name} no longer matches {slot.value}.")
                continue
            if slot is LoadoutSlot.OFF_HAND and two_handed:
                warnings.append("Off Hand is ignored because Main Hand is two-handed.")
                continue
            quality = min(selection.quality, item.max_quality or 1)
            normalized = (
                selection
                if quality == selection.quality
                else LoadoutSelection(
                    selection.item_id,
                    quality,
                    selection.quantity,
                    selection.observed_ip,
                )
            )
            resolved = self.resolver.resolve_item(
                item.item_id,
                city=loadout.market_city,
                quality=quality,
                region=region,
                side=MarketSide(loadout.price_side),
                freshness_policy=freshness_policy,
                as_of=resolution_time,
                role=f"loadout:{slot.value}",
            )
            lines.append(LoadoutPriceLine(slot, normalized, item, resolved))
        known_total = sum(line.line_total or 0.0 for line in lines)
        missing_count = sum(line.line_total is None for line in lines)
        return LoadoutEvaluation(
            lines=tuple(lines),
            ip=calculate_loadout_ip(loadout, items_by_id),
            known_total=known_total,
            selected_count=len(lines),
            missing_count=missing_count,
            warnings=tuple(warnings),
        )


@dataclass(frozen=True, slots=True)
class LoadoutPriceRefreshRequest:
    region: Region
    city: str
    selections: tuple[LoadoutSelection, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.region, Region):
            raise ValueError("loadout refresh region must be a Region")
        if not self.city.strip():
            raise ValueError("loadout refresh city is required")
        if not self.selections:
            raise ValueError("add at least one item before refreshing")


class LoadoutPriceRefreshService:
    """Refresh only the canonical IDs and qualities selected in one build."""

    def __init__(
        self,
        repository: MarketPriceRepository,
        *,
        client_factory: Callable[[Region], AODPClient] = AODPClient,
    ) -> None:
        self.repository = repository
        self.client_factory = client_factory

    def refresh(
        self,
        request: LoadoutPriceRefreshRequest,
        *,
        is_cancelled: CancellationCheck | None = None,
        on_progress: Callable[[BatchProgress], None] | None = None,
    ) -> BatchFetchResult:
        item_ids = tuple(dict.fromkeys(value.item_id for value in request.selections))
        qualities = tuple(dict.fromkeys(value.quality for value in request.selections))
        return CachedMarketService(
            self.client_factory(request.region), self.repository
        ).refresh(
            item_ids,
            cities=(request.city,),
            qualities=qualities,
            is_cancelled=is_cancelled,
            on_progress=on_progress,
        )
