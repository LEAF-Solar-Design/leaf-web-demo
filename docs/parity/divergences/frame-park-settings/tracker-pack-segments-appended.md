# frame-park-settings: a loaded preset's tracker pack carries its default segments plus its saved ones

**Plugin:** Branch2025 `LeafSolarDesign.Core/FramePreset.cs` 76-89 (the `TrackerPack` getter builds
`TrackerPack.DefaultUniform(TargetModuleCount)` on first read) with `LeafSolarDesign.Core/FramePresetStore.cs` 133 and
170 (the store is read with `JsonConvert.DeserializeObject` and default settings, so the saved segment list is
appended to the lazily built default one). Same defect family as pile-block-mapping's reveal boundaries. Filed on
Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, test build 1.1.4.7, 2026-09-24, step f2 (LEAFFRAME on the active
fixed-tilt preset: OK, then Cancel once OK was refused), with the dialog's error label, the command log and both
host stores captured before and after.

## Why

The active preset is 6 rows by 1 column with a stored six-module pack. Loading it gives a pack of the six default
modules plus the six saved ones, so the OK click's pack check (`ValidateTrackerPack`,
`FrameParkSettingsForm.cs` 762-772) reads 12 modules against a target of 6 and refuses. Nothing is committed and the
stores do not change, so a user cannot OK an unchanged preset that was valid when it was saved.

## Studio's behaviour, and the declared diffs

Studio reads the preset as stored (`server/solar_batch2_simple.py`, `frame_park_settings`): the pack holds 6 modules,
the check passes and the OK commits the unchanged preset as active, so the stores still do not change. Both sides
report the same active preset and `store-changed` false. The declared diffs are exactly the comparator's for the
`pack-check` and `committed` values, the plugin's two extra rows (`pack-modules` 12 and `pack-target` 6) and the
entity mapping they shift.

## Retiring it

The divergence goes away when the store reads presets with `ObjectCreationHandling.Replace` (or the getter stops
building a default the deserializer then appends to), so a loaded preset's pack equals the saved one.
