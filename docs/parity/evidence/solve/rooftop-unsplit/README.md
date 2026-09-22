# Solve receipt evidence: rooftop-unsplit

The receipt `docs/parity/receipts/solve/rooftop-unsplit.json` compares the Branch2025
plugin's committed Solve with Studio's for `data/rooftop_unsplit.dwg` and passes with no
diffs. It embeds both evidence documents; this folder holds what is needed to re-run the
Studio half without the network.

- `studio-responses.json`: the seven live stringer responses Studio received on
  2026-09-22 for its own requests (one per group).

Replay the Studio side:

```
python scripts/solar_w1_studio_solve.py --fixture data/rooftop_unsplit.dwg \
  --intake data/rooftop_unsplit.intake.json \
  --placement data/rooftop_unsplit.licensed-placement.json \
  --sizing-response server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json \
  --max-string-length 14 --dwgname rooftop_unsplit.dwg \
  --replay docs/parity/evidence/solve/rooftop-unsplit/studio-responses.json \
  --out-graph <graph.json> --out-metadata <metadata.json>
python scripts/solar_studio_evidence.py --graph <graph.json> --metadata <metadata.json> \
  --family strings --output <studio-evidence.json>
```

Flagged, not hidden: the group placement is the plugin's own PanelGroupCreate output
(Studio has no licensed_matrix broker yet), panels are built from the intake by the
producer, and the string-sizing precondition uses a synthetic response because the live
string-length service no longer accepts Studio's provisional request shape. The fixture
holds only groups the plugin sends to the stringer whole; split groups (200 or more
panels, or over 30 per side) are not yet reproduced by Studio.
