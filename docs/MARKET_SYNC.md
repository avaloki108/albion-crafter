# Royal Market Sync V0.6.2

Royal Market Sync is the explicit broad-cache workflow behind **Market Data → REFRESH ROYAL
MARKETS**. It prepares the local cache for blank Find Me Money discovery, Craft Scanner, and
outer-Royal arbitrage without changing their economic formulas. Application startup remains
offline, and neither Find Me Money nor the Production Calculator silently starts this operation.

## Intentional market universe

The universe is derived from the active static catalog. A production recipe is supported only
when it has trusted static provenance, an unambiguous recipe, known Item Value, known ingredient
returnability, and a verified station mapping. The sync includes:

- every supported Craft or Refine output, which is also the bounded arbitrage output universe;
- every ingredient required to price those supported recipes; and
- each canonical item ID only once, preserving enchantments such as `@1`.

Raw catalog records unrelated to supported production are excluded. The Advanced market-universe
inspection lists canonical ID, name, tier, enchantment, and every reason an item is included. A
new static import invalidates the derived universe but does not delete healthy market prices.

## Royal cities and batching

The default synchronized cities are Bridgewatch, Fort Sterling, Lymhurst, Martlock, and Thetford.
Caerleon is optional and disabled by default. Brecilien is excluded. This does not add Caerleon to
the planner's outer-Royal arbitrage routes.

Current-price requests use [AODP's published regional API](https://www.albion-online-data.com/api)
`/api/v2/stats/prices` endpoint directly. IDs are
de-duplicated and placed into deterministic sequential batches with both limits enforced:

- no more than 100 canonical item IDs per request; and
- no more than 3,900 bytes in the complete encoded URL.

All selected cities are requested together when the URL bound permits it. There is no parallel
fan-out and no request per item or city/item pair. HTTP 429 and transient failures use a bounded
retry policy; permanent failures are reported. AODP publishes limits of 180 requests per minute
and 300 per five minutes; the precomputed default full-sync request count stays comfortably below
both for the current-order pass. Successful batches are merged and committed before the next
batch. Cancellation stops before another request and keeps completed work.

After the current pass, the sync groups only missing current SELL keys by city and requests daily
AODP history in the same bounded sequential batches. History remains in its own cache; the raw
current table is never filled with an estimate. The result reports how many missing SELL keys
produced a labeled historical estimate and how many had no usable retained history. Missing BUY
orders remain missing because AODP history is SELL activity.

## Timestamp-honest cache semantics

AODP is crowd-sourced. A refresh asks for the latest observation AODP currently knows; it does not
cause a player to upload a newer order. Each market row therefore retains separate evidence:

- sell minimum and its AODP observation timestamp;
- buy maximum and its independent AODP observation timestamp; and
- fetch timestamp, meaning only when Albion Crafter checked AODP.

Downloading a five-hour-old observation now leaves it five hours old. Zero or empty AODP sides
mean missing—not a zero-silver order. A missing or older incoming side cannot erase a newer useful
cached side. Manual observations remain in the separate `USER_OVERRIDE` store with their actual
entry time and are never overwritten by AODP synchronization.

The full sync saves valid observations regardless of age. Nonzero current market observations stay
usable; age is an advisory shown to the player rather than a calculation gate. The coverage
dashboard still reports explicit 2-hour, 4-hour, 24-hour, and older windows for transparency,
including missing sides and rows with no usable current order.

## How workflows use the cache

1. Open Market Data and choose the Royal cities.
2. Press **REFRESH ROYAL MARKETS** and allow bounded background batches to finish, or cancel after
   completed data has been saved.
3. Run Find Me Money with a blank item search for broad Craft/Refine/Arbitrage discovery.
4. The planner performs its existing sparse refresh only for exact required keys still missing or
   outside the selected freshness window.

The Production Calculator remains targeted to one selected recipe. Craft Scanner consumes the
same current and history caches and links to Royal Market Sync when zero actionable results are
dominated by genuinely missing data. A broad sync is an accelerator, not an actionability
guarantee: AODP may have no current order or retained SELL history, and top-of-book price is not
live order-book depth or available quantity.

## Persisted metadata and future scope

The existing market-price table remains the source of current observations. Small sync preferences
and the last compact result are stored through existing settings metadata, so SQLite stays schema
V4 and response bodies are not duplicated.

V0.6.2 does not scrape third-party market websites and does not include a live NATS listener. A
possible V0.7+ feature is a continuous AODP market listener that updates this same cache while the
application runs.
