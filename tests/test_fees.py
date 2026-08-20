import pytest

from albion_crafter.core.fees import calculate_market_fees, calculate_station_fee
from albion_crafter.core.models import SaleMethod


def test_sell_order_marketplace_fees_include_setup_and_tax() -> None:
    assert calculate_market_fees(10_000, 0.04, 0.025, SaleMethod.SELL_ORDER) == 650


def test_instant_sell_omits_setup_fee() -> None:
    assert calculate_market_fees(10_000, 0.04, 0.025, SaleMethod.INSTANT_SELL) == 400


def test_station_fee_uses_the_number_displayed_in_albion_ui() -> None:
    assert calculate_station_fee(100, 500, item_count=3) == pytest.approx(168.75)


def test_non_premium_transaction_tax_reference() -> None:
    assert calculate_market_fees(10_000, 0.08, 0.025, SaleMethod.SELL_ORDER) == 1_050


def test_fee_inputs_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        calculate_market_fees(100, -0.01, 0.02)
