from __future__ import annotations

from albion_crafter.market.liquidity import LiquidityAssessment

from .models import PlanReason, PlanReasonCode

# Top-of-book is one reported order, not executable depth.  These deliberately
# broad limits reject only recommendations whose apparent upside needs direct
# in-game corroboration; ordinary thin or volatile markets remain advisory.
MAX_CORROBORATED_SALE_PRICE_DEVIATION = 2.0
MAX_CORROBORATED_ACQUISITION_DISCOUNT = 2 / 3
MAX_UNCORROBORATED_ROI = 5.0


def production_price_sanity_reasons(
    liquidity: LiquidityAssessment | None,
    *,
    roi: float | None,
    item_id: str,
) -> tuple[PlanReason, ...]:
    """Block an implausible sale quote until the player verifies it in game."""

    if liquidity is None:
        # Initial evaluation intentionally runs before history enrichment.
        return ()
    deviation = liquidity.current_price_deviation
    if deviation is not None and deviation > MAX_CORROBORATED_SALE_PRICE_DEVIATION:
        return (
            PlanReason(
                PlanReasonCode.EXTREME_MARKET_OUTLIER,
                f"{item_id} current sell price is {deviation + 1:,.1f}x its recent "
                "reported-history mean. Top-of-book depth is unknown; verify the price in "
                "Albion before treating this opportunity as executable.",
            ),
        )
    if liquidity.weighted_mean_price is None and roi is not None and roi > MAX_UNCORROBORATED_ROI:
        return (
            PlanReason(
                PlanReasonCode.EXTREME_MARKET_OUTLIER,
                f"{item_id} implies {roi:.0%} ROI, but no usable output history corroborates "
                "the top-of-book sell price. Verify the price and available quantity in "
                "Albion before acting.",
            ),
        )
    return ()


def arbitrage_price_sanity_reasons(
    source: LiquidityAssessment | None,
    destination: LiquidityAssessment | None,
    *,
    roi: float | None,
    item_id: str,
) -> tuple[PlanReason, ...]:
    """Block an arbitrage spread built from an extreme optimistic quote."""

    if source is None or destination is None:
        return ()
    source_deviation = source.current_price_deviation
    destination_deviation = destination.current_price_deviation
    if source_deviation is not None and source_deviation < -MAX_CORROBORATED_ACQUISITION_DISCOUNT:
        return (
            PlanReason(
                PlanReasonCode.EXTREME_MARKET_OUTLIER,
                f"{item_id} source sell price is more than "
                f"{MAX_CORROBORATED_ACQUISITION_DISCOUNT:.0%} below its recent "
                "reported-history mean. Verify that acquisition price and depth in Albion.",
            ),
        )
    if (
        destination_deviation is not None
        and destination_deviation > MAX_CORROBORATED_SALE_PRICE_DEVIATION
    ):
        return (
            PlanReason(
                PlanReasonCode.EXTREME_MARKET_OUTLIER,
                f"{item_id} destination sell price is "
                f"{destination_deviation + 1:,.1f}x its recent reported-history mean. "
                "Verify that sale price and depth in Albion.",
            ),
        )
    if (
        (source.weighted_mean_price is None or destination.weighted_mean_price is None)
        and roi is not None
        and roi > MAX_UNCORROBORATED_ROI
    ):
        return (
            PlanReason(
                PlanReasonCode.EXTREME_MARKET_OUTLIER,
                f"{item_id} implies {roi:.0%} arbitrage ROI without complete source and "
                "destination history. Verify both prices and available quantities in Albion.",
            ),
        )
    return ()
