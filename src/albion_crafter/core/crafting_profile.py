from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from .provenance import Provenance

if TYPE_CHECKING:
    from .models import Item, Recipe


CRAFTING_SKILL_MAPPING_VERSION = "albion-crafting-skills-2026-08-v2"
CRAFTING_SKILL_VERIFIED_ON = "2026-08-18"
CRAFTING_SKILL_SOURCE_REFERENCES = (
    "https://wiki.albiononline.com/wiki/Crafting",
    "https://wiki.albiononline.com/wiki/Specializations",
    "https://wiki.albiononline.com/wiki/Crafting_Focus",
)
REFINING_SKILL_MAPPING_VERSION = "albion-refining-skills-2026-08-v1"
REFINING_SKILL_VERIFIED_ON = "2026-08-19"
REFINING_SKILL_SOURCE_REFERENCES = (
    "https://wiki.albiononline.com/wiki/Specializations",
    "https://wiki.albiononline.com/wiki/Crafting_Focus",
)


class FocusEfficiencySource(StrEnum):
    DERIVED_PROFILE = "derived_profile"
    MANUAL_OVERRIDE = "manual_override"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CraftingSkillLevel:
    """One user-entered Destiny Board crafting node.

    ``mutual_fce_per_level`` is data attached to the node, rather than hidden
    in resolution code. This supports ordinary, artifact, and future nodes
    whose mutual contribution differs.
    """

    skill_key: str
    crafting_group: str
    level: int | None
    mutual_fce_per_level: float
    provenance: Provenance = Provenance.USER_PROFILE

    def __post_init__(self) -> None:
        if not self.skill_key.strip() or not self.crafting_group.strip():
            raise ValueError("crafting skill key and group are required")
        if self.level is not None and (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 0 <= self.level <= 100
        ):
            raise ValueError("crafting skill level must be an integer between 0 and 100")
        if not _finite_nonnegative(self.mutual_fce_per_level):
            raise ValueError("mutual FCE per level must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ManualFocusEfficiencyOverride:
    mapping_key: str
    focus_cost_efficiency: float
    entered_at: datetime
    provenance: Provenance = Provenance.USER_OVERRIDE

    def __post_init__(self) -> None:
        if not self.mapping_key.strip():
            raise ValueError("manual FCE override mapping key is required")
        if not _finite_nonnegative(self.focus_cost_efficiency):
            raise ValueError("manual FCE override must be finite and non-negative")
        if self.entered_at.tzinfo is None:
            raise ValueError("manual FCE override entered_at must be timezone-aware")
        if self.provenance is not Provenance.USER_OVERRIDE:
            raise ValueError("manual FCE overrides require USER_OVERRIDE provenance")


@dataclass(frozen=True, slots=True)
class CraftingSkillMapping:
    """Map one canonical item family to its applicable specialization node."""

    mapping_key: str
    crafting_group: str
    specialization_skill_key: str
    unique_fce_per_level: float
    verified: bool
    source_version: str = CRAFTING_SKILL_MAPPING_VERSION
    mastery_fce_per_level: float = 30.0
    mutual_fce_per_level: float = 30.0
    verified_on: str = CRAFTING_SKILL_VERIFIED_ON
    source_references: tuple[str, ...] = CRAFTING_SKILL_SOURCE_REFERENCES

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.mapping_key, self.crafting_group, self.specialization_skill_key)
        ):
            raise ValueError("crafting skill mapping identifiers are required")
        if not all(
            _finite_nonnegative(value)
            for value in (
                self.unique_fce_per_level,
                self.mastery_fce_per_level,
                self.mutual_fce_per_level,
            )
        ):
            raise ValueError("FCE coefficients must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class FocusEfficiencyResolution:
    mapping: CraftingSkillMapping | None
    focus_cost_efficiency: float | None
    source: FocusEfficiencySource
    provenance: Provenance
    missing_skill_keys: tuple[str, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.focus_cost_efficiency is not None


@dataclass(frozen=True, slots=True)
class CraftingSkillProfile:
    """Persistent item-specific crafting profile.

    Groups are incomplete by default. Marking a group complete explicitly
    means omitted nodes in that group are intentional zeroes. The separate
    ``assume_zero_for_unspecified`` switch implements the opt-in convenience
    planning mode without silently treating unknown skill levels as 0.
    """

    available_focus: float = 0.0
    skill_levels: tuple[CraftingSkillLevel, ...] = ()
    manual_fce_overrides: tuple[ManualFocusEfficiencyOverride, ...] = ()
    complete_groups: frozenset[str] = field(default_factory=frozenset)
    assume_zero_for_unspecified: bool = False

    def __post_init__(self) -> None:
        if not _finite_nonnegative(self.available_focus):
            raise ValueError("available Focus must be finite and non-negative")
        skill_keys = [value.skill_key for value in self.skill_levels]
        if len(skill_keys) != len(set(skill_keys)):
            raise ValueError("crafting skill keys must be unique")
        override_keys = [value.mapping_key for value in self.manual_fce_overrides]
        if len(override_keys) != len(set(override_keys)):
            raise ValueError("manual FCE override mapping keys must be unique")

    def resolve(self, mapping: CraftingSkillMapping | None) -> FocusEfficiencyResolution:
        if mapping is None:
            return FocusEfficiencyResolution(
                None,
                None,
                FocusEfficiencySource.UNKNOWN,
                Provenance.UNKNOWN,
            )

        override = next(
            (
                value
                for value in self.manual_fce_overrides
                if value.mapping_key == mapping.mapping_key
            ),
            None,
        )
        if override is not None:
            return FocusEfficiencyResolution(
                mapping,
                override.focus_cost_efficiency,
                FocusEfficiencySource.MANUAL_OVERRIDE,
                override.provenance,
            )

        if not mapping.verified:
            return FocusEfficiencyResolution(
                mapping,
                None,
                FocusEfficiencySource.UNKNOWN,
                Provenance.UNKNOWN,
                (mapping.specialization_skill_key,),
            )

        group_levels = tuple(
            value for value in self.skill_levels if value.crafting_group == mapping.crafting_group
        )
        own = next(
            (
                value
                for value in group_levels
                if value.skill_key == mapping.specialization_skill_key
            ),
            None,
        )
        group_is_complete = (
            mapping.crafting_group in self.complete_groups or self.assume_zero_for_unspecified
        )
        missing: list[str] = []
        if own is None or own.level is None:
            if not group_is_complete:
                missing.append(mapping.specialization_skill_key)
        if any(value.level is None for value in group_levels) and not group_is_complete:
            missing.extend(value.skill_key for value in group_levels if value.level is None)
        if not group_is_complete:
            missing.append(f"{mapping.crafting_group}:unreported_nodes")
        if missing:
            return FocusEfficiencyResolution(
                mapping,
                None,
                FocusEfficiencySource.UNKNOWN,
                Provenance.UNKNOWN,
                tuple(dict.fromkeys(missing)),
            )

        mutual = sum((value.level or 0) * value.mutual_fce_per_level for value in group_levels)
        own_level = own.level if own is not None and own.level is not None else 0
        return FocusEfficiencyResolution(
            mapping,
            mutual + own_level * mapping.unique_fce_per_level,
            FocusEfficiencySource.DERIVED_PROFILE,
            Provenance.USER_PROFILE,
        )


_VERIFIED_EQUIPMENT_SKILL_GROUPS = frozenset(
    {
        "arcanestaff",
        "axe",
        "bow",
        "cloth_armor",
        "cloth_helmet",
        "cloth_shoes",
        "crossbow",
        "cursestaff",
        "dagger",
        "firestaff",
        "froststaff",
        "hammer",
        "holystaff",
        "knuckles",
        "leather_armor",
        "leather_helmet",
        "leather_shoes",
        "mace",
        "naturestaff",
        "plate_armor",
        "plate_helmet",
        "plate_shoes",
        "quarterstaff",
        "shapeshifterstaff",
        "spear",
        "sword",
    }
)
_OFFHAND_SKILL_GROUPS = {
    "booktype": "tome",
    "shieldtype": "shield",
    "torchtype": "torch",
}
_TIER_PREFIX = re.compile(r"^T\d+_")


def crafting_skill_group_for_item(item: Item) -> str | None:
    category = item.crafting_category.casefold()
    if category == "offhand":
        return _OFFHAND_SKILL_GROUPS.get(item.subcategory.casefold())
    return category or None


def crafting_skill_mapping_for_item(item: Item) -> CraftingSkillMapping | None:
    """Derive a stable per-item mapping key from canonical static item identity.

    The unique 250 FCE contribution is verified here for equipment trees.
    Consumables, tools, gathering gear, and other exceptional trees remain
    explicitly unverified until their node coefficients are imported as data;
    callers may still use a provenance-bearing manual effective-FCE override.
    """

    group = crafting_skill_group_for_item(item)
    base_item_id = item.item_id.split("@", 1)[0]
    family = _TIER_PREFIX.sub("", base_item_id).casefold()
    if group is None or not family or family == base_item_id.casefold():
        return None
    skill_key = f"{group}:{family}"
    return CraftingSkillMapping(
        mapping_key=f"{group}/{family}",
        crafting_group=group,
        specialization_skill_key=skill_key,
        unique_fce_per_level=250.0,
        # Ordinary armor and weapon families use the verified 30 mutual/mastery
        # plus 250 unique coefficients. Off-hand and exceptional trees have
        # category-specific coefficients and remain manual-only until their
        # full node topology is imported rather than guessed.
        verified=group in _VERIFIED_EQUIPMENT_SKILL_GROUPS,
    )


def crafting_skill_mapping_for_recipe(recipe: Recipe) -> CraftingSkillMapping | None:
    """Resolve a recipe-safe mapping without guessing artifact-tree coefficients.

    The canonical output identity still supplies the stable mapping key used by
    manual overrides. Derived level-based FCE is enabled only for ordinary
    recipes whose every ingredient is explicitly returnable. A non-returnable
    artifact or unknown returnability may imply a different Destiny Board node,
    so the mapping remains addressable but is conservatively unverified.
    """

    from .models import ActionKind

    if recipe.action_kind is ActionKind.REFINE:
        return None
    mapping = crafting_skill_mapping_for_item(recipe.output)
    if mapping is None or all(material.returnable is True for material in recipe.materials):
        return mapping
    return replace(mapping, verified=False)


def refining_skill_mapping_for_item(item: Item) -> CraftingSkillMapping | None:
    """Map a T4-T8 refined resource to its family-and-tier Destiny Board node."""

    from .models import REFINING_CATEGORIES, ActionKind

    family = item.crafting_category.casefold()
    if item.action_kind is not ActionKind.REFINE or family not in REFINING_CATEGORIES:
        return None
    if item.tier is None or not 4 <= item.tier <= 8:
        return None
    group = f"refining:{family}"
    tier_key = f"{group}:t{item.tier}"
    return CraftingSkillMapping(
        mapping_key=f"refine/{family}/t{item.tier}",
        crafting_group=group,
        specialization_skill_key=tier_key,
        unique_fce_per_level=250.0,
        verified=True,
        source_version=REFINING_SKILL_MAPPING_VERSION,
        mastery_fce_per_level=30.0,
        mutual_fce_per_level=30.0,
        verified_on=REFINING_SKILL_VERIFIED_ON,
        source_references=REFINING_SKILL_SOURCE_REFERENCES,
    )


def refining_skill_mapping_for_recipe(recipe: Recipe) -> CraftingSkillMapping | None:
    return refining_skill_mapping_for_item(recipe.output)


def focus_skill_mapping_for_recipe(recipe: Recipe) -> CraftingSkillMapping | None:
    from .models import ActionKind

    if recipe.action_kind is ActionKind.REFINE:
        return refining_skill_mapping_for_recipe(recipe)
    return crafting_skill_mapping_for_recipe(recipe)


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )
