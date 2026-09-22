# Solve receipt evidence: rooftop-demo

The receipt `docs/parity/receipts/solve/rooftop-demo.json` compares the Branch2025 plugin's
committed Solve with Studio's for `data/rooftop_demo.dwg`, the whole drawing (11 panel groups,
2345 panels, max string length 14), and passes with no diffs. Both sides commit **173 strings**
over every panel (lengths 12x6, 13x65, 14x102) and both survive save and reopen.

This is the first receipt over groups the plugin SPLITS: four groups are cut into 16 pieces, so
Studio made 23 stringer calls, restitched each frame and applied the plugin's string cut.

- `studio-responses.json`: the 23 live stringer responses Studio received on 2026-09-22 for its
  own requests, in call order.

Replay the Studio side:

```
python scripts/solar_w1_studio_solve.py --fixture data/rooftop_demo.dwg \
  --intake data/rooftop_demo.v2.intake.json \
  --placement data/rooftop_demo.licensed-placement.json \
  --sizing-response server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json \
  --max-string-length 14 --dwgname rooftop_demo.dwg \
  --replay docs/parity/evidence/solve/rooftop-demo/studio-responses.json \
  --out-graph <graph.json> --out-metadata <metadata.json>
python scripts/solar_studio_evidence.py --graph <graph.json> --metadata <metadata.json> \
  --family strings --output <studio-evidence.json>
```

Flagged, not hidden: the group placement is the plugin's own PanelGroupCreate output (Studio's
kernel reproduces it, and that is receipted separately under panel-group-create, but this
producer replays the recorded placement), panels are built from the intake by the producer, and
the string-sizing precondition uses a synthetic response because the live string-length service
cannot answer (its upstream weather source is down).
