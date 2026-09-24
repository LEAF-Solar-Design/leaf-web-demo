# Auto-fill-then-solve receipt evidence: rooftop-demo

The receipt `docs/parity/receipts/auto-fill-then-solve/rooftop-demo.json` compares the Branch2025 plugin's committed AutoFillSolve with Studio's for `data/rooftop_demo.dwg`. It passes with no diffs: both sides commit **170 strings** over the 2317 grouped panels, and both survive save and reopen.

The fixture state is the one the auto-fill receipt starts from. The drawing is grouped (11 groups), then REMOVEPANEL takes panels 81C5 to 81E0 out of the 99-panel group, leaving 71, which is infeasible at string length 14.

AutoFillSolve (`Commands.cs` 572-578) has no design logic of its own. It runs AutoFillAllPanelGroups (the local OptimalPlanSolver, because the cloud autofill answered 503; it moves 8201 from Group 6 to Group 8) and then StartPanelGroupsSolve (23 stringer pieces).

The plugin side was captured on licensed AutoCAD 2025, plugin 1.1.4.4, on 2026-09-24. The steps: a copy of the W2 `rooftop_af_pre.dwg`, primed with the plugin-made donor blocks, then `AutoFillSolve`, save, reopen, and the BTHost string and group readbacks. The private raw is `w5-a1-autofillsolve-20260924`.

- `studio-responses.json`: the 23 live stringer responses Studio received on 2026-09-24 for its own requests, in call order.

What this capture found in Studio:
- **The regrid.** AutoFill rebuilds each group it changes (`RebuildPanelGroupBlockFromPgd`, which calls `WriteMatrixForPanelGroup`), so the moved panel, 1027 units away, lands in a row and a column of its own. `server/builtins/solar_autofill.py` now regrids the two groups a correction touches when the request carries the alignment tolerance. Before this, Studio reused a free slot. The auto-fill receipt compares membership only, so it could not see the difference, but the stringer request does.
- **The evidence scope.** The removed panels are in no group. The chained producer declares `unassigned_scope: grouped`, so they are not counted as unassigned, which matches the plugin's evidence rule.

Replay the Studio side:

```
python scripts/solar_w1_studio_autofill_solve.py --fixture data/rooftop_demo.dwg \
  --intake data/rooftop_demo.v2.intake.json \
  --sizing-response server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json \
  --branch-max-offset 120.0 --alignment-tolerance 12.0 --layer-contains Panels \
  --installation-design Roof --max-string-length 14 --dwgname rooftop_demo.dwg \
  --remove 81C5 ... --remove 81E0 \
  --replay docs/parity/evidence/auto-fill-then-solve/rooftop-demo/studio-responses.json \
  --out-graph <graph.json> --out-metadata <metadata.json>
python scripts/solar_studio_evidence.py --graph <graph.json> --metadata <metadata.json> \
  --family strings --output <studio-evidence.json>
```

Flagged, not hidden: the string-sizing precondition uses the same synthetic response as the solve receipts, because the live string-length service cannot reach its weather upstream.
