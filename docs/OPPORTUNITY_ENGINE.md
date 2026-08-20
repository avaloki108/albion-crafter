# Opportunity scanner

The Craft Scanner remains the focused, independent crafting-comparison workflow. V0.6 does not
silently turn it into the unified planner. Find Me Money is the bankroll surface for Craft, Refine,
and Market Arbitrage; the scanner remains useful for inspecting crafting scenarios only.

## Scan lifecycle

Opening the page, editing filters, or loading cached data performs no HTTP. An explicit scan
bulk-loads recipes, market rows, overrides, station observations, profile data, and optional
history, then evaluates scenarios in memory. Refresh remains separately explicit and sparse.
Cancellation and widget/window shutdown are distinct lifecycle paths; completed safe cache writes
remain valid, while stale worker results cannot replace a newer request identity.

## Shared evidence policy

The scanner and V0.6 planner share the same trusted current-price resolver:

- material purchase uses minimum sell;
- sell-order output uses minimum sell plus setup/tax;
- instant output uses maximum buy plus tax and no setup;
- missing/zero sides are missing, never profitable zero inputs;
- exact user overrides take precedence without mutating AODP cache.

They also share station, return, Focus, city-bonus, fee, freshness, and provenance primitives. The
scanner does not evaluate arbitrage routes; `core/arbitrage.py` reuses the fee primitives rather
than copying their constants.

## Liquidity and capital boundary

Reported history is a conservative activity/liquidity signal, not live order depth or a fill
guarantee. The scanner keeps its own bounded scenario quantities. Find Me Money additionally
enforces generic shared acquisition/liquidation capacity across selected actions.

Gross purchases, station fees, listing setup, and explicit transport must fit initial capital.
Transaction tax is deducted at sale. Expected returns or revenue do not fund another scenario or
plan action.

## Performance boundary

All repository reads are bulked and SQLite parameter chunks are bounded. Network clients batch by
item count and encoded URL length, execute sequentially, retry only bounded transient failures,
and persist successful batches independently. Neither workflow performs HTTP or SQL per candidate.
