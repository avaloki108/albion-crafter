from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from .freshness import Freshness, FreshnessPolicy
from .provenance import Provenance

if TYPE_CHECKING:
    from .models import Item


class StationType(StrEnum):
    """Crafting-station identifiers used by persisted fee observations."""

    WARRIOR_FORGE = "warrior_forge"
    HUNTER_LODGE = "hunter_lodge"
    MAGE_TOWER = "mage_tower"
    TOOLMAKER = "toolmaker"
    COOK = "cook"
    ALCHEMIST_LAB = "alchemist_lab"
    MILL = "mill"
    BUTCHER = "butcher"
    SMELTER = "smelter"
    LUMBERMILL = "lumbermill"
    TANNER = "tanner"
    WEAVER = "weaver"
    STONEMASON = "stonemason"

    # Readable aliases matching the possessive in Albion's display names.
    WARRIORS_FORGE = "warrior_forge"
    HUNTERS_LODGE = "hunter_lodge"
    MAGES_TOWER = "mage_tower"
    ALCHEMISTS_LAB = "alchemist_lab"

    @property
    def display_name(self) -> str:
        return {
            StationType.WARRIOR_FORGE: "Warrior's Forge",
            StationType.HUNTER_LODGE: "Hunter's Lodge",
            StationType.MAGE_TOWER: "Mage's Tower",
            StationType.TOOLMAKER: "Toolmaker",
            StationType.COOK: "Cook",
            StationType.ALCHEMIST_LAB: "Alchemist's Lab",
            StationType.MILL: "Mill",
            StationType.BUTCHER: "Butcher",
            StationType.SMELTER: "Smelter",
            StationType.LUMBERMILL: "Lumbermill",
            StationType.TANNER: "Tanner",
            StationType.WEAVER: "Weaver",
            StationType.STONEMASON: "Stonemason",
        }[self]


@dataclass(frozen=True, slots=True)
class StationFeeObservation:
    """One observed in-game station usage fee.

    ``displayed_fee`` deliberately uses the number shown by Albion. A displayed
    value of 500 is stored as 500; conversion belongs to the mechanics formula.
    """

    region: str
    city: str
    station_type: StationType
    displayed_fee: float
    observed_at: datetime
    provenance: Provenance = Provenance.USER_OVERRIDE

    def __post_init__(self) -> None:
        if not self.region.strip() or not self.city.strip():
            raise ValueError("station-fee region and city are required")
        if (
            isinstance(self.displayed_fee, bool)
            or not isinstance(self.displayed_fee, (int, float))
            or not math.isfinite(self.displayed_fee)
            or self.displayed_fee < 0
        ):
            raise ValueError("displayed station fee must be finite and non-negative")
        if self.observed_at.tzinfo is None:
            raise ValueError("station-fee observed_at must be timezone-aware")
        if self.provenance in {
            Provenance.UNKNOWN,
            Provenance.DEMO_SAMPLE,
            Provenance.TEST_FIXTURE,
        }:
            raise ValueError("production station-fee observations need trusted provenance")

    @property
    def key(self) -> tuple[str, str, StationType]:
        return (self.region, self.city, self.station_type)


@dataclass(frozen=True, slots=True)
class StationFeeResolution:
    region: str
    city: str
    station_type: StationType | None
    observation: StationFeeObservation | None
    freshness: Freshness = Freshness.UNKNOWN

    @property
    def is_known(self) -> bool:
        return self.station_type is not None and self.observation is not None

    @property
    def displayed_fee(self) -> float | None:
        return self.observation.displayed_fee if self.observation is not None else None

    @property
    def provenance(self) -> Provenance:
        return self.observation.provenance if self.observation is not None else Provenance.UNKNOWN

    @property
    def observed_at(self) -> datetime | None:
        return self.observation.observed_at if self.observation is not None else None

    @property
    def is_fresh_enough(self) -> bool:
        return self.is_known and self.freshness in {Freshness.FRESH, Freshness.AGING}


_WARRIOR_CATEGORIES = frozenset(
    {
        "axe",
        "crossbow",
        "hammer",
        "knuckles",
        "mace",
        "plate_armor",
        "plate_helmet",
        "plate_shoes",
        "sword",
    }
)
_HUNTER_CATEGORIES = frozenset(
    {
        "bow",
        "dagger",
        "leather_armor",
        "leather_helmet",
        "leather_shoes",
        "naturestaff",
        "quarterstaff",
        "shapeshifterstaff",
        "spear",
    }
)
_MAGE_CATEGORIES = frozenset(
    {
        "arcanestaff",
        "cloth_armor",
        "cloth_helmet",
        "cloth_shoes",
        "cursestaff",
        "firestaff",
        "froststaff",
        "holystaff",
    }
)
_TOOLMAKER_CATEGORIES = frozenset({"bag", "cape", "gatherergear", "tools"})
_MEAT_CATEGORIES = frozenset(
    {
        "meat_chicken",
        "meat_cow",
        "meat_goat",
        "meat_goose",
        "meat_pig",
        "meat_sheep",
    }
)


def station_type_for_item(item: Item) -> StationType | None:
    """Return a verified station classification, or ``None`` rather than guess."""

    category = item.crafting_category.casefold()
    refining = {
        "ore": StationType.SMELTER,
        "wood": StationType.LUMBERMILL,
        "hide": StationType.TANNER,
        "fiber": StationType.WEAVER,
        "rock": StationType.STONEMASON,
    }
    if category in refining:
        return refining[category]
    if category in _WARRIOR_CATEGORIES:
        return StationType.WARRIORS_FORGE
    if category in _HUNTER_CATEGORIES:
        return StationType.HUNTERS_LODGE
    if category in _MAGE_CATEGORIES:
        return StationType.MAGES_TOWER
    if category in _TOOLMAKER_CATEGORIES:
        return StationType.TOOLMAKER
    if category == "food":
        upper_id = item.item_id.split("@", 1)[0].upper()
        if upper_id.endswith(("_FLOUR", "_BUTTER")):
            return StationType.MILL
        if upper_id.endswith("_ALCOHOL"):
            return StationType.ALCHEMIST_LAB
        return StationType.COOK
    if category == "potion":
        return StationType.ALCHEMISTS_LAB
    if category in _MEAT_CATEGORIES:
        return StationType.BUTCHER
    if category == "offhand":
        return {
            "shieldtype": StationType.WARRIORS_FORGE,
            "torchtype": StationType.HUNTERS_LODGE,
            "booktype": StationType.MAGES_TOWER,
        }.get(item.subcategory.casefold())
    return None


def resolve_station_fee(
    item: Item,
    *,
    region: str,
    city: str,
    observations: Iterable[StationFeeObservation],
    freshness_policy: FreshnessPolicy | None = None,
    as_of: datetime | None = None,
) -> StationFeeResolution:
    station_type = station_type_for_item(item)
    if station_type is None:
        return StationFeeResolution(region, city, None, None)
    key = (region, city, station_type)
    matches = (observation for observation in observations if observation.key == key)
    observation = max(matches, key=lambda value: value.observed_at, default=None)
    freshness = (
        freshness_policy.classify(
            observation.observed_at if observation is not None else None,
            now=as_of,
        )
        if freshness_policy is not None
        else Freshness.UNKNOWN
    )
    return StationFeeResolution(region, city, station_type, observation, freshness)
