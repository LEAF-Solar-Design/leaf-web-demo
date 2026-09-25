# pad-grading-batch: every settings save appends two duplicate cable-catalog entries

**Plugin:** Branch2025 settings persistence (`DrawingPropertiesJson.cs` 533, `HomerunRoutingConfig.cs` 24-73).
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step b15 (LEAFGRADEMULTI) of the third ground
batch, after save and reopen: the homerun routing cable catalog grew from 8 entries to 10, the two added
entries being copies of the defaults ("Default DC homerun", 3 AWG, 400 ft; "Upsized long DC homerun",
1/0 AWG).

## Why

`DrawingPropertiesJson` initializes `HomerunRouting = HomerunRoutingConfig.CreateDefault()` (line 533),
which fills `CableCatalog` with the two default entries (`EnsureDefaults`, lines 58-73). The JSON reader
then populates the existing object, and for a list that already holds items it appends the saved entries
instead of replacing them. The saved catalog therefore comes back as defaults plus saved, and the next save
writes that longer list. Every read-and-save cycle adds two duplicate entries; the catalog in this drawing
had already grown to 8 before this step.

This is inferred from the source and the observed growth (2 entries per save, contents identical to the
defaults); a one-line reader test in the plugin would confirm it. A fix: replace rather than reuse the list
when reading (for example `ObjectCreationHandling.Replace` on the property), or deduplicate in
`EnsureDefaults`.

## Studio's behaviour, and the declared diffs

Studio's multi-pad grading computes the same pad, elevation and label and writes no catalog, so the only
disagreement is the plugin's `HomerunRouting` setting row and its entity-mapping entry. When the plugin stops
growing the catalog, this divergence becomes an ordinary passing receipt.

## Retired

Retired by Branch2025 #308 (saved lists replace defaults on load): recaptured 2026-09-24 on an unsigned test build of master 09b5b741 (terrain chain step b15, b14 reopened on the same build), the receipt now passes with no declared diffs. The signed release carrying the fix is pending the operator (Solar residuals R23).
