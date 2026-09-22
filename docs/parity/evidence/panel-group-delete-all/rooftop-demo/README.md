# panel-group-delete-all receipt evidence: rooftop-demo

`docs/parity/receipts/panel-group-delete-all/rooftop-demo.json` compares the plugin's committed
state after `PanelGroupDeleteAll` with Studio's, and passes with no diffs.

Plugin side, licensed AutoCAD 2025, 2026-09-22: on a copy of the drawing the plugin itself grouped,
`read_panel_groups` returned **11 groups**; `PanelGroupDeleteAll` printed **"Deleted 11 panel
group(s)"**; the drawing then read **0 groups**, and after save and reopen it reads `dbmod 0` with
0 groups. The command is synchronous (`Commands.cs:608-633`), with no dialog and no cloud call.

Studio side starts from the SAME committed state rather than from an ungrouped drawing: the
producer builds the panels from the bound intake, creates the groups with the kernel using the same
four parameters the plugin used, and only then deletes them. `provenance.groups_created` is 11 and
`provenance.groups_deleted` is 11, with the deleted neutral ids listed, so the receipt shows the run
really grouped before it deleted.

```
python scripts/solar_w1_studio_group_delete.py --fixture data/rooftop_demo.dwg \
  --intake data/rooftop_demo.v2.intake.json --branch-max-offset 120 --alignment-tolerance 12 \
  --layer-contains Panels --installation-design Roof --out-graph <g> --out-metadata <m>
python scripts/solar_studio_evidence.py --graph <g> --metadata <m> --family groups --output <e>
```

Contract note (rule G8): the entity mapping covers exactly the entities the evidence references.
With every group deleted, `after` references nothing, so both sides carry an EMPTY mapping; the
drawing still holds its 2345 panels, which this capability does not touch and does not compare.
