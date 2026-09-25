# setback-boundary: LEAFSETBACK restores the seven settings LEAFGRADE rewrote

**Plugin:** Branch2025 `LEAFSETBACK` (`Pvcase/LeafSetbackCommand.cs`). **Proven:** licensed AutoCAD 2025
on the VMC, 2026-09-23, step a11 of the second ground batch, immediately after LEAFGRADE (a10) in the same
session, after save and reopen.

LEAFSETBACK draws its inward ring (5 m, array setback) exactly as Studio does. Its settings save also
writes back the seven settings LEAFGRADE had changed (ExtractPanelsFromRackBlocks true,
FlowTypeMarker empty, HomeRunLayer 0, HomerunRouting, PanelLayerContains RACKING_LIMITS, TagColor
ByLayer, TagHeight 5.0). This is the other half of the defect recorded in
`../pad-grading/grade-rewrites-unrelated-settings.md`: two writers in one session each save their own copy
of the whole settings object, so whichever saves last wins.

Studio's setback draws the same ring and, because its grade never changed those settings, has nothing to
restore. The declared diffs are exactly the seven restored `setting` rows and their entity-mapping
entries. When the plugin's grade is fixed, this divergence disappears with it.

## Retired

Retired by Branch2025 #319 (opening a drawing no longer resets its settings): recaptured 2026-09-25 on an unsigned test build of master a94db8d9 (terrain chain a11 in the same session as a10), the receipt passes with no declared diffs: the delta is the setback ring only. The signed release carrying the fix is pending the operator (Solar residuals R23).
