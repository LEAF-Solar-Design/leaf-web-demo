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

## Retired

Retired by Branch2025 #313 (the scan accepts panel-group block references): recaptured 2026-09-25 on an unsigned test build of master a94db8d9 (inverter chain i6, i5 reopened on the same build), LEAFTRENCHAUTO now finds all 11 block panel groups and, like Studio, routes none of them because of the router's grid cap, so both sides report no change and the receipt passes with no declared diffs. The grid step being applied in drawing units is a separate defect on both sides (Solar residuals R27). The signed release carrying #313 is pending the operator (R23).

## Retired

Retired 2026-09-25 by Branch2025 #313 and #326 (block panel groups are scanned and the grid step is converted to drawing units): on test build 10 LEAFTRENCHAUTO routes all 11 panel groups. The remaining differences are three equal-cost ties, declared in equal-cost-ties-on-lattice-bits.md.
