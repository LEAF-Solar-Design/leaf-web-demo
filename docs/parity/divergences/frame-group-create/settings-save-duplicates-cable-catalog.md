# frame-group-create: the settings save appends two duplicate cable-catalog entries

**Plugin:** Branch2025 settings persistence (`DrawingPropertiesJson.cs` 533, `HomerunRoutingConfig.cs` 24-73), the
same defect as `docs/parity/divergences/pad-grading-batch/settings-save-duplicates-cable-catalog.md`.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step c3 of the rooftop chain (LEAFFRAMEGROUPCREATE FGA over
three panel groups), after save and reopen. The frame group was stored exactly as Studio stores it; the homerun
routing cable catalog also grew from 10 entries to 12, the two added entries being copies of the defaults.

## Studio's behaviour, and the declared diffs

Studio's frame-group create writes the same FrameGroups setting (name, colour index, frame handles; the wall-clock
LastModifiedTicks is synthetic) and no catalog, so the only disagreement is the plugin's `HomerunRouting` setting
row and its entity-mapping entry. When the plugin stops growing the catalog, this becomes an ordinary passing
receipt.

## Retired

Retired by Branch2025 #308 (saved lists replace defaults on load): recaptured 2026-09-24 on an unsigned test build of master 09b5b741 (same chain steps, c2 and c10 reopened on the same build), the receipt now passes with no declared diffs. The signed release carrying the fix is pending the operator (Solar residuals R23).
