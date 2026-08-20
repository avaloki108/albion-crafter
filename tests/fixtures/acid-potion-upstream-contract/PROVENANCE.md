# Pinned Acid Potion upstream contract fixture

- Source repository: `https://github.com/ao-data/ao-bin-dumps`
- Source commit: `5cf2e8e9b7021f98683181fa5b0e3c64575978e4`
- Source commit date: `2026-07-27T11:31:41Z`
- Source paths: `items.json`, `items.xml`, and `formatted/items.json`
- Extracted and cross-checked against XML: `2026-08-20`

The fixture preserves the upstream root `items.consumableitem` representation of
`T5_POTION_ACID`, the four referenced root `simpleitem` records, the exact `@...`
attribute names, recipe counts, `@amountcrafted`, `@maxreturnamount`, and English
display names. Unrelated metadata and items were removed. The source output and
Fine Spirit Paws records intentionally have no `@itemvalue`; the three farming
inputs retain their direct value of 40.
