from __future__ import annotations

import math


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def calculate_return_rate(total_production_bonus: float) -> float:
    """Convert a production bonus into a resource return rate.

    This formula comes from the prototype workbook and remains an isolated,
    configurable game-mechanic assumption pending periodic verification.
    """
    if not _finite(total_production_bonus) or total_production_bonus < 0:
        raise ValueError("total_production_bonus must be finite and non-negative")
    return 1.0 - (1.0 / (1.0 + total_production_bonus))


def calculate_expected_material_return(
    returnable_material_value: float, return_rate: float
) -> float:
    if not _finite(returnable_material_value) or returnable_material_value < 0:
        raise ValueError("returnable_material_value must be finite and non-negative")
    if not _finite(return_rate) or not 0 <= return_rate < 1:
        raise ValueError("return_rate must be at least 0 and less than 1")
    return returnable_material_value * return_rate


def calculate_effective_material_cost(
    returnable_material_value: float,
    non_returnable_material_value: float,
    return_rate: float,
) -> float:
    if not _finite(non_returnable_material_value) or non_returnable_material_value < 0:
        raise ValueError("non_returnable_material_value must be finite and non-negative")
    returned = calculate_expected_material_return(returnable_material_value, return_rate)
    return returnable_material_value - returned + non_returnable_material_value
