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
