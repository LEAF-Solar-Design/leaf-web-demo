# trench-routing-auto: the scan skips panel groups that are block references

**Plugin:** Branch2025 `Commands.cs` 6243-6269 (the LEAFTRENCHAUTO model-space scan) and
`LeafSolarDesign.Core/TrenchRouting.cs` 292-300 (the grid cap), filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step i6 of the inverter chain (contract G35) on the
rooftop solve fixture, whose 11 panel groups are block references. The command reported that it found no panel
groups and committed nothing.

## Why

The scan only treats `Polyline` entities on the panel-group layer as panel groups, so a drawing whose panel groups
are block references (every rooftop drawing) has none to route.

## Studio's behaviour, and the declared diffs

Studio also accepts each panel-group block reference as a group (its outlines give the centroid and the obstacles)
and then runs the plugin's own star routing unchanged. On this fixture every route exceeds the router's per-axis
grid cap (a metre-sized grid step applied to a drawing in inches), so Studio routes nothing either and reports
`no-change` where the plugin reports `no-panel-groups`. The declared diff is exactly the comparator's: that one
report value. When the plugin scans block references, this capability can be recaptured for an ordinary receipt.
