# inverter-position: the optimum search is unbounded and never completes

**Plugin:** Branch2025 `StringHomeRunCmd.cs` InverterMove with the optimum-position flag (the 20-unit grid over the
inverter's string extents), filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, POSITIONINV on inverter A9D5 after step i16 of the
inverter chain (contracts G35 and G35b). AutoCAD stopped responding for more than 16 minutes and the process then
exited; nothing was saved, so the drawing on disk is the i16 state unchanged.

## Why

The search scores the total homerun length at every point of a 20-unit grid across the inverter's string extents, on
the UI thread, with no cap on the number of points or on the time spent. On a drawing in inches the grid holds
millions of points.

## Studio's behaviour, and the declared diffs

The plugin's evidence is the unchanged i16 state plus one `report` row (`unbounded-search`). Studio runs the same
scoring over a bounded grid (named point and time budgets, failing closed past either), moves the inverter to the
best point and reroutes its homerun legs. The declared diffs are exactly the comparator's. When the plugin bounds its
search, this capability can be recaptured for an ordinary receipt.
