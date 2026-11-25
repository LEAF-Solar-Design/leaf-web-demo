# X04 offline count contract

Parent: Studio X-bench X04 raw drawing intake. The normal count-modules read tool
lists candidates, then counts an explicit selection. It does not choose the design
role or claim acceptance on the real drawing corpus.

## Candidate eligibility

The tool considers model-space INSERTs and closed four-corner polylines. Records
marked space=paper or a named layout other than Model are excluded. An omitted
layout defaults to Model. Candidate sets group by entity type, layer and block name.
Polyline sets have an empty block name.

A repeated closing point can make a five-point ring with four corners. The four XY
corners must be distinct. All coordinates must be finite, every corner must have a
nonzero finite turn, and the locally computed area must be finite and nonzero.
Winding direction does not change eligibility. Area is computed relative to a local
origin so a large translation does not cancel a small, representable outline.
Numerically unrepresentable area or turns are rejected rather than replaced with an
invented tolerance. This is candidate filtering, not a complete geometry kernel.

## Identity and selection

Known physical handles are trimmed, uppercased and stripped of a leading 0X. Repeated
aliases of one handle count once within a set and once across the selected union.
Records without a usable handle stay distinct by their source row. The tool does not
infer physical identity from matching coordinates. It never mutates the input.

A set ID encodes entity type, layer and block as a JSON array string. Treat it as an
opaque value returned by the listing. Quotes, Unicode and backslashes round-trip.
IDs are stable under input reordering. They describe the set, not a global drawing
identity. Normal execution binds the drawing context, and the picker clears choices
when the drawing changes. A set absent from the current intake is rejected.

Nonempty set_ids takes precedence over set_id. An empty or omitted array falls back
to a nonempty set_id. With neither effective selector, the result is a listing with
count=null and selection_required=true. Repeated selected IDs do not double-count.
Malformed arguments fail schema validation. A mixed known/unknown selection returns
an actionable failure without a partial result or overlay.

## Synthetic request and result excerpts

Listing params:

```json
{}
```

Listing result excerpt:

```json
{"sets":[{"set_id":"[\"INSERT\",\"Demo\",\"Panel\"]","entity_type":"INSERT","layer":"Demo","block":"Panel","count":2}],"count":null,"selection_required":true}
```

Count params and result excerpt for synthetic handles A and B:

```json
{"set_ids":["[\"INSERT\",\"Demo\",\"Panel\"]"]}
```

```json
{"result":{"count":2,"selection_required":false},"overlay":{"highlight_handles":["A","B"]}}
```

Selected-set metadata records which sets were counted. Per-set counts can overlap;
the top-level count is the physical union. Highlights include each known selected
handle once. Unidentified records contribute to the count without invented handles.
Highlight order is not a contract; membership is. An empty selection is listing mode,
not a zero-count claim.

## Focused local reproduction

Run sequentially from the module-count worktree root:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_count_modules.py server/tests/test_x04_count_differential.py server/tests/test_x04_polygon_numeric_limits.py -q
node web/node_modules/vitest/vitest.mjs run src/components/ModuleSetPicker.test.jsx src/components/DrawingDependencyReport.test.jsx --root web
```

The differential test builds a separate logical-entity ledger for three seeded
drawings and checks all sixteen subsets of four competing sets per drawing. Known
aliases, unidentified rows, overlap and paper-space noise are generated independently
of the product's counts. The empty subset checks listing mode.

These are synthetic Python and DOM checks. Keyboard activation is simulated in
JSDOM, not observed on a physical browser. Raw dependency completeness, rendered
block geometry, upload deadlines and real-oracle equality still require the pinned
inventory, canonical staging delivery and complete shared X04 grader.
