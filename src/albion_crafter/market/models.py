from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from albion_crafter.core.freshness import Freshness as Freshness
from albion_crafter.core.freshness import FreshnessPolicy as FreshnessPolicy
from albion_crafter.core.provenance import Provenance


class Region(StrEnum):
    AMERICAS = "americas"
    EUROPE = "europe"
    ASIA = "asia"

    @property
    def api_base_url(self) -> str:
        return {
            Region.AMERICAS: "https://west.albion-online-data.com",
            Region.EUROPE: "https://europe.albion-online-data.com",
            Region.ASIA: "https://east.albion-online-data.com",
        }[self]

    @property
    def display_name(self) -> str:
        return self.value.title()


class MarketSide(StrEnum):
    SELL_ORDER = "sell_order"
    BUY_ORDER = "buy_order"


@dataclass(frozen=True, slots=True)
class MarketPrice:
    item_id: str
    city: str
    quality: int
    region: Region
    sell_price: int | None
    sell_price_timestamp: datetime | None
    buy_price: int | None
    buy_price_timestamp: datetime | None
    fetched_at: datetime
    provenance: Provenance = Provenance.AODP_LIVE

    def __post_init__(self) -> None:
        if not self.item_id or not self.city:
            raise ValueError("item_id and city are required")
        if not 1 <= self.quality <= 5:
            raise ValueError("quality must be between 1 and 5")
        for name in ("sell_price", "buy_price"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("sell_price_timestamp", "buy_price_timestamp", "fetched_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    def timestamp_for_sell_method(self, *, sell_order: bool) -> datetime | None:
        return self.sell_price_timestamp if sell_order else self.buy_price_timestamp

    def price_for_sell_method(self, *, sell_order: bool) -> int | None:
        return self.sell_price if sell_order else self.buy_price

    def timestamp_for_side(self, side: MarketSide) -> datetime | None:
        return (
            self.sell_price_timestamp if side is MarketSide.SELL_ORDER else self.buy_price_timestamp
        )

    def price_for_side(self, side: MarketSide) -> int | None:
        return self.sell_price if side is MarketSide.SELL_ORDER else self.buy_price


@dataclass(frozen=True, slots=True)
class UserPriceOverride:
    item_id: str
    city: str
    quality: int
    region: Region
    side: MarketSide
    price: int
    entered_at: datetime
    provenance: Provenance = Provenance.USER_OVERRIDE

    def __post_init__(self) -> None:
        if not self.item_id or not self.city:
            raise ValueError("item_id and city are required")
        if not 1 <= self.quality <= 5:
            raise ValueError("quality must be between 1 and 5")
        if isinstance(self.price, bool) or not isinstance(self.price, int) or self.price <= 0:
            raise ValueError("override price must be a positive integer")
        if self.entered_at.tzinfo is None:
            raise ValueError("entered_at must be timezone-aware")
