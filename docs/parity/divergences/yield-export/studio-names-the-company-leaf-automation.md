# yield-export: Studio's bundle names the company Leaf Automation

**Plugin:** Branch2025 `Pvcase/LeafYieldExportCommand.cs` (the README header and the manifest `exporter` field).
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step d8 of the terrain chain. Every entry of the plugin's
zip and of Studio's zip is identical after the G28 normalization except the README's first line and the manifest's
`exporter` value, which carry the plugin's older display name where Studio writes Leaf Automation; the manifest's
recorded size and SHA-256 of README.txt differ as a consequence.

## Why

The company name is written Leaf Automation in everything Leaf ships, by the operator's ratified naming rule.
Studio follows it; the installed plugin predates it.

## The declared diffs

Exactly the comparator's: the README text chunk and the manifest text chunk. Nothing about the layout, the bill
of materials, the cable and feeder tables, the shading tables, the pile grouping or the manifest counts differs.

## Retired

Retired by Branch2025 #307 (the yield exporter names the company Leaf Automation): recaptured 2026-09-25 on an unsigned test build of master a94db8d9 (terrain chain d8, d7 reopened on the same build, the bundle written through the Export to Yield dialog), every bundle entry matches Studio's after the G28 normalization and the receipt passes with no declared diffs. The signed release carrying the fix is pending the operator (Solar residuals R23).
