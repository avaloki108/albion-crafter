from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from albion_crafter.database.database import (
    Database,
    MarketPriceRepository,
    PriceOverrideRepository,
    default_database_path,
)
from albion_crafter.database.v3 import MarketHistoryRepository
from albion_crafter.market.aodp import AODPClient, MarketDataError
from albion_crafter.market.backfill import MissingSellHistoryBackfillService
from albion_crafter.market.cache import CachedMarketService
from albion_crafter.market.estimation import (
    DEFAULT_HISTORICAL_ESTIMATION_POLICY,
    estimate_historical_sell_price,
)
from albion_crafter.market.history import AODPHistoryClient, HistoryDataError, HistoryTimeScale
from albion_crafter.market.history_cache import CachedOutputHistoryService
from albion_crafter.market.models import FreshnessPolicy, MarketSide, Region
from albion_crafter.market.pricing import ResolvedPrice, resolve_price


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _resolved(line: ResolvedPrice) -> dict[str, Any]:
    return {
        "value": line.price,
        "side": line.side.value,
        "source": line.source.value,
        "confidence": line.confidence.value,
        "observed_at": _timestamp(line.observation_timestamp),
        "freshness": line.freshness.value,
        "current_price": line.current_price,
        "current_timestamp": _timestamp(line.current_timestamp),
        "current_freshness": line.current_freshness.value,
        "historical_reference_price": line.historical_reference_price,
        "historical_days_used": line.historical_days_used,
        "historical_total_volume": line.historical_total_volume,
        "historical_avg_daily_volume_7d": line.historical_avg_daily_volume_7d,
        "historical_avg_daily_volume_30d": line.historical_avg_daily_volume_30d,
        "historical_median_price": line.historical_median_price,
        "historical_volatility": line.historical_volatility,
        "historical_latest_bucket": _timestamp(line.historical_latest_bucket),
        "historical_outliers_ignored": line.historical_outliers_ignored,
    }


def inspect_market_items(
    database: Database,
    item_ids: tuple[str, ...],
    *,
    city: str,
    quality: int,
    region: Region,
    as_of: datetime,
    maximum_price_age: timedelta,
) -> dict[str, Any]:
    prices = MarketPriceRepository(database)
    overrides = PriceOverrideRepository(database)
    history_repository = MarketHistoryRepository(database)
    freshness_policy = FreshnessPolicy(maximum_price_age)
    items: list[dict[str, Any]] = []
    for item_id in item_ids:
        current = prices.get(item_id, city, quality, region)
        history = history_repository.list_for_items(
            region,
            (item_id,),
            (city,),
            quality,
            as_of - DEFAULT_HISTORICAL_ESTIMATION_POLICY.volume_lookback,
            time_scale=HistoryTimeScale.DAILY,
        )
        estimate = estimate_historical_sell_price(history, as_of=as_of)
        sides: dict[str, ResolvedPrice] = {}
        for side in (MarketSide.SELL_ORDER, MarketSide.BUY_ORDER):
            sides[side.value] = resolve_price(
                item_id=item_id,
                city=city,
                quality=quality,
                side=side,
                role="diagnostic",
                freshness_policy=freshness_policy,
                as_of=as_of,
                market_price=current,
                override=overrides.get(item_id, city, quality, region, side),
                history=history,
            )
        items.append(
            {
                "item_id": item_id,
                "city": city,
                "quality": quality,
                "current": {
                    "sell": current.sell_price if current is not None else None,
                    "sell_timestamp": (
                        _timestamp(current.sell_price_timestamp) if current is not None else None
                    ),
                    "buy": current.buy_price if current is not None else None,
                    "buy_timestamp": (
                        _timestamp(current.buy_price_timestamp) if current is not None else None
                    ),
                    "fetched_at": _timestamp(current.fetched_at) if current is not None else None,
                },
                "history": (
                    {
                        "reference_price": estimate.reference_price,
                        "confidence": estimate.confidence.value,
                        "days_used": estimate.days_used,
                        "days_available": estimate.days_available,
                        "total_volume_7d": estimate.total_volume_7d,
                        "avg_daily_volume_7d": estimate.average_daily_volume_7d,
                        "avg_daily_volume_30d": estimate.average_daily_volume_30d,
                        "median_price": estimate.median_price,
                        "volatility": estimate.volatility,
                        "latest_bucket": _timestamp(estimate.latest_bucket_at),
                        "outliers_ignored": estimate.outliers_ignored,
                    }
                    if estimate is not None
                    else None
                ),
                "resolved_sell": _resolved(sides[MarketSide.SELL_ORDER.value]),
                "resolved_buy": _resolved(sides[MarketSide.BUY_ORDER.value]),
            }
        )
    return {
        "region": region.value,
        "city": city,
        "quality": quality,
        "as_of": as_of.isoformat(),
        "items": items,
    }


def refresh_market_items(
    database: Database,
    item_ids: tuple[str, ...],
    *,
    city: str,
    quality: int,
    region: Region,
    as_of: datetime,
    history_all: bool,
) -> None:
    prices = MarketPriceRepository(database)
    CachedMarketService(AODPClient(region), prices).refresh(
        item_ids,
        cities=(city,),
        qualities=(quality,),
    )
    history_repository = MarketHistoryRepository(database)
    if history_all:
        CachedOutputHistoryService(
            AODPHistoryClient(region),
            history_repository,
        ).refresh_market_items(
            item_ids,
            start_date=(as_of - DEFAULT_HISTORICAL_ESTIMATION_POLICY.volume_lookback).date(),
            end_date=as_of.date(),
            cities=(city,),
            qualities=(quality,),
            time_scale=HistoryTimeScale.DAILY,
        )
    else:
        MissingSellHistoryBackfillService(prices, history_repository).refresh_missing(
            region,
            item_ids,
            (city,),
            quality=quality,
            as_of=as_of,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect raw current, daily history, and resolved Albion market prices."
    )
    parser.add_argument("item_ids", nargs="+", help="Canonical Albion item IDs")
    parser.add_argument("--city", default="Bridgewatch")
    parser.add_argument("--quality", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--region", choices=[value.value for value in Region], default="americas")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument("--max-age-hours", type=float, default=4.0)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh current data and only missing SELL history before inspecting.",
    )
    parser.add_argument(
        "--history-all",
        action="store_true",
        help="With --refresh, retrieve daily history for every requested item.",
    )
    arguments = parser.parse_args(argv)
    item_ids = tuple(dict.fromkeys(value.strip() for value in arguments.item_ids if value.strip()))
    if not item_ids or arguments.max_age_hours <= 0:
        parser.error("item IDs and a positive max age are required")
    database = Database(arguments.database)
    database.initialize()
    as_of = datetime.now(UTC)
    maximum_age = timedelta(hours=arguments.max_age_hours)
    try:
        if arguments.refresh:
            refresh_market_items(
                database,
                item_ids,
                city=arguments.city,
                quality=arguments.quality,
                region=Region(arguments.region),
                as_of=as_of,
                history_all=arguments.history_all,
            )
        result = inspect_market_items(
            database,
            item_ids,
            city=arguments.city,
            quality=arguments.quality,
            region=Region(arguments.region),
            as_of=as_of,
            maximum_price_age=maximum_age,
        )
    except (MarketDataError, HistoryDataError, OSError, ValueError) as exc:
        print(f"Market inspection failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
