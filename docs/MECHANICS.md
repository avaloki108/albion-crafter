# V0.6 production and market mechanics

`CURRENT_RULES` is `albion-2026-08-crafting-refining-arbitrage-v4`, checked 2026-08-19. The
ruleset retains separately status-bearing components for return rate, crafting/refining city
bonuses, Focus production bonus, crafting/refining FCE mappings, marketplace fees, and station
fees. Arbitrage does not invent a new Albion mechanic; it composes verified fee, price-side,
freshness, history, and transport policies.

## Production bonuses and returns

Craft and Refine dispatch through action-aware city datasets. Unsupported city/category/family
evidence remains unknown. Refining covers the five canonical resource families and their verified
Royal-city baselines/specialties. Focus adds the versioned +59 percentage-point production bonus.

Return rate is computed once as:

```text
RRR = 1 - 1 / (1 + total production bonus)
```

Only explicitly returnable ingredients contribute. Expected returns reduce economic cost using
the original acquisition-cost basis; they never reduce gross upfront purchase cash or supply a
second action.

## Focus Cost Efficiency

Focus cost is:

```text
base recipe Focus × 0.5 ** (effective FCE / 10,000)
```

Crafting mappings use the reviewed crafting trees. Refining uses a family-and-tier T4–T8 mapping:
all reported nodes in the family contribute 30 FCE per level and the matching tier contributes an
additional 250 per level. Enchantments share their base-tier node. Unknown FCE disables only the
focused production mode. Arbitrage never consumes Focus and does not carry FCE evidence.

## Marketplace price and fee policy

Material purchases and arbitrage source purchases use current minimum sell. Sell-order liquidation
uses destination minimum sell; instant liquidation uses destination maximum buy. Sell Order pays
the ruleset setup rate and Premium-aware transaction tax. Instant Sale pays transaction tax only.

The common fee primitive returns `(setup cash, transaction tax)`. Setup is required before
sell-order revenue. Tax is deducted at sale, affects economic cost/profit, and is not counted again
as pre-revenue cash.

Arbitrage uses:

```text
pre-revenue cash  = purchase + setup + explicit transport
net proceeds      = gross destination value - setup - transaction tax
expected profit   = net proceeds - purchase - explicit transport
ROI               = profit / (purchase + setup + tax + transport)
margin            = profit / gross destination value
```

All currency outputs are conservatively quantized by the candidate/action boundary. Nonpositive,
stale, future, malformed, or untrusted required prices do not become candidates.

## Station and transport

Production station usage uses Item Value, the exact user-entered displayed usage percentage, the
versioned nutrition factor, and batch count. Missing/stale observations block unless stale use is
explicitly allowed as Advisory. Arbitrage has no station.

Transport policy is generic. Local Only permits local production but no distinct-city arbitrage.
Acknowledged Uncosted adds a visible advisory. Explicit Cost is charged exactly once per production
batch or arbitraged unit according to the action's generic transport allocation.

## Cash and optimizer boundary

Every selected action must fit the initial spendable silver and Focus after protected reserves.
No output revenue, arbitrage proceeds, or expected return is recycled inside a plan. History-based
market capacity is a conservative execution proxy and remains separate from the current
top-of-book quote.
