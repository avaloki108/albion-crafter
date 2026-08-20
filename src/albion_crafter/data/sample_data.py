from __future__ import annotations

from datetime import UTC, datetime

from albion_crafter.core.provenance import Provenance
from albion_crafter.market.models import MarketPrice, Region

from .cities import CITIES

_BASE_PRICES: dict[str, tuple[int, int]] = {
    "T4_MAIN_SWORD": (18_900, 15_400),
    "T5_MAIN_AXE": (74_000, 63_000),
    "T5_BAG": (31_500, 27_000),
    "T4_METALBAR": (420, 370),
    "T4_PLANKS": (350, 305),
    "T5_METALBAR": (920, 810),
    "T5_PLANKS": (790, 690),
    "T5_LEATHER": (860, 750),
    "T5_CLOTH": (780, 680),
    "T5_ARTEFACT_MAIN_AXE_KEEPER": (21_000, 17_500),
}


def sample_market_prices(now: datetime | None = None) -> list[MarketPrice]:
    """Return an isolated demo fixture rejected by the production market repository."""
    current = now or datetime.now(UTC)
    records: list[MarketPrice] = []
    for city_index, city in enumerate(CITIES):
        multiplier = 1.0 + ((city_index - 2) * 0.025)
        for item_id, (sell, buy) in _BASE_PRICES.items():
            records.append(
                MarketPrice(
                    item_id=item_id,
                    city=city,
                    quality=1,
                    region=Region.AMERICAS,
                    sell_price=round(sell * multiplier),
                    sell_price_timestamp=None,
                    buy_price=round(buy * multiplier),
                    buy_price_timestamp=None,
                    fetched_at=current,
                    provenance=Provenance.DEMO_SAMPLE,
                )
            )
    return records
