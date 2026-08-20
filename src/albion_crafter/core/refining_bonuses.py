from __future__ import annotations

from dataclasses import dataclass

from .city_bonuses import CityBonusClassification, CityBonusResolution
from .models import REFINING_CATEGORIES, ActionKind, Item

REFINING_BONUS_DATASET_VERSION = "albion-city-refining-bonuses-2026-08-v1"
REFINING_BONUS_VERIFIED_ON = "2026-08-19"
REFINING_BONUS_SOURCE = "https://wiki.albiononline.com/wiki/Resource_return_rate"
REFINING_GUIDE_SOURCE = "https://albiononline.com/news/guide-refining"

ROYAL_REFINING_BASELINES: dict[str, float] = {
    "Bridgewatch": 0.18,
    "Brecilien": 0.18,
    "Caerleon": 0.18,
    "Fort Sterling": 0.18,
    "Lymhurst": 0.18,
    "Martlock": 0.18,
    "Thetford": 0.18,
}


@dataclass(frozen=True, slots=True)
class CityRefiningBonus:
    city: str
    refining_family: str
    bonus: float = 0.40
    source: str = REFINING_BONUS_SOURCE
    verified_on: str = REFINING_BONUS_VERIFIED_ON
    dataset_version: str = REFINING_BONUS_DATASET_VERSION


CITY_REFINING_BONUSES: tuple[CityRefiningBonus, ...] = (
    CityRefiningBonus("Thetford", "ore"),
    CityRefiningBonus("Fort Sterling", "wood"),
    CityRefiningBonus("Martlock", "hide"),
    CityRefiningBonus("Lymhurst", "fiber"),
    CityRefiningBonus("Bridgewatch", "rock"),
)
_REFINING_BONUS_INDEX = {
    (entry.city, entry.refining_family): entry for entry in CITY_REFINING_BONUSES
}


def resolve_city_refining_bonus(
    item: Item,
    city: str,
    *,
    use_focus: bool,
    focus_production_bonus: float,
) -> CityBonusResolution:
    family = item.crafting_category.casefold()
    focus_bonus = focus_production_bonus if use_focus else 0.0
    baseline = ROYAL_REFINING_BASELINES.get(city)
    common = {
        "dataset_version": REFINING_BONUS_DATASET_VERSION,
        "source": REFINING_BONUS_SOURCE,
        "verified_on": REFINING_BONUS_VERIFIED_ON,
        "action_kind": ActionKind.REFINE,
    }
    if baseline is None:
        return CityBonusResolution(
            city,
            family,
            CityBonusClassification.UNKNOWN_CITY,
            None,
            None,
            focus_bonus,
            None,
            **common,
        )
    if family not in REFINING_CATEGORIES:
        return CityBonusResolution(
            city,
            family,
            CityBonusClassification.UNKNOWN_CRAFTING_GROUP,
            baseline,
            None,
            focus_bonus,
            None,
            **common,
        )
    specialty = _REFINING_BONUS_INDEX.get((city, family))
    specialty_bonus = specialty.bonus if specialty is not None else 0.0
    classification = (
        CityBonusClassification.VERIFIED_SPECIALTY
        if specialty is not None
        else CityBonusClassification.VERIFIED_BASELINE
    )
    return CityBonusResolution(
        city,
        family,
        classification,
        baseline,
        specialty_bonus,
        focus_bonus,
        baseline + specialty_bonus + focus_bonus,
        **common,
    )
