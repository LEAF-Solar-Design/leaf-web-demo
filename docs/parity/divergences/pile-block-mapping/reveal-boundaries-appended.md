# pile-block-mapping: every save appends a copy of each template's reveal boundaries

**Plugin:** Branch2025 `LeafSolarDesign.Core/PileTemplate.cs` 92-93 (`RevealBucketBoundariesM` is initialised to the
five defaults) with `LeafSolarDesign.Core/PileTemplateStore.cs` 159-180 (the store is read with
`JsonConvert.DeserializeObject` and default settings, so the saved list is appended to the initialised one) and
63-74 (`Save` writes every loaded template back). Filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, test build 1.1.4.7, 2026-09-24, step s6 (LEAFPILEBLOCKMAP: source row 0
with its defaults, Save mapping, OK, Close), with the host pile template store captured before and after.

## Why

Loading a template gives it the five default boundaries plus its saved ones, and saving writes the combined list, so
each save adds one copy of the five defaults to every stored template that was loaded rather than built. On the
capture, the two stored templates the mapper did not touch grew from 280 to 285 and from 10 to 15 boundaries. The
mapped template is built fresh and keeps its five.

## Studio's behaviour, and the declared diffs

Studio ports the mapper's select, grid read-back, mapping upsert and store save literally
(`server/solar_pile_block_mapping.py`), and keeps each stored template's boundaries as saved. Its source rows, its
mapping row (template, override flag, local size) and its store reports equal the plugin's. It has no
`pile-template` rows, because nothing in the store changed, while the plugin has one boundary row for each of the two
grown templates. The declared diffs are exactly the comparator's for those two rows, the report rows they shift and
the entity mapping.

## Retiring it

The divergence goes away when the store reads templates with `ObjectCreationHandling.Replace` (or clears the list
before populating it), so a load and save round trip leaves the boundaries unchanged.

## Retired

Retired by Branch2025 #308 (saved lists replace defaults on load): recaptured 2026-09-25 on an unsigned test build of master a94db8d9 with the host pile_templates store restored to the original pre-s6 snapshot, LEAFPILEBLOCKMAP's Save mapping leaves the store byte-identical (the reveal boundaries no longer grow) and the receipt passes with no declared diffs. The signed release carrying the fix is pending the operator (Solar residuals R23).
