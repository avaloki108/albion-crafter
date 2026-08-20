# Static and market data sources

## Static game metadata

The explicit `albion-crafter-update-data` workflow imports maintained
[`ao-data/ao-bin-dumps`](https://github.com/ao-data/ao-bin-dumps) `items.json` plus formatted item
metadata from one pinned commit. Startup never downloads static data.

The importer reads the upstream `items.simpleitem` shape and fields including `@uniquename`,
`@craftingcategory`, `@tier`, `@enchantmentlevel`, `craftingrequirements`, and `craftresource`.
It hydrates supported Craft/Refine recipes, Item Value, Focus, output quantities, ingredient
quantities, and returnability. Enchanted upstream resource identity is normalized to the canonical
Albion Data Project form used throughout market keys—for example `T5_METALBAR_LEVEL1@1` remains
that exact canonical identity.

The offline fixture under `tests/fixtures/refining-upstream-contract/` is a reduced extract shaped
from all five real refining families across T4, T5, T6+, lower-tier refined inputs, and one
enchantment. Its `PROVENANCE.md` pins repository, commit SHA, source date, extraction date, and the
fixture's intended limits. Tests never download it.

Before catalog replacement, structural validation rejects unresolved outputs/ingredients,
nonpositive quantities, invalid identities/ranges, and partial recipe corruption. Soft count,
drop, and sentinel checks guard catastrophic upstream changes. Each attempt records source/payload
hashes, counts, validation messages, activation/force state, and timestamps. Activation is atomic;
a failure leaves the previous catalog usable.

V0.6 derives the arbitrage universe only from supported recipe outputs. Static recipe data gives
bounded identity/display/filter metadata to a trade; arbitrage economics does not pretend to have
a recipe or station.

### V0.6.1 static-coverage audit

V0.6.1 adds a bounded catalog-wide coverage classification and filter-aware matched counts. It
uses the same conservative priority as production preflight: trusted/unambiguous recipe, known
Item Value, known ingredient returnability, then verified station mapping. These are static-data
coverage categories, not missing user settings.

The audit of pinned commit `5cf2e8e9b7021f98683181fa5b0e3c64575978e4` found 9,232 recipes.
Independent raw diagnostics found 1,281 null Item Values and 2,209 outputs without a station
mapping; those sets overlap with each other and with 3,217 ambiguous upstream recipes. Under the
mutually exclusive decision-coverage priority, 3,724 recipes are supported, 3,217 are ambiguous,
1,035 first fail Item Value, and 1,256 first fail station mapping. Unknown returnability is zero
for that import. Most unmapped station outputs have a blank crafting category and represent raw,
artifact, cape/furniture-root, or other non-production records rather than station setup the
player can repair.

`T5_POTION_ACID` is mapped correctly to the Alchemist's Lab, but its pinned output record omits
`@itemvalue`; its rare direbear ingredient omits Item Value too, so material-based derivation is
not possible. The importer therefore preserves `None`—never zero—and the UI labels the recipe as
unsupported upstream static evidence rather than asking the player to invent an Item Value.

## Current market observations

Current prices come from the Albion Online Data Project regional `/api/v2/stats/prices` endpoint.
The cache keeps minimum sell and maximum buy values with independent observation timestamps plus
the separate fetch timestamp. A zero/empty side is stored as missing.

Requests are de-duplicated and split by item count and fully encoded URL length, bounded by the
application's 3,900-byte safety limit and per-operation batch count. Execution is sequential with
bounded retry for transient failures. Cancellation is checked between batches/retries. Successful
batches persist independently, and newer missing/older sides cannot overwrite a useful side.

At application startup, and on **Refresh ALL catalog prices**, the app ignores manual ID fields
and checks every canonical item in the active static catalog at Normal quality across all supported
cities. The operation computes its exact request-plan bound, reports per-batch progress, remains
cancellable, and persists each successful batch. The Market Data table renders only a useful
bounded slice of the potentially much larger cache.

A successful current-price request can still return an empty sell or buy side. That means AODP has
no player-reported top-of-book observation for that exact item/city/quality; it is not zero and the
app does not invent a price. Marketplace refresh also cannot provide upstream static Item Value,
the exact station fee displayed in the Albion client, or a player-specific Focus profile.

Find Me Money preflight computes exact sparse requirements before HTTP. Arbitrage requests source
minimum sell and either destination minimum sell (Sell Order) or maximum buy (Instant Sale).
Repeated market keys across routes are de-duplicated. Trusted exact-side user overrides live in a
separate table and never overwrite AODP cache.

Top-of-book price is not available quantity, guaranteed execution, or order depth.

## Historical reported activity

The separate `/api/v2/stats/history` client uses explicit dates, cities, qualities, and interval
scale with the same bounded batching, retry, cancellation, and per-batch persistence policy.
Intervals report sell-side activity count and average price—not side-specific current depth,
trade-price extremes, or a fill guarantee.

Coverage distinguishes populated success, empty success, partial, failed, cancelled, and never
fetched. Empty/missing evidence does not become invented volume. Find Me Money evaluates current
economics first, then de-duplicates and refreshes shortlisted production liquidation plus arbitrage
source-acquisition/destination-liquidation market keys. It groups item IDs by city instead of one
request per candidate.

The same reported activity is used as a conservative proxy for both arbitrage roles because AODP
does not provide a historical acquisition-side depth series. This limitation is retained in action
evidence and UI warnings. Craft/Refine input materials do not yet receive shared historical
acquisition-capacity constraints.
