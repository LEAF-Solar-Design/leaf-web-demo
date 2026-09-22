# string-multi-add receipt evidence: rooftop-demo

The receipt `docs/parity/receipts/string-multi-add/rooftop-demo.json` compares the Branch2025
plugin's committed MultiString with Studio's on `data/rooftop_demo.dwg`, and passes with no diffs.

What was done on both sides: starting from the solved drawing (173 strings), the 14-panel string
whose first member is 93A6 and the 13-panel string whose first member is 92AE were deleted,
freeing 27 panels, and MultiString ran over all 27 with a target length of 14.

27 does not divide by 14, so the plugin warned ("MultiString - Uneven Panel Count", 1 string of
14 and 1 string of 13) and, on OK, sent a matrix of just those panels to the stringer. It did NOT
rebuild the two strings that were deleted. Both sides commit the same two new strings:

    14: 92A9 92AA 92AB 92AC 92AD 92AE 92C2 92C1 92C0 92BF 92BE 92BD 92BC 939D
    13: 93A6 93A5 93A4 93A3 93A2 93A1 93A0 939F 939E 93B0 93B1 93B2 93B3

Studio reaches this with `server/solar_string_combo.py`, a literal port of the plugin's
StringComboFunc (27 panels at length 14 -> one 14 and one 13), and one stringer call over the
sub-matrix of the selected panels through the same client the whole-drawing solve uses.

- `studio-multi-responses.json`: the one live stringer response Studio received for that
  sub-matrix request on 2026-09-22.

Replay the Studio side (the base solve replays the solve receipt's own recorded responses):

```
python scripts/solar_w1_studio_string_multi_add.py --fixture data/rooftop_demo.dwg \
  --intake data/rooftop_demo.v2.intake.json \
  --placement data/rooftop_demo.licensed-placement.json \
  --sizing-response server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json \
  --max-string-length 14 --dwgname rooftop_demo.dwg \
  --replay docs/parity/evidence/solve/rooftop-demo/studio-responses.json \
  --multi-replay docs/parity/evidence/string-multi-add/rooftop-demo/studio-multi-responses.json \
  --delete string:93A6 --delete string:92AE \
  --add 93A6 ... --add 92C2 \
  --out-graph <graph.json> --out-metadata <metadata.json>
python scripts/solar_studio_evidence.py --graph <graph.json> --metadata <metadata.json> \
  --family strings --output <studio-evidence.json>
```

The 27 `--add` handles are the members of the two deleted strings, in any order: the selection is
a set, and the stringer, not the caller, decides the order.

Flagged, not hidden: the same precondition flags as the solve receipt apply (the plugin's own group
placement is replayed, and string sizing uses a synthetic response because the live string-length
service cannot answer).
