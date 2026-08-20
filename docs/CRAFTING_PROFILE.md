# Production Focus profile and station evidence

The persisted `CraftingSkillProfile` name remains for compatibility, but one generic profile stores
Craft and Refine character knowledge. Spendable Focus and protected reserve remain separate plan
constraints. Arbitrage does not consult this profile.

## Station observations

Station fees are keyed by region, city, and typed station. Settings supports crafting stations plus
Smelter, Lumbermill, Tanner, Weaver, and Stonemason. The exact displayed number and observation
timestamp are stored with user provenance. Missing differs from explicit zero; stale blocks unless
explicitly allowed as Advisory. No workflow scrapes station fees. Arbitrage requires no station.

## Shared profile semantics

The schema stores node key, group, optional level, mutual coefficient, provenance-bearing manual
effective-FCE overrides, explicitly complete groups, and the opt-in “assume every unspecified node
is zero” policy.

Blank/missing is unknown. Explicit `0` is known level zero. Marking a group complete allows its
unspecified members to resolve as zero for that group. The global assume-zero policy is broader and
must remain explicit. A manual effective-FCE override wins for its exact mapping without deleting
stored levels.

## Crafting mappings

The generic crafting editor remains available. Dataset `albion-crafting-skills-2026-08-v2` covers
the reviewed ordinary weapon and cloth/leather/plate armor families. Unsupported or ambiguous
trees remain unknown unless a manual effective-FCE override is supplied.

## Dedicated Refining Skills matrix

Settings now shows five family rows—Ore/Metal Bars, Wood/Planks, Hide/Leather, Fiber/Cloth, and
Rock/Stone Blocks—with T4, T5, T6, T7, and T8 inputs. Each cell is blank-capable and validated as a
0–100 integer when present. Per-tier labels show the calculated effective FCE or why it is unknown;
family completeness is visible/editable. Saving synchronizes the existing profile repository, so
no database migration or duplicate profile model is introduced.

Dataset `albion-refining-skills-2026-08-v1` maps:

```text
mapping: refine/<family>/t<tier>
group:   refining:<family>
node:    refining:<family>:t<tier>
```

For a family, all reported T4–T8 levels contribute 30 FCE each and the matching tier contributes
another 250 per level. Other families contribute nothing. Enchantments use their base-tier mapping.
Derived Focus below T4 or above T8 remains unknown.

## Resolution and persistence

Focus cost uses:

```text
base recipe Focus × 0.5 ** (effective FCE / 10,000)
```

Resolution retains `derived_profile`, `manual_override`, or `unknown`, along with provenance,
mapping version, effective FCE, and missing keys. Unknown FCE disables only the focused variant;
valid non-Focus production remains eligible.

Schema V3 already stores levels, overrides, complete groups, and station fees. V0.6.2 keeps SQLite
schema V4 and uses those rows unchanged. Offscreen tests prove that blank is not saved as zero,
explicit zero is retained, calculated family/tier FCE resolves correctly, family completeness is
visible, and manual overrides retain precedence.
