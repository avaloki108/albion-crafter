from __future__ import annotations

import math
from dataclasses import dataclass

from .fees import calculate_market_fee_components
from .mechanics import CURRENT_RULES, MechanicsRules
from .models import SaleMethod


@dataclass(frozen=True, slots=True)
class ArbitrageEconomics:
    quantity: int
    source_unit_price: float
    destination_unit_price: float
    purchase_cash: float
    gross_destination_value: float
    setup_cash: float
    transaction_tax: float
    transport_cash: float
    pre_revenue_cash: float
    net_sale_proceeds: float
    effective_economic_cost: float
    expected_profit: float
    roi: float | None
    margin: float | None


def calculate_arbitrage_economics(
    source_unit_price: float,
    destination_unit_price: float,
    *,
    quantity: int = 1,
    premium: bool,
    sale_method: SaleMethod,
    transport_cash_per_unit: float = 0.0,
    rules: MechanicsRules = CURRENT_RULES,
) -> ArbitrageEconomics:
    """Calculate one independent source-buy/destination-sale transaction."""

    for name, value in (
        ("source_unit_price", source_unit_price),
        ("destination_unit_price", destination_unit_price),
        ("transport_cash_per_unit", transport_cash_per_unit),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and non-negative")
    if source_unit_price <= 0 or destination_unit_price <= 0:
        raise ValueError("arbitrage source and destination prices must be positive")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("arbitrage quantity must be a positive integer")

    purchase = float(source_unit_price) * quantity
    gross = float(destination_unit_price) * quantity
    setup, tax = calculate_market_fee_components(
        gross,
        rules.transaction_tax(premium=premium),
        rules.sell_order_setup_fee,
        sale_method,
    )
    transport = float(transport_cash_per_unit) * quantity
    net = gross - setup - tax
    profit = net - purchase - transport
    pre_revenue = purchase + setup + transport
    effective_cost = purchase + setup + tax + transport
    return ArbitrageEconomics(
        quantity,
        float(source_unit_price),
        float(destination_unit_price),
        purchase,
        gross,
        setup,
        tax,
        transport,
        pre_revenue,
        net,
        effective_cost,
        profit,
        None if effective_cost == 0 else profit / effective_cost,
        None if gross == 0 else profit / gross,
    )
