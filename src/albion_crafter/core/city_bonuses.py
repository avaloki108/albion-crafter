from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ActionKind, Item


CITY_BONUS_DATASET_VERSION = "albion-city-crafting-bonuses-2026-08-v2"
CITY_BONUS_VERIFIED_ON = "2026-08-18"
CITY_BONUS_SOURCE = "https://wiki.albiononline.com/wiki/Local_Production_Bonus"


class CityBonusClassification(StrEnum):
    VERIFIED_SPECIALTY = "verified_specialty"
    VERIFIED_BASELINE = "verified_baseline"
    UNKNOWN_CITY = "unknown_city"
    UNKNOWN_CRAFTING_GROUP = "unknown_crafting_group"
    UNSUPPORTED_CRAFTING_GROUP = "unsupported_crafting_group"


@dataclass(frozen=True, slots=True)
class CityCraftingBonus:
    city: str
    crafting_group: str
    bonus: float
    source: str = CITY_BONUS_SOURCE
    verified_on: str = CITY_BONUS_VERIFIED_ON
    dataset_version: str = CITY_BONUS_DATASET_VERSION

    def __post_init__(self) -> None:
        if not self.city or not self.crafting_group:
            raise ValueError("city bonus city and crafting group are required")
        if self.bonus < 0:
            raise ValueError("city crafting bonus cannot be negative")


@dataclass(frozen=True, slots=True)
class CityBonusResolution:
    city: str
    crafting_group: str
    classification: CityBonusClassification
    baseline_bonus: float | None
    specialty_bonus: float | None
    focus_bonus: float
    total_production_bonus: float | None
    dataset_version: str = CITY_BONUS_DATASET_VERSION
    source: str = CITY_BONUS_SOURCE
    verified_on: str = CITY_BONUS_VERIFIED_ON
    action_kind: ActionKind | None = None

    @property
    def is_verified(self) -> bool:
        return self.classification in {
            CityBonusClassification.VERIFIED_SPECIALTY,
            CityBonusClassification.VERIFIED_BASELINE,
        }

    @property
    def production_group(self) -> str:
        return self.crafting_group


CITY_CRAFTING_BASELINES: dict[str, float] = {
    "Bridgewatch": 0.18,
    "Brecilien": 0.18,
    "Caerleon": 0.18,
    "Fort Sterling": 0.18,
    "Lymhurst": 0.18,
    "Martlock": 0.18,
    "Thetford": 0.18,
}


def _bonuses(city: str, groups: tuple[str, ...], bonus: float = 0.15):
    return tuple(CityCraftingBonus(city, group, bonus) for group in groups)


CITY_CRAFTING_BONUSES: tuple[CityCraftingBonus, ...] = (
    *_bonuses(
        "Bridgewatch",
        ("crossbow", "dagger", "cursestaff", "plate_armor", "cloth_shoes"),
    ),
    *_bonuses(
        "Fort Sterling",
        ("hammer", "spear", "holystaff", "plate_helmet", "cloth_armor"),
    ),
    *_bonuses(
        "Lymhurst",
        ("sword", "bow", "arcanestaff", "leather_helmet", "leather_shoes"),
    ),
    *_bonuses(
        "Martlock",
        ("axe", "quarterstaff", "froststaff", "plate_shoes", "offhand"),
    ),
    *_bonuses(
        "Thetford",
        ("mace", "naturestaff", "firestaff", "leather_armor", "cloth_helmet"),
    ),
    *_bonuses(
        "Caerleon",
        ("gatherergear", "tools", "food", "knuckles", "shapeshifterstaff"),
    ),
    *_bonuses("Brecilien", ("cape", "bag", "potion")),
)

_BONUS_INDEX = {(entry.city, entry.crafting_group): entry for entry in CITY_CRAFTING_BONUSES}

# This allow-list is intentionally explicit. A new nonempty upstream value is
# unknown until reviewed; it must never silently inherit a verified baseline.
VERIFIED_CRAFTING_GROUPS = frozenset(
    {
        "arcanestaff",
        "axe",
        "bag",
        "bow",
        "cape",
        "cloth_armor",
        "cloth_helmet",
        "cloth_shoes",
        "crossbow",
        "cursestaff",
        "dagger",
        "firestaff",
        "food",
        "froststaff",
        "gatherergear",
        "hammer",
        "holystaff",
        "knuckles",
        "leather_armor",
        "leather_helmet",
        "leather_shoes",
        "mace",
        "naturestaff",
        "offhand",
        "plate_armor",
        "plate_helmet",
        "plate_shoes",
        "potion",
        "quarterstaff",
        "shapeshifterstaff",
        "spear",
        "sword",
        "tools",
    }
)
UNSUPPORTED_CRAFTING_GROUPS = frozenset(
    {
        "fiber",
        "hide",
        "meat_chicken",
        "meat_cow",
        "meat_goat",
        "meat_goose",
        "meat_pig",
        "meat_sheep",
        "ore",
        "rock",
        "wood",
    }
)


def resolve_city_crafting_bonus(
    item: Item,
    city: str,
    *,
    use_focus: bool,
    focus_production_bonus: float,
) -> CityBonusResolution:
    from .models import ActionKind

    group = item.crafting_category.casefold()
    focus_bonus = focus_production_bonus if use_focus else 0.0
    baseline = CITY_CRAFTING_BASELINES.get(city)
    if baseline is None:
        return CityBonusResolution(
            city,
            group,
            CityBonusClassification.UNKNOWN_CITY,
            None,
            None,
            focus_bonus,
            None,
            action_kind=ActionKind.CRAFT,
        )
    if group in UNSUPPORTED_CRAFTING_GROUPS:
        return CityBonusResolution(
            city,
            group,
            CityBonusClassification.UNSUPPORTED_CRAFTING_GROUP,
            baseline,
            None,
            focus_bonus,
            None,
            action_kind=ActionKind.CRAFT,
        )
    if not group or group not in VERIFIED_CRAFTING_GROUPS:
        return CityBonusResolution(
            city,
            group,
            CityBonusClassification.UNKNOWN_CRAFTING_GROUP,
            baseline,
            None,
            focus_bonus,
            None,
            action_kind=ActionKind.CRAFT,
        )
    specialty = _BONUS_INDEX.get((city, group))
    specialty_bonus = specialty.bonus if specialty is not None else 0.0
    classification = (
        CityBonusClassification.VERIFIED_SPECIALTY
        if specialty is not None
        else CityBonusClassification.VERIFIED_BASELINE
    )
    return CityBonusResolution(
        city,
        group,
        classification,
        baseline,
        specialty_bonus,
        focus_bonus,
        baseline + specialty_bonus + focus_bonus,
        action_kind=ActionKind.CRAFT,
    )
