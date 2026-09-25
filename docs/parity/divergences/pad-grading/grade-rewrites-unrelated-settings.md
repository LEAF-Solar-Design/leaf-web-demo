# pad-grading: LEAFGRADE also rewrites seven unrelated drawing settings

**Plugin:** Branch2025 `LEAFGRADE` (`Terrain/GradeCommand.cs`; the elevation is persisted at lines
296-303). **Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step a10 of the second ground batch
(the terrain fixture, continuing in one session from steps a1 to a9), after save and reopen.

## What happens

LEAFGRADE on the site boundary, Auto elevation, persists the balanced pad elevation
(`GradingElevationM` = 0.26064200685635874) as intended. The same save also changes seven settings the
command has no reason to touch:

| setting | before | after |
|---|---|---|
| ExtractPanelsFromRackBlocks | true | false |
| FlowTypeMarker | (empty) | GroundMountPhysical |
| HomeRunLayer | 0 | HomeRun |
| HomerunRouting | (object) | (object rewritten) |
| PanelLayerContains | RACKING_LIMITS | (empty) |
| TagColor | ByLayer | Bylayer |
| TagHeight | 5.0 | 15.3875 |

The next command in the same session that saves drawing settings (LEAFSETBACK, step a11) writes all seven
back to their earlier values; see `../setback-boundary/settings-restored-after-grade.md`.

## Why (inferred, not yet isolated)

The command's own write (`new DrawingProperties()`, which reads the drawing's settings, then sets only the
grading elevation and mode and saves) cannot produce these values by itself: `DrawingProperties.Read`
only deserializes the drawing's JSON. The values match the ground-mount physical workflow's setup, so the
likely source is another writer that runs during the command in the same session and saves a stale or
workflow-initialized copy of the settings. Confirming it needs one plugin-side trace of every settings
save during LEAFGRADE.

## Studio's behaviour, and the declared diffs

Studio's pad grading writes only the grading elevation. The receipt's declared diffs are exactly the seven
extra `setting` rows the plugin commits and the entity-mapping entries that follow them; the grade pad,
its label and the elevation agree. Filed to the Branch2025 lane on 2026-09-23. When the plugin writes only
the grading settings, this divergence should become an ordinary passing receipt.

## Retired

Retired by Branch2025 #319 (opening a drawing no longer resets its settings): the extra settings were never written by LEAFGRADE; they were the open-time flow reset that each save carried. Recaptured 2026-09-25 on an unsigned test build of master a94db8d9 (terrain chain a10, a9 reopened on the same build), the receipt passes with no declared diffs: the delta is the graded pad and GradingElevationM only. The signed release carrying the fix is pending the operator (Solar residuals R23).
