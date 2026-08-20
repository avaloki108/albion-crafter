import pytest

from albion_crafter.core.returns import (
    calculate_effective_material_cost,
    calculate_expected_material_return,
    calculate_return_rate,
)


def test_return_rate_from_production_bonus() -> None:
    assert calculate_return_rate(0) == 0
    assert calculate_return_rate(0.18) == pytest.approx(0.153, abs=0.0005)
    assert calculate_return_rate(0.33) == pytest.approx(0.248, abs=0.0005)
    assert calculate_return_rate(0.77) == pytest.approx(0.435, abs=0.0005)
    assert calculate_return_rate(0.92) == pytest.approx(0.479, abs=0.0005)


def test_expected_and_effective_material_return() -> None:
    assert calculate_expected_material_return(1_000, 0.25) == 250
    assert calculate_effective_material_cost(1_000, 500, 0.25) == 1_250


def test_return_rate_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        calculate_return_rate(-0.01)
    with pytest.raises(ValueError):
        calculate_expected_material_return(100, 1.0)
