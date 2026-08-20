# SQLite schema and migrations

Albion Crafter records `PRAGMA user_version` and V0.6 deliberately remains on SQLite schema 4.
Preference and snapshot JSON evolution does not require a table change. Existing V0/V1 → V2 → V3
→ V4 migrations remain transactional, idempotent, content-preserving, and fail closed on damaged
or future schemas.

## Schema V4 state

Schema V3 introduced station observations, shared crafting/refining profiles, history intervals
and coverage, and catalog import diagnostics. V4 added immutable plan snapshots and their recent
index. Catalog, current market, overrides, history, station/profile, import reports, settings, and
snapshots are preserved unchanged for V0.6.

Current market and history remain separate and are not foreign-keyed to the active catalog, so a
static refresh cannot erase useful observations. Bulk reads use bounded parameter chunks; no
repository is called from candidate or optimizer loops.

## Immutable snapshot envelopes

`plan_snapshots` stores indexed summary fields, complete canonical JSON, and lowercase SHA-256.
Loading verifies size, hash, supported envelope, canonical serialization, domain validation, and
agreement with every indexed field. Opening a historical snapshot is read-only.

Envelope evolution is independent from SQLite schema:

- Format 1 (V0.4.x) loads as Craft, maps legacy `craft_city` in memory, and derives one
  Liquidation capacity requirement from the single execution key.
- Format 2 (V0.5) retains its Craft/Refine action and production-city language, and derives one
  Liquidation capacity requirement.
- Format 3 (V0.6) writes action kind, generic route evidence, every Acquisition/Liquidation
  requirement and role, and complete source/destination/economic evidence.

Old rows retain their original `snapshot_format_version`, payload, and hash. Unsupported future
formats, corrupted capacity evidence, rehashed noncanonical content, and indexed metadata mismatch
are rejected safely. No V5 schema migration is needed.

## Find Me Money preferences

Preferences remain typed JSON in `settings`:

- V1 keeps its existing Craft-only migration behavior.
- V2 preserves the stored Craft/Refine selection and families, writes a separate V3 value, and
  deliberately leaves Arbitrage disabled.
- V3 stores action selection, refining families, generic production-city/transport language,
  arbitrage scope, and source/destination city sets.

Migration never deletes the older preference key or unrelated settings. Unknown fields, wrong
scalar/list types, stored JSON null, invalid city sets, and future envelope versions fail closed.

## Connection boundaries

Connections enable foreign keys and WAL and close deterministically. Schema and snapshot writers
use explicit transactions. Snapshot duplicate detection and retention are atomic. History/current
batch persistence intentionally commits healthy batches independently so partial network failure
does not destroy earlier evidence.
