from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from albion_crafter.core.arbitrage import calculate_arbitrage_economics
from albion_crafter.core.calculator import CraftCalculator
from albion_crafter.core.crafting_profile import focus_skill_mapping_for_recipe
from albion_crafter.core.freshness import Freshness, FreshnessPolicy
from albion_crafter.core.mechanics import MechanicsRules, VerificationStatus
from albion_crafter.core.models import (
    ActionKind,
    CraftingContext,
    CraftingProfile,
    Item,
    MaterialRequirement,
    Recipe,
    SaleMethod,
)
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import station_type_for_item

from .models import (
    OUTER_ROYAL_CITIES,
    CapacityRole,
    ExecutionCapacityKey,
    FindMoneyConstraints,
    OptimizationResult,
    PlanAction,
    PlanReason,
    PlanReasonCode,
    PlanReasonSeverity,
    PlanStatus,
    TransportPolicy,
    quantize_profit_down,
    quantize_resource_up,
)
from .quantity import QuantityCeiling

FreshnessValidationHook = Callable[[PlanAction, datetime], Sequence[PlanReason]]


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    total_pre_revenue_cash: int
    total_focus: int
    total_expected_profit: int
    silver_remaining: int
    focus_remaining: int
    status: PlanStatus
    reasons: tuple[PlanReason, ...]

    @property
    def is_feasible(self) -> bool:
        return self.status is not PlanStatus.NON_ACTIONABLE and not any(
            reason.severity is PlanReasonSeverity.BLOCKING for reason in self.reasons
        )

    @property
    def is_decision_grade(self) -> bool:
        return self.is_feasible and self.status is PlanStatus.DECISION_GRADE


def validate_plan(
    result: OptimizationResult,
    constraints: FindMoneyConstraints,
    ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
    *,
    as_of: datetime,
    freshness_hooks: Sequence[FreshnessValidationHook] = (),
) -> PlanValidationResult:
    """Independently recompute plan resources, capacity, and final freshness."""

    if as_of.tzinfo is None:
        raise ValueError("plan validation as_of must be timezone-aware")
    actions = result.actions
    total_cash = sum(action.pre_revenue_cash_required for action in actions)
    total_focus = sum(action.focus_required for action in actions)
    total_profit = sum(action.expected_profit for action in actions)
    reasons = list(result.reasons)

    expected_totals = (
        total_cash,
        total_focus,
        total_profit,
        constraints.available_silver - total_cash,
        constraints.available_focus - total_focus,
    )
    reported_totals = (
        result.total_pre_revenue_cash,
        result.total_focus,
        result.total_expected_profit,
        result.silver_remaining,
        result.focus_remaining,
    )
    if reported_totals != expected_totals:
        reasons.append(
            PlanReason(
                PlanReasonCode.INVALID_RESOURCE_TOTAL,
                "Optimizer totals do not equal the independently recomputed action totals.",
            )
        )
    if total_cash > constraints.silver_budget:
        reasons.append(
            PlanReason(
                PlanReasonCode.INSUFFICIENT_SILVER,
                f"Plan commits {total_cash:,} silver but only "
                f"{constraints.silver_budget:,} is available after reserve.",
            )
        )
    if total_focus > constraints.focus_budget:
        reasons.append(
            PlanReason(
                PlanReasonCode.INSUFFICIENT_FOCUS,
                f"Plan commits {total_focus:,} Focus but only "
                f"{constraints.focus_budget:,} is available after reserve.",
            )
        )

    units_by_key: dict[ExecutionCapacityKey, int] = defaultdict(int)
    for action in actions:
        key = action.execution_capacity_key
        for requirement_key, units in action.capacity_consumption:
            units_by_key[requirement_key] += units
            if requirement_key not in ceilings:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.QUANTITY_CEILING_EXCEEDED,
                        f"No shared capacity ceiling exists for {requirement_key[1]} in "
                        f"{requirement_key[2]} used by {action.candidate_id}.",
                    )
                )
        reasons.extend(_validate_capacity_evidence(action, ceilings))
        if action.action_kind not in constraints.action_kinds:
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} has an action type outside the run.",
                )
            )
        ceiling = ceilings.get(key)
        if ceiling is None:
            reasons.append(
                PlanReason(
                    PlanReasonCode.QUANTITY_CEILING_EXCEEDED,
                    f"No execution ceiling exists for selected action {action.candidate_id}.",
                )
            )
        else:
            if action.quantity_ceiling != ceiling.maximum_crafts:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} does not retain its validated action cap.",
                    )
                )
            retained_ceiling = _json_object(dict(action.evidence).get("quantity_ceiling"))
            if retained_ceiling is None or (
                retained_ceiling.get("source") != ceiling.source.value
                or retained_ceiling.get("reported_24h_volume") != ceiling.reported_24h_volume
                or retained_ceiling.get("historical_volume_share")
                != ceiling.historical_volume_share
                or retained_ceiling.get("explanation") != ceiling.explanation
            ):
                reasons.append(
                    _invalid_evidence(action, "quantity-ceiling rationale does not match the run")
                )
            if action.execution_ceiling_output_units != ceiling.maximum_output_units:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} does not retain its validated output cap.",
                    )
                )
        if action.route.region is not constraints.region:
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} belongs to a different server region.",
                )
            )
        if action.sale_method is not constraints.sale_method:
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} uses a different sale method.",
                )
            )
        if action.action_kind is ActionKind.ARBITRAGE:
            outer_cities = {city.casefold() for city in OUTER_ROYAL_CITIES}
            if (
                not _city_allowed(
                    action.route.buy_city,
                    constraints.arbitrage_source_cities,
                )
                or action.route.buy_city.casefold() not in outer_cities
            ):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} has an invalid arbitrage source city.",
                    )
                )
            if (
                not _city_allowed(
                    action.route.sell_city,
                    constraints.arbitrage_destination_cities,
                )
                or action.route.sell_city.casefold() not in outer_cities
                or action.route.buy_city.casefold() == action.route.sell_city.casefold()
            ):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} has an invalid arbitrage destination.",
                    )
                )
            if action.route.production_city.casefold() != action.route.buy_city.casefold():
                reasons.append(
                    _invalid_evidence(action, "arbitrage route has a production-city surrogate")
                )
            if action.focus_required or action.focused_quantity or action.station_fee_observed_at:
                reasons.append(
                    _invalid_evidence(action, "arbitrage must not consume Focus or station inputs")
                )
        else:
            if not _city_allowed(action.route.material_city, constraints.material_cities):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} buys materials in a city outside the run.",
                    )
                )
            if not _city_allowed(action.route.production_city, constraints.production_cities):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} produces in a city outside the run.",
                    )
                )
            if not _city_allowed(action.route.sell_city, constraints.sell_cities):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.VALIDATION_FAILED,
                        f"Action {action.candidate_id} sells in a city outside the run.",
                    )
                )
        if action.route.transport_policy is not constraints.transport_policy:
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} does not use the selected transport policy.",
                )
            )
        expected_transport = (
            constraints.transport_cost_per_craft or 0
            if action.route.is_cross_city
            and constraints.transport_policy is TransportPolicy.EXPLICIT_COST
            else 0
        )
        if action.route.transport_cost_per_craft != expected_transport:
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} has an inconsistent transport cash cost.",
                )
            )
        if not constraints.use_focus and action.focused_quantity:
            reasons.append(
                PlanReason(
                    PlanReasonCode.INSUFFICIENT_FOCUS,
                    f"Action {action.candidate_id} uses Focus although Focus is disabled.",
                )
            )
        if constraints.minimum_profit is not None and (
            action.expected_profit < constraints.minimum_profit * action.quantity
        ):
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} falls below the selected per-craft profit.",
                )
            )
        if constraints.minimum_roi is not None and (
            action.roi is None or action.roi < constraints.minimum_roi
        ):
            reasons.append(
                PlanReason(
                    PlanReasonCode.VALIDATION_FAILED,
                    f"Action {action.candidate_id} falls below the selected ROI.",
                )
            )
        if action.liquidity_rank < constraints.minimum_liquidity.minimum_rank:
            reasons.append(
                PlanReason(
                    PlanReasonCode.LOW_LIQUIDITY,
                    f"Action {action.candidate_id} falls below the selected liquidity policy.",
                )
            )
        reasons.extend(
            reason for reason in action.reasons if reason.severity is PlanReasonSeverity.BLOCKING
        )
        for hook in freshness_hooks:
            reasons.extend(hook(action, as_of))

    for key, units in units_by_key.items():
        ceiling = ceilings.get(key)
        if ceiling is None:
            continue
        unit_cap = ceiling.maximum_output_units
        if unit_cap is None:
            unit_cap = ceiling.maximum_crafts
        if units > unit_cap:
            reasons.append(
                PlanReason(
                    PlanReasonCode.QUANTITY_CEILING_EXCEEDED,
                    f"Shared capacity {key[1]} in {key[2]} plans "
                    f"{units:,} market units; the conservative execution ceiling is "
                    f"{unit_cap:,}.",
                )
            )

    unique = _deduplicate_reasons(reasons)
    if not actions or any(reason.severity is PlanReasonSeverity.BLOCKING for reason in unique):
        status = PlanStatus.NON_ACTIONABLE
    elif any(reason.severity is PlanReasonSeverity.WARNING for reason in unique):
        status = PlanStatus.ADVISORY
    else:
        status = PlanStatus.DECISION_GRADE
    return PlanValidationResult(
        total_cash,
        total_focus,
        total_profit,
        constraints.available_silver - total_cash,
        constraints.available_focus - total_focus,
        status,
        unique,
    )


def action_evidence_hook(
    constraints: FindMoneyConstraints,
    rules: MechanicsRules,
) -> FreshnessValidationHook:
    """Independently validate immutable action evidence and recompute economics."""

    market_policy = FreshnessPolicy(constraints.max_market_age)
    valid_market_provenance = {
        Provenance.AODP_LIVE.value,
        Provenance.AODP_CACHED.value,
        Provenance.USER_OVERRIDE.value,
    }
    valid_user_provenance = {
        Provenance.USER_OVERRIDE.value,
        Provenance.USER_PROFILE.value,
        Provenance.DERIVED_MECHANICS.value,
    }

    def validate(action: PlanAction, as_of: datetime) -> tuple[PlanReason, ...]:
        if action.action_kind is ActionKind.ARBITRAGE:
            return _validate_arbitrage_evidence(
                action,
                constraints,
                rules,
                as_of,
                market_policy,
                valid_market_provenance,
            )
        reasons: list[PlanReason] = []
        evidence = dict(action.evidence)
        recipe = _json_object(evidence.get("recipe"))
        price_lines = _json_list(evidence.get("prices"))
        station = _json_object(evidence.get("station_fee"))
        focus = _json_object(evidence.get("focus"))
        mechanics = _json_object(evidence.get("mechanics"))
        accounting = _json_object(evidence.get("accounting"))
        transport = _json_object(evidence.get("transport"))
        ceiling = _json_object(evidence.get("quantity_ceiling"))
        required = {
            "recipe": recipe,
            "prices": price_lines,
            "station_fee": station,
            "focus": focus,
            "mechanics": mechanics,
            "accounting": accounting,
            "transport": transport,
            "quantity_ceiling": ceiling,
        }
        missing = tuple(name for name, value in required.items() if value is None)
        if missing:
            return (
                _invalid_evidence(
                    action,
                    "missing or malformed evidence sections: " + ", ".join(missing),
                ),
            )

        assert recipe is not None
        assert price_lines is not None
        assert station is not None
        assert focus is not None
        assert mechanics is not None
        assert accounting is not None
        assert transport is not None
        assert ceiling is not None

        material_ids = {
            str(value.get("item_id"))
            for value in recipe.get("materials", ())
            if isinstance(value, dict) and value.get("item_id")
        }
        if recipe.get("action_kind") != action.action_kind.value:
            reasons.append(_invalid_evidence(action, "recipe action kind is inconsistent"))
        seen_materials: set[str] = set()
        material_line_count = 0
        required_timestamps: list[datetime] = []
        output_count = 0
        for raw_line in price_lines:
            if not isinstance(raw_line, dict):
                reasons.append(_invalid_evidence(action, "a price evidence row is malformed"))
                continue
            role = raw_line.get("role")
            if role not in {"material", "output"}:
                continue
            item_id = str(raw_line.get("item_id", ""))
            city = str(raw_line.get("city", ""))
            expected_city = (
                action.route.material_city if role == "material" else action.route.sell_city
            )
            expected_side = (
                "sell_order"
                if role == "material" or action.sale_method is SaleMethod.SELL_ORDER
                else "buy_order"
            )
            if city.casefold() != expected_city.casefold() or raw_line.get("side") != expected_side:
                reasons.append(
                    _invalid_evidence(action, f"{role} price route/side is inconsistent")
                )
            if role == "material":
                material_line_count += 1
                seen_materials.add(item_id)
                if item_id not in material_ids:
                    reasons.append(_invalid_evidence(action, "material price is not in the recipe"))
            else:
                output_count += 1
                if item_id != action.item_id:
                    reasons.append(_invalid_evidence(action, "output price item is inconsistent"))
            price = raw_line.get("price")
            if not _finite_number(price) or float(price) <= 0:
                code = (
                    PlanReasonCode.MISSING_MATERIAL_PRICE
                    if role == "material"
                    else PlanReasonCode.MISSING_OUTPUT_PRICE
                )
                reasons.append(PlanReason(code, f"Action {action.candidate_id} has no price."))
            if raw_line.get("provenance") not in valid_market_provenance:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNTRUSTED_PROVENANCE,
                        f"Action {action.candidate_id} uses untrusted {role} price provenance.",
                    )
                )
            observed_at = _parse_evidence_datetime(raw_line.get("observed_at"))
            if observed_at is not None:
                required_timestamps.append(observed_at)
            freshness = market_policy.classify(observed_at, now=as_of)
            if freshness is Freshness.FUTURE:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.FUTURE_MARKET_DATA,
                        f"Action {action.candidate_id} has materially future-dated {role} "
                        "evidence.",
                    )
                )
            elif freshness not in {Freshness.FRESH, Freshness.AGING}:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.STALE_MARKET_DATA,
                        f"Action {action.candidate_id} uses the latest available {role} price; "
                        "its observation timestamp is old or unavailable.",
                        PlanReasonSeverity.WARNING,
                    )
                )
        if seen_materials != material_ids:
            reasons.append(_invalid_evidence(action, "required recipe material prices are missing"))
        if material_line_count != len(material_ids):
            reasons.append(
                _invalid_evidence(action, "material price evidence is duplicated or incomplete")
            )
        if output_count != 1:
            reasons.append(
                _invalid_evidence(action, "exactly one required output price is required")
            )
        required_line_count = material_line_count + output_count
        evidence_oldest = (
            min(required_timestamps)
            if required_line_count and len(required_timestamps) == required_line_count
            else None
        )
        if evidence_oldest != action.oldest_market_observed_at:
            reasons.append(_invalid_evidence(action, "oldest market timestamp is inconsistent"))

        if str(station.get("city", "")).casefold() != action.route.production_city.casefold():
            reasons.append(
                _invalid_evidence(action, "station city does not match the production city")
            )
        evidence_item = Item(
            action.item_id,
            action.display_name,
            int(recipe["tier"]) if recipe.get("tier") is not None else None,
            enchantment=int(recipe.get("enchantment", 0)),
            crafting_category=str(recipe.get("production_group", "")),
        )
        expected_station = station_type_for_item(evidence_item)
        if expected_station is None or station.get("station_type") != expected_station.value:
            reasons.append(_invalid_evidence(action, "station type does not match the recipe"))
        if (
            not _finite_number(station.get("displayed_fee"))
            or float(station.get("displayed_fee", -1)) < 0
        ):
            reasons.append(
                PlanReason(
                    PlanReasonCode.MISSING_STATION_FEE,
                    f"Action {action.candidate_id} has invalid station-fee evidence.",
                )
            )
        if station.get("provenance") not in valid_user_provenance:
            reasons.append(
                PlanReason(
                    PlanReasonCode.UNTRUSTED_PROVENANCE,
                    f"Action {action.candidate_id} uses untrusted station-fee provenance.",
                )
            )
        station_observed_at = _parse_evidence_datetime(station.get("observed_at"))
        if station_observed_at != action.station_fee_observed_at:
            reasons.append(_invalid_evidence(action, "station timestamp is inconsistent"))

        if action.focused_quantity:
            if focus.get("eligible") is not True or not _finite_number(focus.get("fce")):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_FCE,
                        f"Action {action.candidate_id} lacks verified Focus-efficiency evidence.",
                    )
                )

        try:
            domain_recipe = Recipe(
                output=evidence_item,
                output_quantity=int(recipe["output_quantity"]),
                materials=tuple(
                    MaterialRequirement(
                        str(material["item_id"]),
                        float(material["quantity"]),
                        material.get("returnable"),
                    )
                    for material in recipe.get("materials", ())
                    if isinstance(material, dict)
                ),
                item_value=(
                    float(recipe["item_value"]) if recipe.get("item_value") is not None else None
                ),
                base_focus_cost=(
                    float(recipe["base_focus_cost"])
                    if recipe.get("base_focus_cost") is not None
                    else None
                ),
                provenance=Provenance.STATIC_GAME_DATA,
                source_version=str(recipe.get("source_version") or "unknown"),
            )
        except (KeyError, TypeError, ValueError):
            reasons.append(_invalid_evidence(action, "recipe evidence cannot be reconstructed"))
            domain_recipe = None

        if domain_recipe is not None and action.focused_quantity:
            expected_mapping = focus_skill_mapping_for_recipe(domain_recipe)
            if expected_mapping is None or focus.get("mapping_key") != expected_mapping.mapping_key:
                reasons.append(
                    _invalid_evidence(action, "Focus mapping does not match the recipe family")
                )
            if focus.get("source") not in {"derived_profile", "manual_override"}:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_FCE,
                        f"Action {action.candidate_id} uses unknown Focus-efficiency provenance.",
                    )
                )
            if focus.get("source") == "derived_profile" and (
                focus.get("provenance") != Provenance.USER_PROFILE.value
                or focus.get("mapping_verified") is not True
                or not str(focus.get("mapping_source_version", "")).strip()
            ):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_FCE,
                        f"Action {action.candidate_id} lacks a verified derived skill mapping.",
                    )
                )
            if focus.get("source") == "manual_override" and (
                focus.get("provenance") != Provenance.USER_OVERRIDE.value
                or not str(focus.get("mapping_key", "")).strip()
            ):
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_FCE,
                        f"Action {action.candidate_id} has invalid manual FCE evidence.",
                    )
                )

        health = rules.verification_health(as_of=as_of)
        expected_components = {name: status.value for name, status in rules.verification_components}
        if mechanics.get("component_statuses") != expected_components:
            reasons.append(
                _invalid_evidence(action, "mechanics component statuses are inconsistent")
            )
        required_components = {
            "resource_return_rate",
            "marketplace_fees",
            "station_fee_formula",
            (
                "refining_city_bonuses"
                if action.action_kind is ActionKind.REFINE
                else "crafting_city_bonuses"
            ),
        }
        if action.focused_quantity:
            required_components.update(
                {
                    "focus_production_bonus",
                    (
                        "refining_fce_mapping"
                        if action.action_kind is ActionKind.REFINE
                        else "crafting_fce_mapping"
                    ),
                }
            )
        provisional_dependencies = tuple(
            sorted(
                name
                for name in required_components
                if rules.component_status(name) is not VerificationStatus.VERIFIED
            )
        )
        if provisional_dependencies:
            reasons.append(
                PlanReason(
                    PlanReasonCode.UNVERIFIED_MECHANICS,
                    f"Action {action.candidate_id} depends on provisional mechanics: "
                    + ", ".join(provisional_dependencies),
                )
            )
        if (
            mechanics.get("ruleset_id") != rules.ruleset_id
            or mechanics.get("city_bonus_dataset")
            != rules.production_bonus_dataset_version(action.action_kind)
            or mechanics.get("status") != VerificationStatus.VERIFIED.value
            or mechanics.get("action_kind") != action.action_kind.value
        ):
            reasons.append(
                PlanReason(
                    PlanReasonCode.UNVERIFIED_MECHANICS,
                    f"Action {action.candidate_id} does not retain the active verified ruleset.",
                )
            )
        elif health.is_aging:
            reasons.append(
                PlanReason(
                    PlanReasonCode.UNVERIFIED_MECHANICS,
                    health.warning or "Mechanics verification is aging.",
                    PlanReasonSeverity.WARNING,
                )
            )
        for mode, used in (
            ("nonfocused_city_bonus", action.nonfocused_quantity),
            ("focused_city_bonus", action.focused_quantity),
        ):
            bonus = mechanics.get(mode)
            classification = bonus.get("classification") if isinstance(bonus, dict) else None
            if used and classification not in {"verified_specialty", "verified_baseline"}:
                reasons.append(
                    PlanReason(
                        PlanReasonCode.UNKNOWN_CITY_BONUS,
                        f"Action {action.candidate_id} lacks a verified {mode} classification.",
                    )
                )

        for mode, use_focus in (
            ("nonfocused_city_bonus", False),
            ("focused_city_bonus", True),
        ):
            retained = mechanics.get(mode)
            expected = rules.production_bonus_resolution(
                action.action_kind,
                evidence_item,
                action.route.production_city,
                use_focus=use_focus,
            )
            if not isinstance(retained, dict) or any(
                retained.get(key) != expected_value
                for key, expected_value in (
                    ("classification", expected.classification.value),
                    ("baseline_bonus", expected.baseline_bonus),
                    ("specialty_bonus", expected.specialty_bonus),
                    ("focus_bonus", expected.focus_bonus),
                    ("total_production_bonus", expected.total_production_bonus),
                )
            ):
                reasons.append(
                    _invalid_evidence(action, f"{mode} does not match verified mechanics")
                )

        if (
            transport.get("policy") != action.route.transport_policy.value
            or transport.get("cost_per_craft") != action.route.transport_cost_per_craft
        ):
            reasons.append(_invalid_evidence(action, "transport evidence is inconsistent"))
        if (
            ceiling.get("maximum_crafts") != action.quantity_ceiling
            or ceiling.get("maximum_output_units") != action.execution_ceiling_output_units
            or ceiling.get("source")
            not in {
                "explicit_cap",
                "historical_volume_share",
                "explicit_fallback_no_history",
            }
            or not str(ceiling.get("explanation", "")).strip()
        ):
            reasons.append(_invalid_evidence(action, "quantity-ceiling evidence is inconsistent"))

        if domain_recipe is not None:
            reasons.extend(
                _validate_recomputed_economics(
                    action,
                    domain_recipe,
                    price_lines,
                    station,
                    focus,
                    accounting,
                    rules,
                    as_of,
                    constraints.premium,
                )
            )
        reasons.extend(_validate_action_accounting(action, accounting))
        return _deduplicate_reasons(reasons)

    return validate


def _validate_arbitrage_evidence(
    action: PlanAction,
    constraints: FindMoneyConstraints,
    rules: MechanicsRules,
    as_of: datetime,
    market_policy: FreshnessPolicy,
    valid_market_provenance: set[str],
) -> tuple[PlanReason, ...]:
    evidence = dict(action.evidence)
    arbitrage = _json_object(evidence.get("arbitrage"))
    prices = _json_list(evidence.get("prices"))
    marketplace = _json_object(evidence.get("marketplace"))
    accounting = _json_object(evidence.get("arbitrage_accounting"))
    transport = _json_object(evidence.get("transport"))
    capacity = _json_object(evidence.get("capacity_evidence"))
    required = {
        "arbitrage": arbitrage,
        "prices": prices,
        "marketplace": marketplace,
        "arbitrage_accounting": accounting,
        "transport": transport,
        "capacity_evidence": capacity,
    }
    missing = tuple(name for name, value in required.items() if value is None)
    if missing:
        return (
            _invalid_evidence(
                action,
                "missing or malformed arbitrage sections: " + ", ".join(missing),
            ),
        )
    assert arbitrage is not None
    assert prices is not None
    assert marketplace is not None
    assert accounting is not None
    assert transport is not None
    assert capacity is not None

    reasons: list[PlanReason] = []
    if any(
        (
            arbitrage.get("action_kind") != ActionKind.ARBITRAGE.value,
            arbitrage.get("item_id") != action.item_id,
            str(arbitrage.get("source_city", "")).casefold() != action.route.buy_city.casefold(),
            str(arbitrage.get("destination_city", "")).casefold()
            != action.route.sell_city.casefold(),
            arbitrage.get("quality") != action.quality,
        )
    ):
        reasons.append(_invalid_evidence(action, "arbitrage identity is inconsistent"))

    expected_lines = {
        "arbitrage_source": (action.route.buy_city, "sell_order"),
        "arbitrage_destination": (
            action.route.sell_city,
            ("sell_order" if action.sale_method is SaleMethod.SELL_ORDER else "buy_order"),
        ),
    }
    retained_prices: dict[str, float] = {}
    timestamps: list[datetime] = []
    seen_roles: list[str] = []
    for raw_line in prices:
        if not isinstance(raw_line, dict) or raw_line.get("role") not in expected_lines:
            reasons.append(_invalid_evidence(action, "an arbitrage price row is malformed"))
            continue
        role = str(raw_line["role"])
        seen_roles.append(role)
        expected_city, expected_side = expected_lines[role]
        if (
            raw_line.get("item_id") != action.item_id
            or str(raw_line.get("city", "")).casefold() != expected_city.casefold()
            or raw_line.get("side") != expected_side
        ):
            reasons.append(_invalid_evidence(action, f"{role} route or side is inconsistent"))
        price = raw_line.get("price")
        if not _finite_number(price) or float(price) <= 0:
            reasons.append(_invalid_evidence(action, f"{role} price is nonpositive"))
        else:
            retained_prices[role] = float(price)
        if raw_line.get("provenance") not in valid_market_provenance:
            reasons.append(_invalid_evidence(action, f"{role} provenance is untrusted"))
        observed_at = _parse_evidence_datetime(raw_line.get("observed_at"))
        if observed_at is not None:
            timestamps.append(observed_at)
        freshness = market_policy.classify(observed_at, now=as_of)
        if freshness is Freshness.FUTURE:
            reasons.append(
                PlanReason(
                    PlanReasonCode.FUTURE_MARKET_DATA,
                    f"Action {action.candidate_id} has materially future-dated {role} evidence.",
                )
            )
        elif freshness not in {Freshness.FRESH, Freshness.AGING}:
            reasons.append(
                PlanReason(
                    PlanReasonCode.STALE_MARKET_DATA,
                    f"Action {action.candidate_id} uses the latest available {role} price; "
                    "its observation timestamp is old or unavailable.",
                    PlanReasonSeverity.WARNING,
                )
            )
    if sorted(seen_roles) != sorted(expected_lines):
        reasons.append(_invalid_evidence(action, "source and destination prices must be unique"))
    if (min(timestamps) if len(timestamps) == 2 else None) != action.oldest_market_observed_at:
        reasons.append(_invalid_evidence(action, "oldest arbitrage timestamp is inconsistent"))

    if any(
        (
            marketplace.get("ruleset_id") != rules.ruleset_id,
            marketplace.get("premium") is not constraints.premium,
            marketplace.get("sale_method") != action.sale_method.value,
            marketplace.get("setup_rate") != rules.sell_order_setup_fee,
            marketplace.get("transaction_tax_rate")
            != rules.transaction_tax(premium=constraints.premium),
            marketplace.get("marketplace_fee_status")
            != rules.component_status("marketplace_fees").value,
        )
    ):
        reasons.append(_invalid_evidence(action, "marketplace mechanics are inconsistent"))
    if rules.component_status("marketplace_fees") is not VerificationStatus.VERIFIED:
        reasons.append(
            PlanReason(
                PlanReasonCode.UNVERIFIED_MECHANICS,
                f"Action {action.candidate_id} lacks verified marketplace fee mechanics.",
            )
        )
    if (
        transport.get("policy") != action.route.transport_policy.value
        or transport.get("cost_per_action_unit") != action.route.transport_cost_per_action_unit
    ):
        reasons.append(_invalid_evidence(action, "arbitrage transport is inconsistent"))
    if any(accounting.get(name) is not None for name in ("focus", "station", "rrr", "fce")):
        reasons.append(_invalid_evidence(action, "production-only inputs must be N/A"))

    requirements = {requirement.role: requirement for requirement in action.capacity_requirements}
    source_requirement = requirements.get(CapacityRole.ACQUISITION)
    destination_requirement = requirements.get(CapacityRole.LIQUIDATION)
    if (
        len(requirements) != 2
        or source_requirement is None
        or destination_requirement is None
        or source_requirement.key
        != (action.route.region, action.item_id, action.route.buy_city, action.quality)
        or destination_requirement.key != action.execution_capacity_key
        or source_requirement.units_per_action_unit != 1
        or destination_requirement.units_per_action_unit != 1
    ):
        reasons.append(_invalid_evidence(action, "arbitrage capacity requirements are invalid"))

    if set(retained_prices) == set(expected_lines):
        expected = calculate_arbitrage_economics(
            retained_prices["arbitrage_source"],
            retained_prices["arbitrage_destination"],
            premium=constraints.premium,
            sale_method=action.sale_method,
            transport_cash_per_unit=action.route.transport_cost_per_action_unit,
            rules=rules,
        )
        float_fields = {
            "purchase_cash": expected.purchase_cash,
            "gross_destination_value": expected.gross_destination_value,
            "setup_cash": expected.setup_cash,
            "transaction_tax": expected.transaction_tax,
            "transport_cash": expected.transport_cash,
            "pre_revenue_cash": expected.pre_revenue_cash,
            "net_sale_proceeds": expected.net_sale_proceeds,
            "effective_economic_cost": expected.effective_economic_cost,
            "expected_profit": expected.expected_profit,
            "roi": expected.roi,
            "margin": expected.margin,
        }
        for field, expected_value in float_fields.items():
            actual = accounting.get(field)
            if (
                not _finite_number(actual)
                or expected_value is None
                or not math.isclose(float(actual), expected_value, rel_tol=1e-9, abs_tol=1e-7)
            ):
                reasons.append(_invalid_evidence(action, f"{field} does not recompute"))
        expected_action = {
            "pre-revenue cash": quantize_resource_up(expected.pre_revenue_cash) * action.quantity,
            "expected profit": quantize_profit_down(expected.expected_profit) * action.quantity,
            "expected revenue": quantize_resource_up(expected.gross_destination_value)
            * action.quantity,
            "economic cost": quantize_resource_up(expected.effective_economic_cost)
            * action.quantity,
        }
        actual_action = {
            "pre-revenue cash": action.pre_revenue_cash_required,
            "expected profit": action.expected_profit,
            "expected revenue": action.expected_revenue,
            "economic cost": action.effective_economic_cost,
        }
        for field, expected_value in expected_action.items():
            if actual_action[field] != expected_value:
                reasons.append(_invalid_evidence(action, f"{field} does not recompute"))
    if (
        action.focus_required != 0
        or action.focused_quantity != 0
        or action.nonfocused_quantity != action.quantity
        or action.incremental_focus_profit not in {None, 0}
    ):
        reasons.append(_invalid_evidence(action, "arbitrage Focus accounting is nonzero"))
    return _deduplicate_reasons(reasons)


def _validate_recomputed_economics(
    action: PlanAction,
    recipe: Recipe,
    price_lines: list,
    station: dict,
    focus: dict,
    accounting: dict,
    rules: MechanicsRules,
    as_of: datetime,
    premium: bool,
) -> tuple[PlanReason, ...]:
    material_prices: dict[str, float] = {}
    output_prices: list[float] = []
    for line in price_lines:
        if not isinstance(line, dict) or not _finite_number(line.get("price")):
            continue
        if line.get("role") == "material":
            material_prices[str(line.get("item_id"))] = float(line["price"])
        elif line.get("role") == "output":
            output_prices.append(float(line["price"]))
    if len(output_prices) != 1 or set(material_prices) != {
        material.item_id for material in recipe.materials
    }:
        return (_invalid_evidence(action, "prices cannot reconstruct recipe economics"),)

    fce = focus.get("fce")
    if not _finite_number(fce):
        fce = 0.0
    profile = CraftingProfile(
        available_focus=1_000_000_000_000.0,
        focus_cost_efficiency=float(fce),
    )
    base_context = dict(
        craft_city=action.route.production_city,
        sell_city=action.route.sell_city,
        material_buy_city=action.route.material_city,
        crafts=1,
        output_quality=1,
        premium=premium,
        station_usage_fee_percent=float(station["displayed_fee"]),
        as_of=as_of,
        sale_method=action.sale_method,
        profile=profile,
    )
    calculator = CraftCalculator(rules)
    expected_modes = {
        "nonfocused_per_craft": calculator.calculate(
            recipe,
            material_prices,
            output_prices[0],
            CraftingContext(use_focus=False, **base_context),
        )
    }
    if action.focused_quantity:
        expected_modes["focused_per_craft"] = calculator.calculate(
            recipe,
            material_prices,
            output_prices[0],
            CraftingContext(use_focus=True, **base_context),
        )

    reasons: list[PlanReason] = []
    fields = (
        "return_rate",
        "gross_material_purchase_cash",
        "returned_material_cost_basis_value",
        "effective_material_cost",
        "station_cash",
        "listing_setup_cash",
        "transaction_tax",
        "market_fees",
        "total_pre_revenue_cash_required",
        "effective_economic_cost",
        "gross_sale_value",
        "net_sale_value",
        "profit",
        "focus_used",
    )
    for mode, result in expected_modes.items():
        retained = accounting.get(mode)
        if not isinstance(retained, dict):
            reasons.append(_invalid_evidence(action, f"{mode} accounting is missing"))
            continue
        for field in fields:
            expected = getattr(result, field)
            actual = retained.get(field)
            if expected is None and actual is None:
                continue
            if (
                not _finite_number(expected)
                or not _finite_number(actual)
                or not math.isclose(
                    float(expected),
                    float(actual),
                    rel_tol=1e-9,
                    abs_tol=1e-7,
                )
            ):
                reasons.append(_invalid_evidence(action, f"{mode} {field} does not recompute"))
    return tuple(reasons)


def _validate_action_accounting(
    action: PlanAction,
    accounting: dict,
) -> tuple[PlanReason, ...]:
    nonfocused = accounting.get("nonfocused_per_craft")
    focused = accounting.get("focused_per_craft")
    if not isinstance(nonfocused, dict) or (
        action.focused_quantity and not isinstance(focused, dict)
    ):
        return (_invalid_evidence(action, "per-mode accounting evidence is incomplete"),)
    focused = focused if isinstance(focused, dict) else {}
    required_nonfocused = (
        "gross_material_purchase_cash",
        "station_cash",
        "listing_setup_cash",
        "effective_economic_cost",
        "gross_sale_value",
        "profit",
    )
    required_focused = (*required_nonfocused, "focus_used")
    if any(not _finite_number(nonfocused.get(key)) for key in required_nonfocused) or (
        action.focused_quantity
        and any(not _finite_number(focused.get(key)) for key in required_focused)
    ):
        return (_invalid_evidence(action, "per-mode accounting contains missing values"),)

    transport = action.route.transport_cost_per_craft
    cash_per_craft = (
        quantize_resource_up(nonfocused["gross_material_purchase_cash"])
        + quantize_resource_up(nonfocused["station_cash"])
        + quantize_resource_up(nonfocused["listing_setup_cash"])
        + transport
    )
    nonfocused_profit = quantize_profit_down(nonfocused["profit"] - transport)
    focused_profit = (
        quantize_profit_down(focused["profit"] - transport) if action.focused_quantity else 0
    )
    expected = {
        "pre-revenue cash": cash_per_craft * action.quantity,
        "expected profit": (
            nonfocused_profit * action.nonfocused_quantity
            + focused_profit * action.focused_quantity
        ),
        "expected revenue": quantize_resource_up(nonfocused["gross_sale_value"]) * action.quantity,
        "economic cost": (
            quantize_resource_up(nonfocused["effective_economic_cost"] + transport)
            * action.nonfocused_quantity
            + (
                quantize_resource_up(focused["effective_economic_cost"] + transport)
                * action.focused_quantity
                if action.focused_quantity
                else 0
            )
        ),
        "Focus": (
            quantize_resource_up(focused["focus_used"]) * action.focused_quantity
            if action.focused_quantity
            else 0
        ),
        "incremental Focus profit": (
            (focused_profit - nonfocused_profit) * action.focused_quantity
            if action.focused_quantity
            else 0
        ),
    }
    actual = {
        "pre-revenue cash": action.pre_revenue_cash_required,
        "expected profit": action.expected_profit,
        "expected revenue": action.expected_revenue,
        "economic cost": action.effective_economic_cost,
        "Focus": action.focus_required,
        "incremental Focus profit": action.incremental_focus_profit,
    }
    return tuple(
        _invalid_evidence(action, f"{name} does not match recomputed per-mode accounting")
        for name, expected_value in expected.items()
        if actual[name] != expected_value
    )


def _validate_capacity_evidence(
    action: PlanAction,
    ceilings: Mapping[ExecutionCapacityKey, QuantityCeiling],
) -> tuple[PlanReason, ...]:
    """Validate the complete generic capacity envelope retained by V3 actions."""

    raw = _json_list(dict(action.evidence).get("capacity_ceilings"))
    if raw is None:
        return (
            (_invalid_evidence(action, "multi-capacity ceiling evidence is missing"),)
            if len(action.capacity_requirements) > 1
            else ()
        )
    rows: dict[ExecutionCapacityKey, dict] = {}
    reasons: list[PlanReason] = []
    for value in raw:
        if not isinstance(value, dict):
            reasons.append(_invalid_evidence(action, "a capacity-ceiling row is malformed"))
            continue
        key_value = value.get("key")
        try:
            key = (
                action.route.region.__class__(key_value[0]),
                str(key_value[1]),
                str(key_value[2]),
                int(key_value[3]),
            )
        except (IndexError, TypeError, ValueError):
            reasons.append(_invalid_evidence(action, "a capacity-ceiling key is malformed"))
            continue
        if key in rows:
            reasons.append(_invalid_evidence(action, "capacity-ceiling rows are duplicated"))
        rows[key] = value
    expected_keys = {requirement.key for requirement in action.capacity_requirements}
    if set(rows) != expected_keys:
        reasons.append(_invalid_evidence(action, "capacity-ceiling keys are incomplete"))
    for requirement in action.capacity_requirements:
        ceiling = ceilings.get(requirement.key)
        row = rows.get(requirement.key)
        if ceiling is None or row is None:
            continue
        if any(
            (
                row.get("role") != requirement.role.value,
                row.get("units_per_action_unit") != requirement.units_per_action_unit,
                row.get("maximum_action_units") != ceiling.maximum_crafts,
                row.get("maximum_market_units") != ceiling.maximum_output_units,
                row.get("source") != ceiling.source.value,
                row.get("reported_24h_volume") != ceiling.reported_24h_volume,
                row.get("historical_volume_share") != ceiling.historical_volume_share,
                row.get("explanation") != ceiling.explanation,
            )
        ):
            reasons.append(
                _invalid_evidence(action, "a retained capacity ceiling does not match the run")
            )
    return tuple(reasons)


def _json_object(value: str | None) -> dict | None:
    try:
        parsed = json.loads(value) if value is not None else None
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_list(value: str | None) -> list | None:
    try:
        parsed = json.loads(value) if value is not None else None
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _parse_evidence_datetime(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _finite_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _invalid_evidence(action: PlanAction, message: str) -> PlanReason:
    return PlanReason(
        PlanReasonCode.INVALID_ACTION_EVIDENCE,
        f"Action {action.candidate_id} has invalid immutable evidence: {message}.",
    )


def _city_allowed(city: str, allowed: Sequence[str]) -> bool:
    return city.casefold() in {value.casefold() for value in allowed}


def default_freshness_hooks(
    constraints: FindMoneyConstraints,
) -> tuple[FreshnessValidationHook, FreshnessValidationHook]:
    """Build the standard final market/station timestamp checks for one run."""

    return (
        market_freshness_hook(constraints.max_market_age),
        station_fee_freshness_hook(
            constraints.max_station_fee_age,
            allow_stale=constraints.allow_stale_station_fees,
        ),
    )


def market_freshness_hook(max_age: timedelta) -> FreshnessValidationHook:
    if max_age <= timedelta(0):
        raise ValueError("market freshness max_age must be positive")

    policy = FreshnessPolicy(max_age)

    def validate(action: PlanAction, as_of: datetime) -> tuple[PlanReason, ...]:
        observed_at = action.oldest_market_observed_at
        freshness = policy.classify(observed_at, now=as_of)
        if freshness in {Freshness.FRESH, Freshness.AGING}:
            return ()
        if freshness is Freshness.FUTURE:
            return (
                PlanReason(
                    PlanReasonCode.FUTURE_MARKET_DATA,
                    f"Action {action.candidate_id} has required market evidence beyond the "
                    "tolerated two-minute clock skew at plan completion.",
                ),
            )
        return (
            PlanReason(
                PlanReasonCode.STALE_MARKET_DATA,
                f"Action {action.candidate_id} uses the latest available required market "
                "evidence at plan completion; its timestamp is old or unavailable.",
                PlanReasonSeverity.WARNING,
            ),
        )

    return validate


def station_fee_freshness_hook(
    max_age: timedelta,
    *,
    allow_stale: bool = False,
) -> FreshnessValidationHook:
    if max_age <= timedelta(0):
        raise ValueError("station freshness max_age must be positive")

    policy = FreshnessPolicy(max_age)

    def validate(action: PlanAction, as_of: datetime) -> tuple[PlanReason, ...]:
        if action.action_kind is ActionKind.ARBITRAGE:
            return ()
        observed_at = action.station_fee_observed_at
        freshness = policy.classify(observed_at, now=as_of)
        if freshness in {Freshness.FRESH, Freshness.AGING}:
            return ()
        if freshness is Freshness.FUTURE:
            return (
                PlanReason(
                    PlanReasonCode.FUTURE_STATION_FEE,
                    f"Action {action.candidate_id} has station-fee evidence beyond the tolerated "
                    "two-minute clock skew at plan completion.",
                ),
            )
        return (
            PlanReason(
                PlanReasonCode.STALE_STATION_FEE,
                f"Action {action.candidate_id} has missing or stale station-fee evidence at "
                "plan completion.",
                (PlanReasonSeverity.WARNING if allow_stale else PlanReasonSeverity.BLOCKING),
            ),
        )

    return validate


def _deduplicate_reasons(reasons) -> tuple[PlanReason, ...]:
    unique = {(reason.code, reason.message, reason.severity): reason for reason in reasons}
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[2].value, item[0].value, item[1]),
        )
    )
