# panel-group-create receipt evidence: rooftop-demo

The receipt `docs/parity/receipts/panel-group-create/rooftop-demo.json` compares the Branch2025
plugin's committed `PanelGroupCreate` with Studio's for `data/rooftop_demo.dwg` and passes with no
diffs: the same **11 groups** over the same **2345 panels**, group for group and member for
member, with the plugin's state read back after save and reopen.

Studio's side is its own pure kernel (`server/solar_panel_group_kernel.py`), not a recorded
placement: the same island partition, angle sub-split and matrix the plugin computes with AutoCAD
geometry, from the intake alone.

Reproduce the Studio side (no network, no AutoCAD):

```
python scripts/solar_w1_studio_groups.py --fixture data/rooftop_demo.dwg \
  --intake data/rooftop_demo.v2.intake.json \
  --branch-max-offset 120 --alignment-tolerance 12 \
  --layer-contains Panels --installation-design Roof \
  --out-graph <graph.json> --out-metadata <metadata.json>
python scripts/solar_studio_evidence.py --graph <graph.json> --metadata <metadata.json> \
  --family groups --output <studio-evidence.json>
```

The four parameters are not typed by hand on either side: they are read from the plugin run's own
`get_drawing_properties` capture (`BranchMaxOffset` 120, `AlignmentTolerance` 12,
`PanelLayerContains` "Panels" giving the filter `*Panels*`, `InstallationDesign` "Roof").

Recorded divergence, not matched: the plugin numbers groups ("Group N") in the order of the
AutoCAD handles of the outline polylines it creates. That order is not a property of the drawing's
geometry and cannot be reproduced outside AutoCAD, so the joint identity contract treats the label
like an entity handle: both sides name a group by its neutral id (`group:` plus its smallest
member handle by hex value) and keep their own labels in `provenance.group_names`.
