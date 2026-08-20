# Pinned refining upstream contract fixture

- Source repository: `https://github.com/ao-data/ao-bin-dumps`
- Source commit: `5cf2e8e9b7021f98683181fa5b0e3c64575978e4`
- Source commit date: `2026-07-27T11:31:41Z`
- Source paths: `items.json` and `formatted/items.json`
- Extracted: `2026-08-19`

Extraction retained the upstream `items.simpleitem[]`, `@...` attribute names,
`craftingrequirements`, and `craftresource` shapes. It keeps the first standard
non-token refining recipe for T4, T5, and T6 outputs in every supported family,
plus the independently rooted enchanted T5 metal-bar record. Nonessential item
metadata, alternate faction-token recipes, translations other than `EN-US`, and
unrelated items were removed. The fixture is intentionally small and is not a
replacement for the application catalog cache.
