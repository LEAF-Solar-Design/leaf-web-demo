# auto-fill-revert: the plugin's revert skips exactly the groups AutoFill changed

**Plugin:** Branch2025 `AUTOFILLREVERT` (`Commands.cs` 581-606, `BranchCmd.RevertLastAutoFill`
4562-4644). **Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23.

## What happens

1. On the plugin-grouped rooftop drawing, REMOVEPANEL takes 28 panels out of one 99-panel group,
   leaving 71, an infeasible count at string length 14.
2. `AUTOFILL` moves one panel, `8201`, from that 71-panel group to a 137-panel neighbour
   ("Correction: OptimalSolver: Group 6 -> Group 8 (1 panels, d=1027, direct)"), leaving 70 and 138.
   Both groups are REBUILT, and the rebuild gives each a NEW block handle.
3. `AUTOFILLREVERT`, in the same session, prints "Revert complete: 9 group(s) restored." and
   "Revert: 2 group(s) from snapshot no longer exist and were skipped." After save and reopen the
   two changed groups are STILL 70 and 138.

## Why

`_lastAutoFillSnapshot` (`BranchCmd.cs` 3982-3991) is keyed by each group's block handle at scan
time, and the revert looks groups up by the handle they have now. AutoFill's Phase 4 rebuilds
every group it modifies with `RebuildPanelGroupBlockFromPgd`, which replaces the block reference and
its handle. So the only groups a revert can find are the ones AutoFill did not touch; the ones it
did touch are exactly those reported as "no longer exist". Any non-empty AutoFill is therefore
irreversible through AUTOFILLREVERT, and the revert still rewrites every untouched group.

## Studio's behaviour, and the declared diffs

Studio's revert applies the inverse of the recorded corrections and restores the pre-AutoFill
membership exactly: 8201 back in the 71-panel group, the neighbour back to 137. The committed
panel set is identical on both sides; the ONLY disagreement is which group holds 8201. Every
declared diff in the receipt derives from that one fact: the two groups' membership lists differ
in length and shift index by index from 8201's position, one group's neutral id differs because
8201 is its smallest member (`group:8201` for the plugin, `group:8228` for Studio), its name follows
its id, and one entity-mapping value follows the group id.

Filed to the Branch2025 lane on 2026-09-23. A fix: key the snapshot by a stable group identity
(for example the smallest member handle), or remap old to new handles during Phase 4. When the
plugin is fixed, this divergence should become an ordinary passing receipt.

## Retired

Retired by Branch2025 #314 (AUTOFILLREVERT remaps the snapshot to the groups AutoFill rebuilt): recaptured 2026-09-25 on an unsigned test build of master a94db8d9 from the same pre-AutoFill drawing (28 panels removed), AUTOFILL moved one panel (Group 6 71 to 70, Group 8 137 to 138) and AUTOFILLREVERT printed "Revert complete: 2 group(s) restored." After save and reopen the sizes are back to 71 and 137 and the receipt passes with no declared diffs. The signed release carrying the fix is pending the operator (Solar residuals R23).
