# frame-group-manage: rename and delete each append two duplicate cable-catalog entries

**Plugin:** Branch2025 settings persistence (`DrawingPropertiesJson.cs` 533, `HomerunRoutingConfig.cs` 24-73), the
same defect as `docs/parity/divergences/pad-grading-batch/settings-save-duplicates-cable-catalog.md`.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, rooftop chain steps c4 (LEAFFRAMEGROUPRENAME FGA to FGB)
and c7 (LEAFFRAMEGROUPDELETE FGB), each after save and reopen. Each step changed the FrameGroups setting exactly as
Studio does and also grew the homerun routing cable catalog by two copies of the defaults (12 to 14, 14 to 16).
The list step (c5, LEAFFRAMEGROUPLIST) writes nothing and passes with zero diffs.

## Studio's behaviour, and the declared diffs

Studio's rename and delete write the same FrameGroups setting and no catalog, so in c4 and c7 the only
disagreement is the plugin's `HomerunRouting` setting row and its entity-mapping entry. When the plugin stops
growing the catalog, both become ordinary passing receipts.

## Retired

Retired by Branch2025 #308 (saved lists replace defaults on load): recaptured 2026-09-24 on an unsigned test build of master 09b5b741 (same chain steps, c2 and c10 reopened on the same build), the receipt now passes with no declared diffs. The signed release carrying the fix is pending the operator (Solar residuals R23).
