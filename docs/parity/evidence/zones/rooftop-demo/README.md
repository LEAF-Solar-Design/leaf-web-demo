# Zones receipt evidence: rooftop-demo

Three receipts compare the Branch2025 plugin's committed electrical-zone state with Studio's on
`data/rooftop_demo.dwg`, each comparator verdict pass with no diffs. The plugin side was captured on
licensed AutoCAD 2025 on 2026-09-22, saved and reopened before every read.

The capture was DESIGNED so each receipt can only pass on the capability it names:

| receipt | what was committed | why it discriminates |
|---|---|---|
| `electrical-zone-add` | `LEAFADDZONE` twice: Zone A (colour 1) and Zone B (colour 3), both EMPTY | read before any assignment, so it proves only what zone-add commits |
| `electrical-zone-assign-panels` | `LEAFZONEASSIGNPANELS` with two windows that split the drawing's largest island (556 panels) at x = 14860: Zone A 224 panels, Zone B 277, 55 straddling panels left unassigned | the counts were predicted from the intake before the run and matched exactly |
| `panel-group-create-zone-aware` | `PanelGroupCreateZoneAware` on that split: two groups, 224 panels at 30 x 8 and 277 at 31 x 10 | plain PanelGroupCreate makes ONE 556-panel group from that island, so only zone-aware behaviour passes |

Reproduce the Studio side (no network, no AutoCAD):

```
python scripts/solar_w1_studio_zones.py --fixture data/rooftop_demo.dwg \
  --intake data/rooftop_demo.v2.intake.json --layer-contains Panels --installation-design Roof \
  --zone "Zone A:1" --zone "Zone B:3" --out-graph <g> --out-metadata <m>          # zone-add
python scripts/solar_w1_studio_zones.py <same inputs> \
  --zone "Zone A:1:14100,1150,14860,2950" --zone "Zone B:3:14860,1150,15650,2950" \
  --out-graph <g> --out-metadata <m>                                              # assign
python scripts/solar_w1_studio_zones.py <same inputs, same two windows> \
  --group --branch-max-offset 120 --alignment-tolerance 12 \
  --out-graph <g> --out-metadata <m> --out-groups-metadata <gm>                   # zone-aware
python scripts/solar_studio_evidence.py --graph <g> --metadata <m or gm> \
  --family zones|groups --output <evidence.json>
```

Contract notes (joint identity contract, rules Z1 to Z8 and G1 to G8): a zone is named by its own
name, which the operator chose and the plugin stores, so it is compared; membership sorts by handle
value; the entity mapping covers exactly the zones and their assigned panels. A zone's equipment and
sizing records are null and named `not-part-of-this-capability`: creating and filling a zone commits
identity and membership, while equipment and sizing are set by inverter-add and string-sizer and
belong to their receipts. Each side keeps its raw zone fields under `provenance.zone_fields`.
