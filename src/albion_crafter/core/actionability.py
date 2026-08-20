from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActionabilityStatus(StrEnum):
    ACTIONABLE = "ACTIONABLE"
    NOT_ACTIONABLE = "NOT ACTIONABLE"


class ReasonSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


class ReasonCode(StrEnum):
    MISSING_MATERIAL_PRICE = "missing_material_price"
    MISSING_OUTPUT_PRICE = "missing_output_price"
    STALE_PRICE = "stale_price"
    FUTURE_TIMESTAMP = "future_timestamp"
    UNKNOWN_TIMESTAMP = "unknown_timestamp"
    UNTRUSTED_PROVENANCE = "untrusted_provenance"
    PROVISIONAL_MECHANICS = "provisional_mechanics"
    UNKNOWN_ITEM_VALUE = "unknown_item_value"
    UNKNOWN_RETURNABILITY = "unknown_returnability"
    AMBIGUOUS_RECIPE = "ambiguous_recipe"
    MISSING_FOCUS_COST = "missing_focus_cost"
    INSUFFICIENT_FOCUS = "insufficient_focus"
    UNKNOWN_STATION_FEE = "unknown_station_fee"
    STALE_STATION_FEE = "stale_station_fee"
    FUTURE_STATION_FEE_TIMESTAMP = "future_station_fee_timestamp"
    UNKNOWN_STATION_FEE_TIMESTAMP = "unknown_station_fee_timestamp"
    UNKNOWN_CRAFTING_SPECIALIZATION = "unknown_crafting_specialization"
    UNKNOWN_REFINING_SPECIALIZATION = "unknown_refining_specialization"
    UNKNOWN_CITY_BONUS_CLASSIFICATION = "unknown_city_bonus_classification"
    UNSUPPORTED_OUTPUT_QUALITY = "unsupported_output_quality"
    UNKNOWN_LIQUIDITY = "unknown_liquidity"
    LOW_LIQUIDITY = "low_liquidity"
    HISTORICAL_PRICE_ESTIMATE = "historical_price_estimate"
    TOP_OF_BOOK_DEPTH_UNMODELED = "top_of_book_depth_unmodeled"
    TRANSPORT_ASSUMPTION = "transport_assumption"


@dataclass(frozen=True, slots=True)
class ActionabilityReason:
    code: ReasonCode
    message: str
    severity: ReasonSeverity = ReasonSeverity.BLOCKING


@dataclass(frozen=True, slots=True)
class ActionabilityAssessment:
    reasons: tuple[ActionabilityReason, ...] = ()

    @property
    def status(self) -> ActionabilityStatus:
        return (
            ActionabilityStatus.ACTIONABLE
            if not any(reason.severity is ReasonSeverity.BLOCKING for reason in self.reasons)
            else ActionabilityStatus.NOT_ACTIONABLE
        )

    @property
    def is_actionable(self) -> bool:
        return not any(reason.severity is ReasonSeverity.BLOCKING for reason in self.reasons)

    @property
    def warnings(self) -> tuple[ActionabilityReason, ...]:
        return tuple(reason for reason in self.reasons if reason.severity is ReasonSeverity.WARNING)

    @property
    def blocking_reasons(self) -> tuple[ActionabilityReason, ...]:
        return tuple(
            reason for reason in self.reasons if reason.severity is ReasonSeverity.BLOCKING
        )

    def adding(self, *reasons: ActionabilityReason) -> ActionabilityAssessment:
        unique: dict[tuple[ReasonCode, str, ReasonSeverity], ActionabilityReason] = {
            (reason.code, reason.message, reason.severity): reason for reason in self.reasons
        }
        unique.update(
            {(reason.code, reason.message, reason.severity): reason for reason in reasons}
        )
        return ActionabilityAssessment(tuple(unique.values()))
