# inverter-move: the move erases the inverter's homeruns and never replaces them

**Plugin:** Branch2025 `StringHomeRunCmd.cs` (InverterMove / InverterMoveSave) and the optimized homerun reroute it
calls, filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step i17 of the inverter chain (contracts G35 and G35b).
The inverter moved to the point the jig acquired, its DC homerun polylines were erased, and the reroute printed
"No inverters found in drawing. Place inverters first." even though the command had just resolved the inverter by
selection. The reopened drawing holds the moved inverter and none of its homeruns.

## Why

The reroute that follows the move discovers inverters by a different rule from the one the move itself used, finds
none, and returns without routing, so the strings of the moved inverter are left without homeruns.

## Studio's behaviour, and the declared diffs

Studio moves the inverter to the same acquired point and then reroutes each erased homerun from the same string
endpoint to the moved inverter. The device row matches; the declared diffs are exactly the comparator's, all in the
homerun rows (removed on the plugin side, changed on the Studio side). When the plugin's reroute finds the moved
inverter, this capability can be recaptured for an ordinary receipt.

## Retired

Retired 2026-09-25 by Branch2025 #323 and #331 (the inverters of a drawing open at plugin load are scanned and their strings linked) and #335 (the inverter evidence carries each device's Number), with Studio selecting the moved inverter's strings by number as StringHomeRunCmd.InverterMove does. Recaptured on test build 10 (master 2e8700d7 plus #334, OSMODE 0): MOVEINV moves block A9D5 to the typed point, erases its 24 homeruns and, because no string circuit names its number 14 (Solar residuals R33), routes none; Studio does the same and the i17 receipt compares with no diffs. The signed release carrying the plugin fixes is pending the operator (R23).
