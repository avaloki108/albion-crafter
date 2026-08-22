from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .models import Item


class LoadoutSlot(StrEnum):
    MAIN_HAND = "main_hand"
    OFF_HAND = "off_hand"
    HEAD = "head"
    CHEST = "chest"
    SHOES = "shoes"
    BAG = "bag"
    CAPE = "cape"
    MOUNT = "mount"
    POTION = "potion"
    FOOD = "food"


@dataclass(frozen=True, slots=True)
class LoadoutSlotSpec:
    label: str
    categories: tuple[str, ...]
    subcategories: tuple[str, ...] = ()
    contributes_to_average_ip: bool = False
    allows_quantity: bool = False


LOADOUT_SLOT_SPECS: dict[LoadoutSlot, LoadoutSlotSpec] = {
    LoadoutSlot.MAIN_HAND: LoadoutSlotSpec("Main Hand", ("weapons",), contributes_to_average_ip=True),
    LoadoutSlot.OFF_HAND: LoadoutSlotSpec("Off Hand", ("offhands",), contributes_to_average_ip=True),
    LoadoutSlot.HEAD: LoadoutSlotSpec("Head", ("head",), contributes_to_average_ip=True),
    LoadoutSlot.CHEST: LoadoutSlotSpec("Armor", ("armors",), contributes_to_average_ip=True),
    LoadoutSlot.SHOES: LoadoutSlotSpec("Shoes", ("shoes",), contributes_to_average_ip=True),
    LoadoutSlot.BAG: LoadoutSlotSpec("Bag", ("bags",)),
    LoadoutSlot.CAPE: LoadoutSlotSpec("Cape", ("capes",), contributes_to_average_ip=True),
    LoadoutSlot.MOUNT: LoadoutSlotSpec("Mount", ("mounts",)),
    LoadoutSlot.POTION: LoadoutSlotSpec(
        "Potion", ("consumables",), ("potions",), allows_quantity=True
    ),
    LoadoutSlot.FOOD: LoadoutSlotSpec(
        "Food", ("consumables",), ("food",), allows_quantity=True
    ),
}

QUALITY_NAMES: dict[int, str] = {
    1: "Normal",
    2: "Good",
    3: "Outstanding",
    4: "Excellent",
    5: "Masterpiece",
}
QUALITY_IP_BONUS: dict[int, int] = {1: 0, 2: 20, 3: 40, 4: 60, 5: 100}

LOADOUT_LOCATION_TAGS = (
    "Open World",
    "Static Dungeon",
    "Avalonian Dungeon",
    "Solo Dungeon",
    "Roads of Avalon",
    "Depths",
    "Hellgate",
    "Corrupted Dungeon",
    "Mists",
    "Knightfall Abbey",
    "Arena",
    "Other",
)
LOADOUT_ZONE_TAGS = ("Blue", "Yellow", "Orange", "Red", "Black")
LOADOUT_SIZE_TAGS = ("Solo", "Duo", "Trio", "Small Group", "Large Group", "Zerg")
LOADOUT_ROLE_TAGS = (
    "Tank",
    "Healer",
    "DPS",
    "Support",
    "Crowd Control",
    "Utility",
    "Other",
)
LOADOUT_ACTIVITY_TAGS = (
    "PvE Farm",
    "Tracking",
    "Ganking",
    "PvP",
    "Faction Warfare",
    "Territory",
    "Crystal League",
    "Crafting",
    "Gathering",
    "Transporting",
    "Exploration",
    "Ratting",
    "Other",
)
LOADOUT_BUDGET_TAGS = (
    "Newbie (<100k)",
    "Low (<300k)",
    "Medium (<2M)",
    "High (<5M)",
    "Gucci (>5M)",
)


def _clean_tags(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _optional_non_negative_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("item power must be a non-negative integer")
    parsed = int(value)
    if parsed < 0:
        raise ValueError("item power must be a non-negative integer")
    return parsed


@dataclass(frozen=True, slots=True)
class LoadoutSelection:
    item_id: str
    quality: int = 1
    quantity: int = 1
    observed_ip: int | None = None

    def __post_init__(self) -> None:
        clean_id = self.item_id.strip()
        if not clean_id:
            raise ValueError("loadout item_id is required")
        if isinstance(self.quality, bool) or not 1 <= self.quality <= 5:
            raise ValueError("loadout quality must be between 1 and 5")
        if isinstance(self.quantity, bool) or not 1 <= self.quantity <= 999:
            raise ValueError("loadout quantity must be between 1 and 999")
        if self.observed_ip is not None and (
            isinstance(self.observed_ip, bool) or self.observed_ip < 0
        ):
            raise ValueError("observed item power must be non-negative")
        object.__setattr__(self, "item_id", clean_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "quality": self.quality,
            "quantity": self.quantity,
            "observed_ip": self.observed_ip,
        }

    @classmethod
    def from_dict(cls, value: object) -> LoadoutSelection:
        if not isinstance(value, dict):
            raise ValueError("loadout selection must be an object")
        return cls(
            item_id=str(value.get("item_id", "")),
            quality=int(value.get("quality", 1)),
            quantity=int(value.get("quantity", 1)),
            observed_ip=_optional_non_negative_int(value.get("observed_ip")),
        )


@dataclass(frozen=True, slots=True)
class LoadoutSlotChoice:
    main: LoadoutSelection | None = None
    alternatives: tuple[LoadoutSelection, ...] = ()

    def __post_init__(self) -> None:
        if len(self.alternatives) > 2:
            raise ValueError("a loadout slot supports at most two swaps")

    def to_dict(self) -> dict[str, object]:
        return {
            "main": None if self.main is None else self.main.to_dict(),
            "alternatives": [selection.to_dict() for selection in self.alternatives],
        }

    @classmethod
    def from_dict(cls, value: object) -> LoadoutSlotChoice:
        if not isinstance(value, dict):
            raise ValueError("loadout slot choice must be an object")
        main_value = value.get("main")
        alternatives_value = value.get("alternatives", [])
        if not isinstance(alternatives_value, list):
            raise ValueError("loadout alternatives must be a list")
        return cls(
            main=None if main_value is None else LoadoutSelection.from_dict(main_value),
            alternatives=tuple(
                LoadoutSelection.from_dict(selection) for selection in alternatives_value[:2]
            ),
        )


@dataclass(frozen=True, slots=True)
class SavedLoadout:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "New build"
    author: str = ""
    location_tags: tuple[str, ...] = ()
    zone_tags: tuple[str, ...] = ()
    size_tags: tuple[str, ...] = ()
    role_tags: tuple[str, ...] = ()
    activity_tags: tuple[str, ...] = ()
    budget_tag: str | None = None
    strengths: str = ""
    weaknesses: str = ""
    description: str = ""
    rotation_notes: str = ""
    slots: dict[LoadoutSlot, LoadoutSlotChoice] = field(default_factory=dict)
    market_city: str = "Bridgewatch"
    price_side: str = "sell_order"
    target_ip: int | None = None
    last_known_cost: float | None = None
    last_missing_prices: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("loadout id is required")
        if not self.market_city.strip():
            raise ValueError("loadout market city is required")
        if self.price_side not in {"sell_order", "buy_order"}:
            raise ValueError("loadout price side must be sell_order or buy_order")
        if self.target_ip is not None and self.target_ip < 0:
            raise ValueError("target item power must be non-negative")
        if self.last_known_cost is not None and self.last_known_cost < 0:
            raise ValueError("last known cost must be non-negative")
        if self.last_missing_prices < 0:
            raise ValueError("last missing price count must be non-negative")
        if self.updated_at.tzinfo is None:
            raise ValueError("loadout updated_at must be timezone-aware")
        if any(not isinstance(slot, LoadoutSlot) for slot in self.slots):
            raise ValueError("loadout slots must use LoadoutSlot keys")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "location_tags": list(self.location_tags),
            "zone_tags": list(self.zone_tags),
            "size_tags": list(self.size_tags),
            "role_tags": list(self.role_tags),
            "activity_tags": list(self.activity_tags),
            "budget_tag": self.budget_tag,
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "description": self.description,
            "rotation_notes": self.rotation_notes,
            "slots": {slot.value: choice.to_dict() for slot, choice in self.slots.items()},
            "market_city": self.market_city,
            "price_side": self.price_side,
            "target_ip": self.target_ip,
            "last_known_cost": self.last_known_cost,
            "last_missing_prices": self.last_missing_prices,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: object) -> SavedLoadout:
        if not isinstance(value, dict):
            raise ValueError("saved loadout must be an object")
        raw_slots = value.get("slots", {})
        if not isinstance(raw_slots, dict):
            raise ValueError("saved loadout slots must be an object")
        slots: dict[LoadoutSlot, LoadoutSlotChoice] = {}
        for slot_name, choice_value in raw_slots.items():
            try:
                slot = LoadoutSlot(str(slot_name))
            except ValueError:
                continue
            choice = LoadoutSlotChoice.from_dict(choice_value)
            if choice.main is not None or choice.alternatives:
                slots[slot] = choice
        timestamp_value = value.get("updated_at")
        if timestamp_value:
            updated_at = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
        else:
            updated_at = datetime.now(UTC)
        budget = value.get("budget_tag")
        return cls(
            id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            author=str(value.get("author", "")),
            location_tags=_clean_tags(value.get("location_tags", [])),
            zone_tags=_clean_tags(value.get("zone_tags", [])),
            size_tags=_clean_tags(value.get("size_tags", [])),
            role_tags=_clean_tags(value.get("role_tags", [])),
            activity_tags=_clean_tags(value.get("activity_tags", [])),
            budget_tag=None if budget in (None, "") else str(budget),
            strengths=str(value.get("strengths", "")),
            weaknesses=str(value.get("weaknesses", "")),
            description=str(value.get("description", "")),
            rotation_notes=str(value.get("rotation_notes", "")),
            slots=slots,
            market_city=str(value.get("market_city", "Bridgewatch")),
            price_side=str(value.get("price_side", "sell_order")),
            target_ip=_optional_non_negative_int(value.get("target_ip")),
            last_known_cost=(
                None
                if value.get("last_known_cost") is None
                else float(value["last_known_cost"])
            ),
            last_missing_prices=int(value.get("last_missing_prices", 0)),
            updated_at=updated_at,
        )


def item_matches_loadout_slot(item: Item, slot: LoadoutSlot) -> bool:
    spec = LOADOUT_SLOT_SPECS[slot]
    category = item.category.casefold()
    subcategory = item.subcategory.casefold()
    return category in spec.categories and (
        not spec.subcategories or subcategory in spec.subcategories
    )


def is_two_handed(item: Item) -> bool:
    return item.category.casefold() == "weapons" and "2H" in item.item_id.upper().split("_")


def estimated_base_item_power(item: Item) -> int | None:
    if item.tier is None or not any(
        item_matches_loadout_slot(item, slot) for slot in LOADOUT_SLOT_SPECS
    ):
        return None
    return (item.tier + 3 + item.enchantment) * 100


def selection_item_power(selection: LoadoutSelection | None, item: Item | None) -> int | None:
    if selection is None:
        return None
    if selection.observed_ip is not None:
        return selection.observed_ip
    if item is None:
        return None
    base = estimated_base_item_power(item)
    return None if base is None else base + QUALITY_IP_BONUS[selection.quality]


@dataclass(frozen=True, slots=True)
class LoadoutIpSummary:
    average_ip: float | None
    combat_slots_filled: int


def calculate_loadout_ip(
    loadout: SavedLoadout,
    items_by_id: dict[str, Item],
) -> LoadoutIpSummary:
    main_selection = loadout.slots.get(LoadoutSlot.MAIN_HAND, LoadoutSlotChoice()).main
    main_item = items_by_id.get(main_selection.item_id) if main_selection else None
    main_ip = selection_item_power(main_selection, main_item) or 0
    two_handed = main_item is not None and is_two_handed(main_item)
    off_selection = loadout.slots.get(LoadoutSlot.OFF_HAND, LoadoutSlotChoice()).main
    off_ip = (
        main_ip
        if two_handed
        else selection_item_power(
            off_selection,
            items_by_id.get(off_selection.item_id) if off_selection else None,
        )
        or 0
    )
    total = main_ip + off_ip
    filled = 2 if main_selection is not None and two_handed else int(main_selection is not None)
    if not two_handed and off_selection is not None:
        filled += 1
    for slot in (LoadoutSlot.HEAD, LoadoutSlot.CHEST, LoadoutSlot.SHOES, LoadoutSlot.CAPE):
        selection = loadout.slots.get(slot, LoadoutSlotChoice()).main
        total += selection_item_power(
            selection,
            items_by_id.get(selection.item_id) if selection else None,
        ) or 0
        filled += int(selection is not None)
    return LoadoutIpSummary(None if filled == 0 else total / 6.0, filled)


def loadout_validation_issues(loadout: SavedLoadout) -> tuple[str, ...]:
    issues: list[str] = []
    if not loadout.name.strip():
        issues.append("Build name is required")
    if not any(choice.main is not None for choice in loadout.slots.values()):
        issues.append("Select at least one main item")
    return tuple(issues)


def loadout_summary_text(
    loadout: SavedLoadout,
    items_by_id: dict[str, Item],
    *,
    average_ip: float | None = None,
    known_cost: float | None = None,
    missing_prices: int = 0,
) -> str:
    lines = [loadout.name.strip() or "Unnamed build"]
    if loadout.author.strip():
        lines.append(f"By {loadout.author.strip()}")
    for slot, spec in LOADOUT_SLOT_SPECS.items():
        choice = loadout.slots.get(slot)
        if choice is None or choice.main is None:
            continue
        selection = choice.main
        item = items_by_id.get(selection.item_id)
        label = item.display_name if item else selection.item_id
        suffix = f" x{selection.quantity}" if selection.quantity != 1 else ""
        lines.append(f"{spec.label}: {label} [{selection.item_id}]{suffix}")
        if choice.alternatives:
            swap_labels = [
                (items_by_id.get(value.item_id).display_name if items_by_id.get(value.item_id) else value.item_id)
                for value in choice.alternatives
            ]
            lines.append(f"  Swaps: {', '.join(swap_labels)}")
    if average_ip is not None:
        lines.append(f"Estimated average IP: {average_ip:,.0f}")
    if known_cost is not None:
        completeness = "complete" if missing_prices == 0 else f"{missing_prices} missing"
        lines.append(f"Cached cost in {loadout.market_city}: {known_cost:,.0f} silver ({completeness})")
    if loadout.description.strip():
        lines.extend(("", loadout.description.strip()))
    if loadout.rotation_notes.strip():
        lines.extend(("", f"Rotation: {loadout.rotation_notes.strip()}"))
    return "\n".join(lines)


def loadout_from_fields(**values: Any) -> SavedLoadout:
    """Typed construction hook used by the Qt editor and tests."""

    return SavedLoadout(**values)
