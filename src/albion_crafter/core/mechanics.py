from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from .city_bonuses import (
    CITY_BONUS_DATASET_VERSION,
    CITY_CRAFTING_BASELINES,
    CITY_CRAFTING_BONUSES,
    UNSUPPORTED_CRAFTING_GROUPS,
    CityBonusResolution,
    resolve_city_crafting_bonus,
)
from .models import ActionKind, Item, SaleMethod
from .refining_bonuses import (
    REFINING_BONUS_DATASET_VERSION,
    resolve_city_refining_bonus,
)


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    PROVISIONAL = "provisional"


VERIFICATION_COMPONENT_NAMES = (
    "resource_return_rate",
    "crafting_city_bonuses",
    "refining_city_bonuses",
    "focus_production_bonus",
    "crafting_fce_mapping",
    "refining_fce_mapping",
    "marketplace_fees",
    "station_fee_formula",
)
DEFAULT_VERIFICATION_COMPONENTS = tuple(
    (name, VerificationStatus.VERIFIED) for name in VERIFICATION_COMPONENT_NAMES
)


@dataclass(frozen=True, slots=True)
class MechanicsVerificationHealth:
    ruleset_id: str
    verified_on: date
    checked_as_of: date
    age_days: int
    verification_status: VerificationStatus
    source_references: tuple[str, ...]
    warning_after_days: int
    component_statuses: tuple[tuple[str, VerificationStatus], ...] = ()

    @property
    def is_aging(self) -> bool:
        return self.age_days > self.warning_after_days

    @property
    def warning(self) -> str | None:
        if not self.is_aging:
            return None
        return f"Mechanics rules were last verified {self.age_days} days ago."


# Compatibility aliases. Resolution uses the versioned, status-bearing table in
# ``city_bonuses`` and never treats an arbitrary nonempty category as verified.
ROYAL_CITY_BASELINES = CITY_CRAFTING_BASELINES
ROYAL_CITY_SPECIALTIES: dict[str, frozenset[str]] = {
    city: frozenset(entry.crafting_group for entry in CITY_CRAFTING_BONUSES if entry.city == city)
    for city in CITY_CRAFTING_BASELINES
}
UNMODELED_CRAFTING_CATEGORIES = UNSUPPORTED_CRAFTING_GROUPS


@dataclass(frozen=True, slots=True)
class MechanicsRules:
    ruleset_id: str
    checked_on: str
    verification_status: VerificationStatus
    focus_production_bonus: float
    sell_order_setup_fee: float
    premium_transaction_tax: float
    non_premium_transaction_tax: float
    station_nutrition_factor: float
    royal_city_specialty_bonus: float
    source_references: tuple[str, ...] = ()
    verification_warning_after_days: int = 90
    component_statuses: tuple[tuple[str, VerificationStatus], ...] = DEFAULT_VERIFICATION_COMPONENTS

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.checked_on)
        except ValueError as exc:
            raise ValueError("checked_on must be an ISO date") from exc
        if self.verification_warning_after_days < 1:
            raise ValueError("verification warning age must be positive")
        component_names = tuple(name for name, _ in self.component_statuses)
        if len(component_names) != len(set(component_names)):
            raise ValueError("mechanics component names must be unique")
        if set(component_names) != set(VERIFICATION_COMPONENT_NAMES):
            raise ValueError("mechanics component statuses must cover the complete named set")
        if any(not isinstance(status, VerificationStatus) for _, status in self.component_statuses):
            raise ValueError("mechanics component statuses must use VerificationStatus")

    def verification_health(
        self,
        *,
        as_of: date | datetime | None = None,
    ) -> MechanicsVerificationHealth:
        checked_as_of = as_of or datetime.now(UTC)
        current_date = (
            checked_as_of.date() if isinstance(checked_as_of, datetime) else checked_as_of
        )
        verified_on = date.fromisoformat(self.checked_on)
        return MechanicsVerificationHealth(
            ruleset_id=self.ruleset_id,
            verified_on=verified_on,
            checked_as_of=current_date,
            age_days=max((current_date - verified_on).days, 0),
            verification_status=self.verification_status,
            source_references=self.source_references,
            warning_after_days=self.verification_warning_after_days,
            component_statuses=self.verification_components,
        )

    @property
    def verification_components(self) -> tuple[tuple[str, VerificationStatus], ...]:
        return self.component_statuses

    def component_status(self, name: str) -> VerificationStatus:
        try:
            return dict(self.component_statuses)[name]
        except KeyError as error:
            raise ValueError(f"unknown mechanics verification component {name!r}") from error

    @property
    def city_bonus_dataset_version(self) -> str:
        return CITY_BONUS_DATASET_VERSION

    def city_bonus(self, item: Item, craft_city: str, *, use_focus: bool) -> CityBonusResolution:
        return resolve_city_crafting_bonus(
            item,
            craft_city,
            use_focus=use_focus,
            focus_production_bonus=self.focus_production_bonus,
        )

    def production_bonus_resolution(
        self,
        action_kind: ActionKind,
        item: Item,
        production_city: str,
        *,
        use_focus: bool,
    ) -> CityBonusResolution:
        if action_kind is ActionKind.REFINE:
            return resolve_city_refining_bonus(
                item,
                production_city,
                use_focus=use_focus,
                focus_production_bonus=self.focus_production_bonus,
            )
        return self.city_bonus(item, production_city, use_focus=use_focus)

    def production_bonus(self, item: Item, craft_city: str, *, use_focus: bool) -> float | None:
        """Compatibility wrapper for V0.4 crafting-only callers.

        New production planning must call :meth:`production_bonus_resolution`
        with an explicit action kind.
        """

        return self.city_bonus(item, craft_city, use_focus=use_focus).total_production_bonus

    def production_bonus_dataset_version(self, action_kind: ActionKind) -> str:
        return (
            REFINING_BONUS_DATASET_VERSION
            if action_kind is ActionKind.REFINE
            else CITY_BONUS_DATASET_VERSION
        )

    def transaction_tax(self, *, premium: bool) -> float:
        return self.premium_transaction_tax if premium else self.non_premium_transaction_tax

    def total_market_fee_rate(self, *, premium: bool, sale_method: SaleMethod) -> float:
        rate = self.transaction_tax(premium=premium)
        if sale_method is SaleMethod.SELL_ORDER:
            rate += self.sell_order_setup_fee
        return rate


CURRENT_RULES = MechanicsRules(
    ruleset_id="albion-2026-08-crafting-refining-arbitrage-v4",
    checked_on="2026-08-19",
    verification_status=VerificationStatus.VERIFIED,
    focus_production_bonus=0.59,
    sell_order_setup_fee=0.025,
    premium_transaction_tax=0.04,
    non_premium_transaction_tax=0.08,
    station_nutrition_factor=0.1125,
    royal_city_specialty_bonus=0.15,
    source_references=(
        "https://wiki.albiononline.com/wiki/Resource_return_rate",
        "https://wiki.albiononline.com/wiki/Local_Production_Bonus",
        "https://wiki.albiononline.com/wiki/Crafting_Focus",
        "https://wiki.albiononline.com/wiki/Marketplace",
        "https://wiki.albiononline.com/wiki/Building",
        "https://albiononline.com/news/guide-refining",
        "https://wiki.albiononline.com/wiki/Specializations",
    ),
)
