from __future__ import annotations

import math

from .models import SaleMethod


def calculate_station_fee(
    item_value: float,
    station_usage_fee_percent: float,
    *,
    station_nutrition_factor: float = 0.1125,
    item_count: int = 1,
) -> float:
    """Calculate usage cost from the percentage shown in Albion's station UI."""
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in (item_value, station_usage_fee_percent, station_nutrition_factor)
        )
        or isinstance(item_count, bool)
        or not isinstance(item_count, int)
        or item_count < 1
    ):
        raise ValueError("station fee inputs must be finite/non-negative and crafts positive")
    return item_value * station_nutrition_factor * (station_usage_fee_percent / 100.0) * item_count


def calculate_market_fees(
    gross_sale_value: float,
    transaction_tax_rate: float,
    setup_fee_rate: float,
    sale_method: SaleMethod = SaleMethod.SELL_ORDER,
) -> float:
    _validate_market_fee_inputs(gross_sale_value, transaction_tax_rate, setup_fee_rate)
    effective_rate = transaction_tax_rate
    if sale_method is SaleMethod.SELL_ORDER:
        effective_rate += setup_fee_rate
    return gross_sale_value * effective_rate


def calculate_market_fee_components(
    gross_sale_value: float,
    transaction_tax_rate: float,
    setup_fee_rate: float,
    sale_method: SaleMethod = SaleMethod.SELL_ORDER,
) -> tuple[float, float]:
    """Return ``(listing/setup cash, transaction tax)``.

    The listing fee is paid before sell-order revenue arrives. Transaction tax
    is deducted at sale and therefore affects economics but not pre-revenue
    cash requirements.
    """

    _validate_market_fee_inputs(gross_sale_value, transaction_tax_rate, setup_fee_rate)
    setup = gross_sale_value * setup_fee_rate if sale_method is SaleMethod.SELL_ORDER else 0.0
    return setup, gross_sale_value * transaction_tax_rate


def _validate_market_fee_inputs(*values: float) -> None:
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in values
    ):
        raise ValueError("market fee inputs must be finite and non-negative")
