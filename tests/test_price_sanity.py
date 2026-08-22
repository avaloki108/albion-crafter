from __future__ import annotations

from datetime import UTC, datetime

import pytest

from albion_crafter.market.liquidity import LiquidityAssessment, LiquidityLevel
from albion_crafter.planning.models import PlanReasonCode
from albion_crafter.planning.price_sanity import arbitrage_price_sanity_reasons

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _liquidity(*, mean: float | None, deviation: float | None) -> LiquidityAssessment:
    return LiquidityAssessment(
        LiquidityLevel.MODERATE if mean is not None else LiquidityLevel.UNKNOWN,
        100 if mean is not None else 0,
        4 if mean is not None else 0,
        mean,
        deviation,
        None,
        NOW if mean is not None else None,
        mean,
        mean,
        (),
    )


@pytest.mark.parametrize(
    ("source", "destination", "roi"),
    (
        (_liquidity(mean=100, deviation=-0.8), _liquidity(mean=200, deviation=0.1), 1.0),
        (_liquidity(mean=100, deviation=0.1), _liquidity(mean=200, deviation=4.0), 1.0),
        (_liquidity(mean=None, deviation=None), _liquidity(mean=200, deviation=0.1), 10.0),
    ),
)
def test_extreme_arbitrage_quotes_require_in_game_verification(
    source: LiquidityAssessment,
    destination: LiquidityAssessment,
    roi: float,
) -> None:
    reasons = arbitrage_price_sanity_reasons(
        source,
        destination,
        roi=roi,
        item_id="T5_ITEM",
    )

    assert [reason.code for reason in reasons] == [PlanReasonCode.EXTREME_MARKET_OUTLIER]


def test_ordinary_arbitrage_spread_remains_eligible() -> None:
    reasons = arbitrage_price_sanity_reasons(
        _liquidity(mean=100, deviation=-0.1),
        _liquidity(mean=200, deviation=0.2),
        roi=0.5,
        item_id="T5_ITEM",
    )

    assert reasons == ()
