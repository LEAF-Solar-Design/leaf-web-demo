# cable-export: the workbook save fails and nothing is written

**Plugin:** Branch2025 `StringHomerunExportForm.cs` 384-400 (Export All, `excelPackage.SaveAs` inside a bare
`catch`), filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step i9 of the inverter chain (contract G35). Export All
showed "Unable to save to ..." and no workbook was written; the drawing did not change.

## Why

The workbook library's zip writer needs a legacy code page that the AutoCAD .NET host has not registered, so the
save throws, and the bare `catch` replaces the cause with a generic message.

## Studio's behaviour, and the declared diffs

Studio writes the Export All workbook (one sheet per schedule the export form fills) and its evidence carries the
file as a `file` row, where the plugin's evidence carries only its `report` row. The declared diffs are exactly the
comparator's. When the plugin registers the code page, this capability can be recaptured for an ordinary receipt.

## Retired

Retired by Branch2025 #309 (the code pages the workbook writer needs are registered): recaptured 2026-09-25 on an unsigned test build of master a94db8d9 (inverter chain i9, i8 reopened on the same build), Export All writes the workbook and the plugin evidence carries it as a file row (Branch2025 #325). Studio's workbook now follows the real plugin layout and the receipt differs only by the rating row declared in inverter-rating-units.md. The signed release carrying #309 is pending the operator (Solar residuals R23).
