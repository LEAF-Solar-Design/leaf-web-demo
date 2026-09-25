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

## Retired

Retired 2026-09-25 by Branch2025 #312 (the optimum search is bounded: 9800 checks in 0.22 s on test build 9), #334 (an inverter with no linked strings stays where it is instead of moving to the empty-extents sentinel) and #335, with Studio selecting strings by number. On test build 10 PositionInv on block A9D5 reports "Inverter 14 has no connected strings; its position is unchanged." and leaves the drawing unchanged; Studio reports no-connected-strings and the position receipt compares with no diffs. The signed release is pending the operator (R23).
