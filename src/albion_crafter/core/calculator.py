from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from .actionability import (
    ActionabilityAssessment,
    ActionabilityReason,
    ReasonCode,
    ReasonSeverity,
)
from .crafting_profile import CraftingSkillProfile, focus_skill_mapping_for_recipe
from .fees import (
    calculate_market_fee_components,
    calculate_market_fees,
    calculate_station_fee,
)
from .focus import calculate_focus_cost, calculate_silver_per_focus
from .freshness import Freshness
from .mechanics import CURRENT_RULES, MechanicsRules, VerificationStatus
from .models import (
    ActionKind,
    CraftingContext,
    CraftResult,
    MaterialRequirement,
    Recipe,
    SaleMethod,
)
from .provenance import Provenance
from .returns import calculate_expected_material_return, calculate_return_rate
from .stations import station_type_for_item


def calculate_material_cost(
    materials: tuple[MaterialRequirement, ...],
    material_prices: Mapping[str, float | None],
    *,
    crafts: int = 1,
) -> tuple[float | None, float | None, tuple[str, ...]]:
    """Return (returnable cost, non-returnable cost, missing item IDs)."""
    if crafts < 1:
        raise ValueError("crafts must be positive")
    missing = tuple(
        sorted(
            {
                material.item_id
                for material in materials
                if material_prices.get(material.item_id) is None
            }
        )
    )
    if missing or any(material.returnable is None for material in materials):
        return None, None, missing

    returnable = 0.0
    non_returnable = 0.0
    for material in materials:
        price = material_prices[material.item_id]
        assert price is not None
        if not _finite_number(price) or price < 0:
            raise ValueError(f"price for {material.item_id} must be finite and non-negative")
        line_cost = material.quantity * price * crafts
        if material.returnable:
            returnable += line_cost
        else:
            non_returnable += line_cost
    return returnable, non_returnable, ()


def calculate_upfront_material_cost(
    materials: tuple[MaterialRequirement, ...],
    material_prices: Mapping[str, float | None],
    *,
    crafts: int = 1,
) -> tuple[float | None, tuple[str, ...]]:
    """Return gross cash needed to acquire inputs before any material returns."""

    if crafts < 1:
        raise ValueError("crafts must be positive")
    missing = tuple(
        sorted(
            {
                material.item_id
                for material in materials
                if material_prices.get(material.item_id) is None
            }
        )
    )
    if missing:
        return None, missing
    total = 0.0
    for material in materials:
        price = material_prices[material.item_id]
        assert price is not None
        if not _finite_number(price) or price < 0:
            raise ValueError(f"price for {material.item_id} must be finite and non-negative")
        total += material.quantity * price * crafts
    return total, ()


def calculate_profit(net_sale_value: float, total_craft_cost: float) -> float:
    return net_sale_value - total_craft_cost


def calculate_roi(profit: float, total_craft_cost: float) -> float | None:
    return None if total_craft_cost == 0 else profit / total_craft_cost


def calculate_margin(profit: float, gross_sale_value: float) -> float | None:
    return None if gross_sale_value == 0 else profit / gross_sale_value


def calculate_break_even_price(
    total_craft_cost: float,
    output_quantity: int,
    transaction_tax_rate: float,
    setup_fee_rate: float,
    sale_method: SaleMethod = SaleMethod.SELL_ORDER,
) -> float | None:
    if output_quantity < 1:
        raise ValueError("output_quantity must be positive")
    fee_rate = transaction_tax_rate
    if sale_method is SaleMethod.SELL_ORDER:
        fee_rate += setup_fee_rate
    retained_rate = 1.0 - fee_rate
    if retained_rate <= 0:
        return None
    return total_craft_cost / (output_quantity * retained_rate)


class CraftCalculator:
    """Pure crafting calculator driven by an explicit versioned ruleset."""

    def __init__(self, rules: MechanicsRules = CURRENT_RULES) -> None:
        self.rules = rules

    def calculate(
        self,
        recipe: Recipe,
        material_prices: Mapping[str, float | None],
        output_unit_price: float | None,
        context: CraftingContext,
        data_quality: ActionabilityAssessment | None = None,
        *,
        returned_material_craft_city_prices: Mapping[str, float | None] | None = None,
    ) -> CraftResult:
        if output_unit_price is not None and (
            not _finite_number(output_unit_price) or output_unit_price < 0
        ):
            raise ValueError("output_unit_price must be finite and non-negative")

        crafts = context.crafts
        output_quantity = recipe.output_quantity * crafts
        displayed_station_fee, station_fee_problem = self._resolve_station_fee_input(
            recipe, context
        )
        focus_efficiency = self._resolve_focus_efficiency(recipe, context)
        reasons = self._initial_reasons(
            recipe,
            material_prices,
            output_unit_price,
            context,
            displayed_station_fee=displayed_station_fee,
            station_fee_problem=station_fee_problem,
            focus_efficiency=focus_efficiency,
        )
        if data_quality is not None:
            reasons.extend(data_quality.reasons)
        else:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.UNTRUSTED_PROVENANCE,
                    "Price provenance and timestamps were not supplied.",
                )
            )
        if context.crafts > 1 or recipe.output_quantity > 1:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.TOP_OF_BOOK_DEPTH_UNMODELED,
                    "The requested batch uses top-of-book unit prices; "
                    "order-book depth is not modeled.",
                    ReasonSeverity.WARNING,
                )
            )
        returnable, non_returnable, missing = calculate_material_cost(
            recipe.materials, material_prices, crafts=crafts
        )
        upfront_material_cost, _ = calculate_upfront_material_cost(
            recipe.materials, material_prices, crafts=crafts
        )
        missing_prices = set(missing)
        if output_unit_price is None:
            missing_prices.add(recipe.output.item_id)
        station_fee: float | None = None
        if recipe.item_value is not None and displayed_station_fee is not None:
            station_fee = calculate_station_fee(
                recipe.item_value,
                displayed_station_fee,
                station_nutrition_factor=self.rules.station_nutrition_factor,
                item_count=output_quantity,
            )
        pre_listing_cash = (
            upfront_material_cost + station_fee
            if upfront_material_cost is not None and station_fee is not None
            else None
        )

        no_focus_city_bonus = self.rules.production_bonus_resolution(
            recipe.action_kind,
            recipe.output,
            context.production_city,
            use_focus=False,
        )
        with_focus_city_bonus = self.rules.production_bonus_resolution(
            recipe.action_kind,
            recipe.output,
            context.production_city,
            use_focus=True,
        )
        no_focus_bonus = no_focus_city_bonus.total_production_bonus
        with_focus_bonus = with_focus_city_bonus.total_production_bonus
        if not no_focus_city_bonus.is_verified:
            classification = no_focus_city_bonus.classification.value.replace("_", " ")
            reasons.append(
                ActionabilityReason(
                    ReasonCode.UNKNOWN_CITY_BONUS_CLASSIFICATION,
                    f"City {recipe.action_kind.value} production bonus classification is "
                    f"{classification} for "
                    f"{recipe.output.crafting_category or recipe.output.item_id} in "
                    f"{context.craft_city}.",
                )
            )

        if (
            returnable is None
            or non_returnable is None
            or no_focus_bonus is None
            or with_focus_bonus is None
        ):
            return self._incomplete(
                recipe,
                context,
                output_quantity,
                missing_prices,
                reasons,
                raw_material_cost=upfront_material_cost,
                station_fee=station_fee,
                upfront_capital_required=pre_listing_cash,
            )

        no_focus_rate = calculate_return_rate(no_focus_bonus)
        with_focus_rate = calculate_return_rate(with_focus_bonus)
        selected_rate = with_focus_rate if context.use_focus else no_focus_rate
        raw_material_cost = returnable + non_returnable
        returned_value = calculate_expected_material_return(returnable, selected_rate)
        returned_craft_city_value = self._returned_material_market_value(
            recipe,
            returned_material_craft_city_prices,
            crafts=crafts,
            return_rate=selected_rate,
        )
        effective_without_focus = raw_material_cost - calculate_expected_material_return(
            returnable, no_focus_rate
        )
        effective_with_focus = raw_material_cost - calculate_expected_material_return(
            returnable, with_focus_rate
        )
        effective_selected = effective_with_focus if context.use_focus else effective_without_focus

        if output_unit_price is None:
            total = effective_selected + station_fee if station_fee is not None else None
            listing_cash = 0.0 if context.sale_method is SaleMethod.INSTANT_SELL else None
            total_pre_revenue = (
                pre_listing_cash + listing_cash
                if pre_listing_cash is not None and listing_cash is not None
                else None
            )
            return CraftResult(
                item_id=recipe.output.item_id,
                crafts=crafts,
                output_quantity=output_quantity,
                raw_material_cost=raw_material_cost,
                expected_returned_material_value=returned_value,
                effective_material_cost=effective_selected,
                station_fee=station_fee,
                total_craft_cost=total,
                gross_sale_value=None,
                market_fees=None,
                net_sale_value=None,
                profit=None,
                roi=None,
                margin=None,
                break_even_price=None,
                focus_used=None,
                focus_available=context.profile.available_focus,
                focus_shortfall=None,
                profit_without_focus=None,
                profit_with_focus=None,
                incremental_focus_profit=None,
                silver_per_focus=None,
                return_rate=selected_rate,
                ruleset_id=self.rules.ruleset_id,
                actionability=ActionabilityAssessment(tuple(reasons)),
                missing_price_item_ids=tuple(sorted(missing_prices)),
                upfront_material_cost=upfront_material_cost,
                upfront_capital_required=pre_listing_cash,
                gross_material_purchase_cash=upfront_material_cost,
                station_cash=station_fee,
                listing_setup_cash=listing_cash,
                transaction_tax=None,
                total_pre_revenue_cash_required=total_pre_revenue,
                effective_economic_cost=None,
                returned_material_cost_basis_value=returned_value,
                returned_material_craft_city_market_value=returned_craft_city_value,
            )

        gross_sale = output_unit_price * output_quantity
        tax_rate = self.rules.transaction_tax(premium=context.premium)
        listing_cash, transaction_tax = calculate_market_fee_components(
            gross_sale,
            tax_rate,
            self.rules.sell_order_setup_fee,
            context.sale_method,
        )
        market_fees = calculate_market_fees(
            gross_sale,
            tax_rate,
            self.rules.sell_order_setup_fee,
            context.sale_method,
        )
        net_sale = gross_sale - market_fees
        upfront_capital = pre_listing_cash + listing_cash if pre_listing_cash is not None else None

        if station_fee is None:
            return CraftResult(
                item_id=recipe.output.item_id,
                crafts=crafts,
                output_quantity=output_quantity,
                raw_material_cost=raw_material_cost,
                expected_returned_material_value=returned_value,
                effective_material_cost=effective_selected,
                station_fee=None,
                total_craft_cost=None,
                gross_sale_value=gross_sale,
                market_fees=market_fees,
                net_sale_value=net_sale,
                profit=None,
                roi=None,
                margin=None,
                break_even_price=None,
                focus_used=None,
                focus_available=context.profile.available_focus,
                focus_shortfall=None,
                profit_without_focus=None,
                profit_with_focus=None,
                incremental_focus_profit=None,
                silver_per_focus=None,
                return_rate=selected_rate,
                ruleset_id=self.rules.ruleset_id,
                actionability=ActionabilityAssessment(tuple(reasons)),
                upfront_material_cost=upfront_material_cost,
                upfront_capital_required=None,
                gross_material_purchase_cash=upfront_material_cost,
                station_cash=None,
                listing_setup_cash=listing_cash,
                transaction_tax=transaction_tax,
                total_pre_revenue_cash_required=None,
                effective_economic_cost=None,
                returned_material_cost_basis_value=returned_value,
                returned_material_craft_city_market_value=returned_craft_city_value,
            )

        total_without_focus = effective_without_focus + station_fee
        total_with_focus = effective_with_focus + station_fee
        profit_without_focus = calculate_profit(net_sale, total_without_focus)
        profit_with_focus = calculate_profit(net_sale, total_with_focus)
        selected_total = total_with_focus if context.use_focus else total_without_focus
        selected_profit = profit_with_focus if context.use_focus else profit_without_focus
        incremental = profit_with_focus - profit_without_focus
        effective_economic_cost = selected_total + market_fees

        focus_used: float | None = None
        focus_shortfall: float | None = None
        silver_per_focus: float | None = None
        if (
            context.use_focus
            and recipe.base_focus_cost is not None
            and recipe.base_focus_cost > 0
            and focus_efficiency is not None
        ):
            focus_used = (
                calculate_focus_cost(
                    recipe.base_focus_cost,
                    focus_efficiency,
                )
                * crafts
            )
            focus_shortfall = max(focus_used - context.profile.available_focus, 0.0)
            if focus_shortfall:
                reasons.append(
                    ActionabilityReason(
                        ReasonCode.INSUFFICIENT_FOCUS,
                        f"Focused batch needs {focus_used:,.0f} Focus; "
                        f"only {context.profile.available_focus:,.0f} is available.",
                    )
                )
            silver_per_focus = calculate_silver_per_focus(incremental, focus_used)

        return CraftResult(
            item_id=recipe.output.item_id,
            crafts=crafts,
            output_quantity=output_quantity,
            raw_material_cost=raw_material_cost,
            expected_returned_material_value=returned_value,
            effective_material_cost=effective_selected,
            station_fee=station_fee,
            total_craft_cost=selected_total,
            gross_sale_value=gross_sale,
            market_fees=market_fees,
            net_sale_value=net_sale,
            profit=selected_profit,
            roi=calculate_roi(selected_profit, effective_economic_cost),
            margin=calculate_margin(selected_profit, gross_sale),
            break_even_price=calculate_break_even_price(
                selected_total,
                output_quantity,
                self.rules.transaction_tax(premium=context.premium),
                self.rules.sell_order_setup_fee,
                context.sale_method,
            ),
            focus_used=focus_used,
            focus_available=context.profile.available_focus,
            focus_shortfall=focus_shortfall,
            profit_without_focus=profit_without_focus,
            profit_with_focus=profit_with_focus,
            incremental_focus_profit=incremental,
            silver_per_focus=silver_per_focus,
            return_rate=selected_rate,
            ruleset_id=self.rules.ruleset_id,
            actionability=ActionabilityAssessment(tuple(reasons)),
            missing_price_item_ids=(),
            upfront_material_cost=upfront_material_cost,
            upfront_capital_required=pre_listing_cash,
            gross_material_purchase_cash=upfront_material_cost,
            station_cash=station_fee,
            listing_setup_cash=listing_cash,
            transaction_tax=transaction_tax,
            total_pre_revenue_cash_required=upfront_capital,
            effective_economic_cost=effective_economic_cost,
            returned_material_cost_basis_value=returned_value,
            returned_material_craft_city_market_value=returned_craft_city_value,
        )

    def _initial_reasons(
        self,
        recipe: Recipe,
        material_prices: Mapping[str, float | None],
        output_unit_price: float | None,
        context: CraftingContext,
        *,
        displayed_station_fee: float | None,
        station_fee_problem: str | None,
        focus_efficiency: float | None,
    ) -> list[ActionabilityReason]:
        reasons: list[ActionabilityReason] = []
        for material in recipe.materials:
            if material_prices.get(material.item_id) is None:
                reasons.append(
                    ActionabilityReason(
                        ReasonCode.MISSING_MATERIAL_PRICE,
                        f"{material.item_id} material price is missing.",
                    )
                )
            if material.returnable is None:
                reasons.append(
                    ActionabilityReason(
                        ReasonCode.UNKNOWN_RETURNABILITY,
                        f"{material.item_id} returnability is unverified.",
                    )
                )
        if output_unit_price is None:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.MISSING_OUTPUT_PRICE,
                    f"{recipe.output.item_id} output price is missing.",
                )
            )
        if context.output_quality > 1:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.UNSUPPORTED_OUTPUT_QUALITY,
                    "Output quality above Normal is hypothetical; crafting quality "
                    "probability is not modeled.",
                )
            )
        if displayed_station_fee is None:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.UNKNOWN_STATION_FEE,
                    station_fee_problem
                    or f"Station usage fee is unknown for {context.craft_city}.",
                )
            )
        elif context.station_fee_freshness_policy is not None:
            observation = context.station_fee_observation
            if observation is None:
                reasons.append(
                    ActionabilityReason(
                        ReasonCode.UNKNOWN_STATION_FEE_TIMESTAMP,
                        "Station fee was supplied without a timestamp, so its age is unknown.",
                    )
                )
            else:
                as_of = context.as_of or datetime.now(UTC)
                freshness = context.station_fee_freshness_policy.classify(
                    observation.observed_at,
                    now=as_of,
                )
                if freshness is Freshness.STALE:
                    age = max(as_of - observation.observed_at, timedelta(0))
                    maximum_hours = (
                        context.station_fee_freshness_policy.max_age.total_seconds() / 3600
                    )
                    reasons.append(
                        ActionabilityReason(
                            ReasonCode.STALE_STATION_FEE,
                            f"{context.craft_city} {observation.station_type.display_name} fee "
                            f"was observed {age.total_seconds() / 3600:.1f}h ago; maximum "
                            f"accepted age is {maximum_hours:.1f}h.",
                        )
                    )
                elif freshness is Freshness.FUTURE:
                    reasons.append(
                        ActionabilityReason(
                            ReasonCode.FUTURE_STATION_FEE_TIMESTAMP,
                            f"{context.craft_city} {observation.station_type.display_name} fee "
                            "has a timestamp beyond the tolerated two-minute clock skew.",
                        )
                    )
                elif freshness is Freshness.UNKNOWN:
                    reasons.append(
                        ActionabilityReason(
                            ReasonCode.UNKNOWN_STATION_FEE_TIMESTAMP,
                            "Station fee observation timestamp is unknown.",
                        )
                    )
        if recipe.item_value is None:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.UNKNOWN_ITEM_VALUE,
                    "Station cost cannot be calculated because item value is unknown.",
                )
            )
        if recipe.recipe_ambiguous:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.AMBIGUOUS_RECIPE,
                    "The static dataset exposes multiple recipes; the primary recipe is shown.",
                )
            )
        if recipe.provenance is not Provenance.STATIC_GAME_DATA:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.UNTRUSTED_PROVENANCE,
                    f"Recipe provenance is {recipe.provenance.value}.",
                )
            )
        if self.rules.verification_status is not VerificationStatus.VERIFIED:
            reasons.append(
                ActionabilityReason(
                    ReasonCode.PROVISIONAL_MECHANICS,
                    f"Mechanics ruleset {self.rules.ruleset_id} is provisional.",
                )
            )
        if context.use_focus and (recipe.base_focus_cost is None or recipe.base_focus_cost <= 0):
            reasons.append(
                ActionabilityReason(
                    ReasonCode.MISSING_FOCUS_COST,
                    "A positive base Focus cost is missing from static metadata.",
                )
            )
        if (
            context.use_focus
            and recipe.base_focus_cost is not None
            and recipe.base_focus_cost > 0
            and focus_efficiency is None
        ):
            action_label = "Refining" if recipe.action_kind is ActionKind.REFINE else "Crafting"
            reason_code = (
                ReasonCode.UNKNOWN_REFINING_SPECIALIZATION
                if recipe.action_kind is ActionKind.REFINE
                else ReasonCode.UNKNOWN_CRAFTING_SPECIALIZATION
            )
            reasons.append(
                ActionabilityReason(
                    reason_code,
                    f"{action_label} specialization is unknown for {recipe.output.display_name}.",
                )
            )
        return reasons

    @staticmethod
    def _resolve_focus_efficiency(recipe: Recipe, context: CraftingContext) -> float | None:
        if isinstance(context.profile, CraftingSkillProfile):
            resolution = context.profile.resolve(focus_skill_mapping_for_recipe(recipe))
            return resolution.focus_cost_efficiency
        return context.profile.focus_cost_efficiency

    @staticmethod
    def _resolve_station_fee_input(
        recipe: Recipe, context: CraftingContext
    ) -> tuple[float | None, str | None]:
        observation = context.station_fee_observation
        if observation is None:
            fee = context.station_usage_fee_percent
            return (
                fee,
                None
                if fee is not None
                else f"Station usage fee is unknown for {context.craft_city}.",
            )
        expected_station = station_type_for_item(recipe.output)
        if expected_station is None:
            return (
                None,
                f"Production station classification is unknown for {recipe.output.display_name}.",
            )
        if observation.station_type is not expected_station:
            return (
                None,
                f"Station usage fee is unknown for {context.craft_city} "
                f"{expected_station.display_name}; the supplied observation is for "
                f"{observation.station_type.display_name}.",
            )
        return observation.displayed_fee, None

    def _incomplete(
        self,
        recipe: Recipe,
        context: CraftingContext,
        output_quantity: int,
        missing_prices: set[str],
        reasons: list[ActionabilityReason],
        *,
        raw_material_cost: float | None,
        station_fee: float | None,
        upfront_capital_required: float | None,
    ) -> CraftResult:
        return CraftResult(
            item_id=recipe.output.item_id,
            crafts=context.crafts,
            output_quantity=output_quantity,
            raw_material_cost=raw_material_cost,
            expected_returned_material_value=None,
            effective_material_cost=None,
            station_fee=station_fee,
            total_craft_cost=None,
            gross_sale_value=None,
            market_fees=None,
            net_sale_value=None,
            profit=None,
            roi=None,
            margin=None,
            break_even_price=None,
            focus_used=None,
            focus_available=context.profile.available_focus,
            focus_shortfall=None,
            profit_without_focus=None,
            profit_with_focus=None,
            incremental_focus_profit=None,
            silver_per_focus=None,
            return_rate=None,
            ruleset_id=self.rules.ruleset_id,
            actionability=ActionabilityAssessment(tuple(reasons)),
            missing_price_item_ids=tuple(sorted(missing_prices)),
            upfront_material_cost=raw_material_cost,
            upfront_capital_required=upfront_capital_required,
            gross_material_purchase_cash=raw_material_cost,
            station_cash=station_fee,
            total_pre_revenue_cash_required=upfront_capital_required,
        )

    @staticmethod
    def _returned_material_market_value(
        recipe: Recipe,
        craft_city_prices: Mapping[str, float | None] | None,
        *,
        crafts: int,
        return_rate: float,
    ) -> float | None:
        """Informational value of expected returns where they physically appear.

        This value never enters profit. Profit continues to use acquisition cost
        basis through ``expected_returned_material_value``.
        """

        if craft_city_prices is None:
            return None
        returnable = tuple(material for material in recipe.materials if material.returnable)
        if any(craft_city_prices.get(material.item_id) is None for material in returnable):
            return None
        total = 0.0
        for material in returnable:
            price = craft_city_prices[material.item_id]
            assert price is not None
            if not _finite_number(price) or price < 0:
                raise ValueError(f"price for {material.item_id} must be finite and non-negative")
            total += material.quantity * crafts * price * return_rate
        return total


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
