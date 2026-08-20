# Static and market data sources

## Static game metadata

The explicit `albion-crafter-update-data` workflow imports maintained
[`ao-data/ao-bin-dumps`](https://github.com/ao-data/ao-bin-dumps) `items.json` plus formatted item
metadata from one pinned commit. Startup never downloads static data.

The importer walks every upstream root item collection, including `items.simpleitem` and
`items.consumableitem`, and reads fields including `@uniquename`, `@craftingcategory`, `@tier`,
`@enchantmentlevel`, `craftingrequirements`, and `craftresource`.
It hydrates supported Craft/Refine recipes, Item Value, Focus, output quantities, ingredient
quantities, and returnability. A direct root `@itemvalue` is authoritative and is never replaced.
When a craftable output omits that attribute, the importer resolves the value through the recipe
graph to a fixed point: known ingredient Item Values are multiplied by their counts, summed, and
divided by `@amountcrafted`. An ingredient with no Item Value is allowed to contribute zero only
when its recipe entry explicitly has `@maxreturnamount="0"`; other unknown inputs keep the output
unresolved. This is static-data processing and does not use market prices. Enchanted upstream
resource identity is normalized to the canonical Albion Data Project form used throughout market
keys—for example `T5_METALBAR_LEVEL1@1` remains that exact canonical identity.

The offline fixtures under `tests/fixtures/` pin the refining contract and the exact Acid Potion
source fragment. The refining extract covers all five real families across T4, T5, T6+,
lower-tier inputs, and one enchantment. Each `PROVENANCE.md` records the repository, commit SHA,
source date, extraction date, and intended limits. Tests never download them.

Before catalog replacement, structural validation rejects unresolved outputs/ingredients,
nonpositive quantities, invalid identities/ranges, duplicate canonical IDs, invalid finite numeric
fields, and partial recipe corruption. Soft count, drop, and sentinel checks guard catastrophic
upstream changes. Each attempt records source/payload hashes, counts, validation messages,
activation/force state, and timestamps. Activation is atomic; a failure leaves the previous
catalog usable.

V0.6 derives the arbitrage universe only from supported recipe outputs. Static recipe data gives
bounded identity/display/filter metadata to a trade; arbitrage economics does not pretend to have
a recipe or station.

### V0.6.2 static-coverage audit

V0.6.2 retains the bounded catalog-wide coverage classification and filter-aware matched counts. It
uses the same conservative priority as production preflight: trusted/unambiguous recipe, known
Item Value, known ingredient returnability, then verified station mapping. These are static-data
coverage categories, not missing user settings.

The audit of pinned commit `5cf2e8e9b7021f98683181fa5b0e3c64575978e4` found 11,805 items,
9,232 recipes, and 19,419 ingredient rows. Of the recipe outputs, 1,139 preserve a direct
`@itemvalue`, 8,058 are resolved from recipes, and 35 remain unresolved. Under the mutually
exclusive decision-coverage priority, 4,138 recipes are supported, 3,217 are ambiguous, 33 first
fail Item Value, and 1,844 first fail station mapping. Unknown returnability is zero for that
import. Most unmapped station outputs have a blank crafting category and represent raw, artifact,
cape/furniture-root, or other non-production records rather than station setup the player can
repair.

`T5_POTION_ACID` is one root `consumableitem`, correctly mapped to the Alchemist's Lab. Both JSON
and XML omit its direct Item Value and declare a batch of 10 using one nonreturnable
`T5_ALCHEMY_RARE_DIREBEAR`, 48 `T5_TEASEL`, 24 `T4_BURDOCK`, and 12 `T4_MILK`. The rare component
also has no Item Value; because its recipe row explicitly has `maxreturnamount=0`, it contributes
zero to the derived static value while remaining a required market purchase. The other three
inputs each have direct Item Value 40, so the output Item Value is
`(48 * 40 + 24 * 40 + 12 * 40) / 10 = 336`.

Run `albion-crafter-inspect-item ITEM_ID` to print what the active importer persisted and, when
the pinned cache is present, the matching raw source record and whether Item Value was direct,
recipe-derived, or unavailable.

## Current market observations

Current prices come from the Albion Online Data Project regional `/api/v2/stats/prices` endpoint.
The cache keeps minimum sell and maximum buy values with independent observation timestamps plus
the separate fetch timestamp. A zero/empty side is stored as missing.

Requests are de-duplicated and split by item count and fully encoded URL length, bounded by the
application's 3,900-byte safety limit and per-operation batch count. Execution is sequential with
bounded retry for transient failures. Cancellation is checked between batches/retries. Successful
batches persist independently, and newer missing/older sides cannot overwrite a useful side.

Application startup performs no network requests. The explicit **REFRESH ROYAL MARKETS** action
ignores manual ID fields and checks the intentional market universe at Normal quality across the
selected Royal cities. That universe consists of all supported Craft/Refine outputs plus their
required ingredients; unsupported raw catalog noise is excluded. The default city set is the five
outer Royals, with optional Caerleon. Brecilien is not part of this synchronization.

The operation computes its exact request-plan bound, reports per-batch progress, remains
cancellable, and persists each successful batch before continuing. Batches contain at most 100
item IDs and their complete encoded URLs stay within 3,900 bytes. Execution is sequential; 429
and other transient failures use the existing bounded retry policy. The Market Data table renders
only a bounded slice of the potentially much larger cache, while the reusable coverage service
reports the complete selected-universe age distribution.

A successful current-price request can still return an empty sell or buy side. That means AODP has
no player-reported top-of-book observation for that exact item/city/quality; it is not zero and the
app does not invent a current price. The selected-recipe refresh then batches only missing or stale
required SELL keys into the daily history endpoint. Marketplace refresh also cannot provide
upstream static Item Value, the exact station fee displayed in the Albion client, or a
player-specific Focus profile.

Find Me Money preflight computes exact sparse requirements before HTTP. Arbitrage requests source
minimum sell and either destination minimum sell (Sell Order) or maximum buy (Instant Sale).
Repeated market keys across routes are de-duplicated. Trusted exact-side user overrides live in a
separate table and never overwrite AODP cache.

Top-of-book price is not available quantity, guaranteed execution, or order depth.

The current-price source remains AODP directly. Albion Crafter does not scrape third-party market
websites. A possible V0.7+ enhancement is a continuous AODP NATS listener; V0.6.2 deliberately
implements only explicit REST synchronization.

## Historical reported activity

The separate `/api/v2/stats/history` client uses explicit dates, cities, qualities, and interval
scale with the same bounded batching, retry, cancellation, and per-batch persistence policy.
Intervals report sell-side activity count and average price—not side-specific current depth,
trade-price extremes, or a fill guarantee.

### Historical SELL-price resolution

Calculator and Craft Scanner SELL resolution uses this fixed precedence:

1. an exact trusted user override, when one exists;
2. a reasonably fresh current minimum sell observation;
3. a city/quality-specific estimate from recent daily AODP sell history;
4. a preserved but explicitly stale/future/untimestamped current observation when no history can
   replace it; or
5. missing.

BUY resolution never uses history: it remains exact override, current maximum buy, or missing.
No other city's price is substituted. Raw current rows and raw history intervals remain in their
separate tables; an estimate is constructed at resolution time and is never written into
`sell_price_min`.

The estimate starts with up to seven recent usable UTC daily buckets. It computes an unweighted
median, rejects price points beyond the larger of 50% median deviation or three times the median
absolute relative deviation, caps each retained day's volume weight at three times median daily
volume, then takes the weighted median. This prevents one abnormal price or volume day from
dominating while retaining `item_count` as the activity signal. Evidence exposes the reference
and median prices, usable/ignored days, seven-day total and average daily volume, optional 30-day
average volume, latest bucket, and weighted median absolute relative deviation.

Confidence is deterministic. `LIVE` means a usable current observation. A historical estimate is
`HIGH` with at least five retained days, at least 50 reported items, no more than 25% volatility,
and activity within two days; `MEDIUM` requires at least three days, 10 items, no more than 60%
volatility, and activity within four days. Other usable history is `LOW`; no usable source is
`MISSING`. Historical estimates are visible advisory warnings, not current top-of-book claims.

Coverage distinguishes populated success, empty success, partial, failed, cancelled, and never
fetched. Empty/missing evidence does not become invented volume. Find Me Money evaluates current
economics first, then de-duplicates and refreshes shortlisted production liquidation plus arbitrage
source-acquisition/destination-liquidation market keys. It groups item IDs by city instead of one
request per candidate.

The same reported activity is used as a conservative proxy for both arbitrage roles because AODP
does not provide a historical acquisition-side depth series. This limitation is retained in action
evidence and UI warnings. Craft/Refine input materials do not yet receive shared historical
acquisition-capacity constraints.

Run `albion-crafter-inspect-market ITEM_ID --city Bridgewatch` to inspect cached raw and resolved
evidence. Add `--refresh` to fetch current data plus history only for missing/stale SELL keys, or
`--history-all` with `--refresh` when an explicit diagnostic needs history comparisons for every
requested item.
