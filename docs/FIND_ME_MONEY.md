# Find Me Money V0.6.1

Find Me Money is the GUI-independent bankroll planner for Craft, Refine, and Market Arbitrage. It
turns explicit constraints and locally retained evidence into one immutable, independently
validated plan. Construction and preflight perform no HTTP; network work starts only after an
explicit player action.

## Simple Mode and Advanced Mode

Simple Mode is the default application page and the default Find Me Money presentation. The
player enters available silver, an optional reserve, home city, Premium/Focus choice, optional
item search, and a named trust preset, then presses **FIND ME MONEY** once. That action runs the
same V0.6 preflight and execution services used by Advanced Mode; it does not introduce a second
calculator or a different price policy.

The shared saved action selection may include Craft, Refine, and Arbitrage. Home city drives the
ordinary production city fields; configured outer-Royal arbitrage scope and city sets remain
intact. Focus is unavailable until a usable saved profile exists, but non-Focus choices continue.

If a required station fee is missing/stale, Simple Mode stops before market HTTP and asks inline
for the exact displayed Albion fee. If sparse AODP refresh still leaves a required side
missing/stale/invalid, it offers an exact-side, exact-city user override inline. Both are persisted
with user provenance and timestamp, then the full preflight is rebuilt before planning continues.

The three trust presets are transparent bundles of existing V0.6 controls:

- Fast / broad: 24-hour prices, stale-station allowance, no history refresh, unknown liquidity
  accepted.
- Careful: 4-hour prices, 24-hour station fees, history enabled, 20% historical-volume cap,
  unknown liquidity accepted.
- Strict: 2-hour prices, 12-hour station fees, history enabled, 10% historical-volume cap, at
  least Moderate liquidity.

Advanced Mode retains the independent action toggles, full route/city universe, transport and
sale policies, precise freshness/threshold/cap controls, explicit two-stage preflight/run buttons,
raw evidence counts, near misses, snapshots, and exports.

Simple zero-action outcomes are not conflated: **Setup Required** is player-only station evidence;
**Not Enough Data to Know** is unresolved required market evidence; **No Profit Found** is used
only after complete price evidence produced fully priced candidates but no selectable allocation.
Unsupported static recipes receive a separate supported-data explanation and are never presented
as an Item Value field the player should guess.

## Inputs and bounded universe

`FindMoneyConstraints` records starting silver/Focus, protected reserves, Premium, region, filters,
freshness, sale method, transport, minimum profit/ROI/liquidity, explicit quantity cap, historical
volume share, and selected action kinds.

Production uses material, production, and sell cities plus the five refining-family filters.
Arbitrage adds a scope—All Production Outputs, Crafted Outputs, or Refined Resources—and separate
source/destination subsets of the five outer Royal cities. Same-city pairs are discarded.
Caerleon, Brecilien, Black Market, Outlands, hideouts, and rest cities are outside V0.6.

The arbitrage item universe is not every Albion item. It is the de-duplicated output set of the
currently imported supported recipes after query, tier, enchantment, category/family, and scope
filters.

## Network-free preflight and staged run

1. Preflight bulk-loads static data, station observations, the shared Focus profile, current cache,
   overrides, and optional history coverage. It builds legal production and arbitrage routes,
   exact price-side requirements, cached/missing/stale/future counts, production-only station/FCE
   gaps, history-capacity keys, component/work estimates, and current AODP request batches.
2. Explicit current refresh requests only required stale/missing keys. Complete batches persist
   independently after a later failure or cancellation.
3. Initial evaluation computes production modes and fee-aware one-unit arbitrage economics.
4. Safe preprocessing removes only proven dominated/equivalent routes with identical capacity
   semantics.
5. A bounded shortlist is selected after current economics. Source and destination history keys
   are de-duplicated, grouped by city, and refreshed in bounded batches.
6. Final evaluation applies history/liquidity evidence and rechecks current freshness.
7. Quantity construction and optimization enforce silver, Focus, and every shared market capacity.
8. Independent validation recomputes resources, arbitrage fees, immutable evidence, and aggregate
   capacity before a snapshot can be treated as actionable.
9. A new format-3 snapshot is persisted; cancellation never creates a partial plan.

One aware UTC `as_of` value is used per phase. Large route, graph, evaluation, shortlist,
optimization, validation, and UI loops contain cancellation checks. Repository reads are bulked;
there is no SQL or HTTP request per candidate.

## Exact current-price policy

- Production inputs: current minimum sell in the material city.
- Production sell order: current minimum sell in the destination.
- Production instant sale: current maximum buy in the destination.
- Arbitrage source: current minimum sell, always.
- Arbitrage sell order: destination minimum sell.
- Arbitrage instant sale: destination maximum buy.

Trusted exact-side user overrides retain precedence without overwriting AODP cache rows. A zero or
empty side is missing. Missing, stale, materially future-dated, malformed, or untrusted required
evidence is never replaced by zero or the opposite market side.

## Arbitrage economics and evidence

For quantity `q`:

```text
purchase cash       = source min-sell × q
gross sale          = selected destination side × q
setup cash          = setup rate × gross sale for Sell Order, otherwise 0
transaction tax     = Premium-aware tax rate × gross sale
transport cash      = explicit cost × q, or 0 only when explicitly acknowledged uncosted
pre-revenue cash    = purchase + setup + transport
net sale proceeds   = gross sale - setup - transaction tax
expected profit     = net sale proceeds - purchase - transport
```

The setup fee appears once. Transaction tax affects proceeds/economic cost but is not a second
upfront purchase. Arbitrage has Focus 0, no station, no returns, and N/A RRR/FCE. Retained evidence
includes both prices/sides/timestamps/provenance/freshness, Premium and fee components, transport,
accounting, ruleset, and both capacity envelopes.

`LOCAL_ONLY` generates no cross-city trade. `ACKNOWLEDGED_UNCOSTED` makes a feasible trade
Advisory. `EXPLICIT_COST` charges the configured amount once per moved item.

## Generic shared capacity

`CapacityRequirement` has a market `ExecutionCapacityKey`, an Acquisition or Liquidation role,
and units consumed per action unit.

- Craft/Refine normally has one Liquidation requirement; units per batch equal actual output.
- Arbitrage has one Acquisition source and one Liquidation destination requirement; each consumes
  one unit per traded item.

Capacity keys are `(region, canonical item, city, quality)` and intentionally omit action kind.
Thus Craft or Refine output and Arbitrage into the same city consume one combined destination
ceiling. Multiple trade routes sharing a source likewise consume one combined source ceiling.

History is reported market activity, not side-specific live depth. A configured fraction of a
reliable positive reported volume is a conservative unit ceiling. An explicitly derived zero
ceiling permits no allocation. Empty, missing, or unreliable history never becomes unlimited;
the finite explicit user cap is retained as a labeled fallback and unknown history keeps the action
Advisory rather than Decision Grade.

## Multi-capacity optimizer and exactness

Candidates and capacity keys form a bipartite graph. Capacity-connected candidates are solved
together so no route can reserve a shared source/destination independently. Each component builds
a Pareto frontier over cash, Focus, profit, capacity use, liquidity, and deterministic signature.
Component frontiers then combine under the one global bankroll and Focus budget.

The primary objective is expected profit, followed deterministically by lower cash, lower Focus,
stronger minimum liquidity, fewer actions, and canonical signature. Revenue and returned resources
never finance another selected action.

The default frontier limit is 2,000 states; quantity and portfolio transition limits are 2,000,000.
Any candidate, quantity, component, shortlist, or portfolio trimming that can remove the true
optimum produces **Approximate** with named diagnostics. **Exact** means those bounds did not remove
an option in the evaluated evidence universe. Both statuses always enforce feasibility.

The standalone independent check is:

```bash
uv run python scripts/verify_multicapacity_optimizer.py
```

It uses direct Cartesian brute force rather than the component/Pareto algorithm.

## Validation, UI, and exclusions

`validate_plan` does not trust candidate construction. For Arbitrage it independently verifies
identity, distinct permitted cities, source/destination sides and freshness, fees, cash timing,
profit/ROI/margin, zero Focus/station state, both requirements/evidence rows, aggregate capacities,
budgets, reserves, and transport. Corrupted capacity evidence makes the plan Non-Actionable.

The plan table labels each action. Arbitrage details say BUY, MOVE, SELL, FEES, EXPECTED, and
EXECUTION EVIDENCE from the snapshot—never a fresh render-time quote. Near misses retain reasons
such as missing/stale sides, fees erasing the spread, policy thresholds, or zero capacity.

V0.6 deliberately does not add shared historical acquisition capacity for Craft/Refine input
materials, buy orders, same-city flipping, Black Market, travel/risk modeling, live depth,
automation, multi-stage chains, or reinvestment.
