from __future__ import annotations

import math


def calculate_focus_cost(base_focus_cost: float, focus_cost_efficiency: float) -> float:
    """Apply the prototype's halving-per-10,000 focus-efficiency curve."""
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in (base_focus_cost, focus_cost_efficiency)
    ):
        raise ValueError("focus values must be finite and non-negative")
    return base_focus_cost * (0.5 ** (focus_cost_efficiency / 10_000.0))


def calculate_silver_per_focus(incremental_profit: float, focus_used: float) -> float | None:
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (incremental_profit, focus_used)
        )
        or focus_used < 0
    ):
        raise ValueError("silver-per-Focus inputs must be finite and Focus cannot be negative")
    return None if focus_used == 0 else incremental_profit / focus_used
