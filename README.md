# Albion Crafter 0.6.1

Albion Crafter is a cross-platform PySide6 desktop decision aid for Albion Online production and
market analysis. Find Me Money can now allocate one bankroll across **crafting, refining, and
outer-Royal-city market arbitrage**, while protecting silver and Focus reserves and requiring
defensible price, mechanics, and execution-capacity evidence.

The application never reads or controls the Albion client, moves items, uses Focus, crafts,
refines, or places market orders.

## Primary workflows

### Find Me Money — Simple Mode

The application opens on the normal player workflow: enter bankroll, choose a home city, and
press **FIND ME MONEY**. One explicit action performs the network-free preflight and then the
existing sparse refresh, Craft/Refine/Arbitrage evaluation, shared-capacity optimization,
validation, and immutable-snapshot pipeline. On each app launch, a separate background task checks
every active-catalog item at Normal quality across all supported cities and persists each
successful AODP batch without blocking the UI.

Simple Mode searches every action kind enabled by the saved V0.6 controls. It asks inline only
for genuinely manual blockers: current displayed station fees or required Albion prices that
AODP could not provide. Focus is disabled, without blocking non-Focus planning, until a usable
profile exists. Fast, Careful, and Strict presets expose their freshness/history/liquidity
tradeoffs in player language. Advanced Mode retains all V0.6 inputs, preflight evidence, near
misses, optimizer diagnostics, and export/history controls.

Zero-action results are deliberately distinct:

- **Setup Required** means exact player-observed evidence must be entered.
- **Not Enough Data to Know** means required market observations are missing, stale, or invalid.
- **No Profit Found** means fully priced opportunities were checked but none survived the current
  bankroll and policies.

### Production Calculator

The calculator evaluates one supported craft or refining recipe. It refreshes only that recipe's
required material/output keys after an explicit button press. Taxes, returns, cash timing, Focus
mapping, timestamps, provenance, and actionability evidence remain available in the detail view.

### Find Me Money

Find Me Money answers: given starting silver, reserves, Focus, Premium, filters, cities,
freshness, skills, sale method, transport policy, and liquidity limits, which executable set of
actions maximizes expected profit?

- Craft, Refine, and Market Arbitrage are independent action selections. Existing V0.5 saved
  preferences migrate with arbitrage disabled.
- Arbitrage is limited to distinct pairs of Bridgewatch, Fort Sterling, Lymhurst, Martlock, and
  Thetford. Its bounded item universe is derived from supported production outputs.
- Preflight is read-only and network-free. It reports action/route counts, exact sparse current
  keys, cached/missing/stale/future evidence, production-only station/FCE gaps, potential source
  and destination history keys, capacity-component estimates, and named optimizer bounds.
- Current refresh is explicit, sparse, sequential, cancellation-aware, and partial-failure
  tolerant. History is shortlisted only after current-price economics reject weak routes.
- One action-agnostic optimizer may choose any mixture—or no action. It never forces
  diversification or recycles expected revenue inside the plan.
- The table and **DO THIS** detail use immutable plan evidence and distinguish Craft, Refine, and
  Arbitrage without inventing a station, RRR, or FCE for a trade.

Player-facing recipe counts are filter-aware. Catalog coverage also separates supported recipes
from conservative static-data exclusions, so unsupported roots and incomplete upstream Item Value
records no longer look like user setup tasks.

## Arbitrage policy

Source acquisition always uses the current minimum sell price; buy orders are not supported.
Sell-order liquidation uses the destination minimum sell and charges setup plus transaction tax.
Instant liquidation uses the destination maximum buy, charges transaction tax, and has no setup
fee. Matching trusted user overrides retain the existing precedence policy.

For quantity `q`, pre-revenue capital is:

```text
source purchase cash + sell-order setup cash + explicit transport cash
```

Transaction tax is deducted from sale proceeds and is not counted a second time as upfront cash.
Expected profit is net destination proceeds minus purchase and transport. Arbitrage consumes zero
Focus, has no station, and has no resource returns, RRR, or FCE.

`LOCAL_ONLY` produces no arbitrage routes but still permits local production.
`ACKNOWLEDGED_UNCOSTED` keeps a conspicuous transport warning. `EXPLICIT_COST` charges the entered
cost once per action unit.

## Shared market capacity

Each candidate carries explicit capacity requirements keyed by region, canonical item, city, and
quality. Production consumes liquidation units equal to its output per batch. Arbitrage consumes
one acquisition unit at its source and one liquidation unit at its destination. Capacity keys do
not include action kind, so production and arbitrage selling the same item into the same city
compete for one ceiling.

The optimizer builds candidate-to-capacity connected components, constructs a capacity-feasible
Pareto frontier for each, then combines component frontiers under the shared silver and Focus
budgets. Historical activity remains a conservative execution proxy—not current order-book
depth. Missing evidence is never converted to unlimited capacity.

Named state/transition limits preserve feasibility but change the result to **Approximate** with a
reason whenever optimality can no longer be proven. **Exact** means no configured trimming or
approximation could have removed the optimum from the evaluated universe.

## Refining hardening

The five supported refining families remain ore/Metal Bars, wood/Planks, hide/Leather,
fiber/Cloth, and rock/Stone Blocks. Offline tests now pin an upstream-shaped `ao-bin-dumps`
extract, its commit/date provenance, lower-tier inputs, returnability, and enchanted canonical IDs
such as `T5_METALBAR_LEVEL1@1`.

Settings includes a dedicated five-family T4–T8 Refining Skills matrix. Blank means unknown;
explicit `0` means known level zero. Family completeness, calculated effective FCE, the global
unspecified-zero policy, generic crafting nodes, and manual effective-FCE overrides all retain
their distinct meanings.

## Persistence compatibility

SQLite remains schema version 4; existing JSON columns already represent the V0.6 domain.

- Snapshot format 1 loads as legacy Craft with one derived liquidation requirement.
- Snapshot format 2 retains Craft/Refine and derives one liquidation requirement from its legacy
  execution key.
- Snapshot format 3 writes generic routes and all acquisition/liquidation requirements.
- Preferences V1 retain their Craft-only migration. Preferences V2 preserve their prior
  Craft/Refine selection and migrate to V3 with Arbitrage disabled. V3 stores arbitrage selection,
  scope, city sets, and generic transport cost.
- Old snapshot rows are immutable and are never rewritten when opened. Canonical JSON and SHA-256
  integrity checks remain enforced; unsupported future formats fail closed.

## Setup and run

Python 3.12 or newer is required.

```bash
uv sync --extra dev
uv run albion-crafter-update-data
uv run albion-crafter
```

Startup automatically refreshes the complete active catalog's Normal-quality current prices from
AODP. The same operation is available as **Refresh ALL catalog prices** on Market Data and ignores
the manual ID fields. If no static catalog exists, startup first downloads and atomically imports
the maintained [`ao-data/ao-bin-dumps`](https://github.com/ao-data/ao-bin-dumps) release. To
isolate local state:

```bash
ALBION_CRAFTER_DATA_DIR=/tmp/albion-crafter-dev uv run albion-crafter
```

## Verification

Ordinary automated tests and independent verifiers are offline:

```bash
uv sync --extra dev
uv run pytest
uv run pytest -W error::ResourceWarning
uv run pytest --cov=albion_crafter --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run python scripts/verify_planning_preprocessing.py
uv run python scripts/verify_multicapacity_optimizer.py
QT_QPA_PLATFORM=offscreen ALBION_CRAFTER_SMOKE_TEST=1 uv run albion-crafter
```

The preprocessing verifier checks safe route pruning. The independent multi-capacity verifier
compares the production optimizer with a separate brute-force product search across seeded small
Craft/Refine/Arbitrage fixtures with shared silver, Focus, acquisition, and liquidation limits.

## Architecture

```text
src/albion_crafter/
├── core/         production mechanics plus fee-primitive-based arbitrage economics
├── market/       trusted price policy and bounded current/history clients
├── database/     schema V4 repositories, preference adapters, immutable snapshots
├── data/         guarded crafting/refining static-data ingestion
├── opportunity/  legacy independent Craft Scanner
├── planning/     three-action preflight, multi-capacity optimizer, validation, exports
├── ui/           calculator, unified planner, profile/settings, cancellable workers
└── main.py       dependency composition and background market startup
```

See [Find Me Money](docs/FIND_ME_MONEY.md), [Mechanics](docs/MECHANICS.md),
[Data sources](docs/DATA_SOURCES.md), [Database](docs/DATABASE.md),
[Focus profile](docs/CRAFTING_PROFILE.md), and
[Opportunity scanner](docs/OPPORTUNITY_ENGINE.md).

## Deliberate V0.6 limits

Top-of-book prices are not order depth, and historical volume is only an execution proxy. V0.6
does not model buy-order acquisition, same-city flipping, Caerleon, Brecilien, Black Market,
Outlands/hideout markets, travel time, mount capacity, route finding, gank/faction risk, automatic
orders or game actions, forecasts, time-to-sell, quality expected value, journals/laborers,
gathering/farming, multi-stage production, revenue/return recycling, or forced diversification.

Craft/refine material purchases use current prices, but V0.6 does not impose shared historical
acquisition-capacity constraints on those inputs. Normal quality remains the only planning mode.
`Albion_Crafting_Calculator_V1.xlsx` is a reference artifact and is not read at runtime.
