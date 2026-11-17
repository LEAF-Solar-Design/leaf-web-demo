# Prepared solar string tags

Studio X01 needs visible circuit labels on normal exported drawings. The product
core in `server/solar_string_tags.py` prepares deterministic placement data for
that work. It does not bind a catalog operation, write CAD entities, materialize
a drawing, or export text. The caller separately owns publication. This slice
adds no graph text kind and never fabricates `tag_text_ref`.

## API and scale

`prepare_tags(graph, request)` returns exactly:

```python
{
    "schema": "leaf.solar-string-tags.v1",
    "tag_layer": "LEAF_STRING_TAGS",
    "paper_mm_per_drawing_unit": 0.254,
    "tags": [
        {"string_ref": "<existing string id>", "text": "S1",
         "position": [x, y, 0], "height": 7.874015748031496,
         "rotation_degrees": 0}
    ]
}
```

Required request keys are `expected_rev` (a genuine integer equal to the graph
revision) and `paper_mm_per_drawing_unit` (finite, positive int or float; booleans
are refused). Optional keys are `tag_layer`, default `LEAF_STRING_TAGS`, and
`paper_height_mm`, default 2.0, finite and at least 1.8 mm. Unknown keys and types
are refused. There is no default paper scale. Height in drawing units is
`paper_height_mm / paper_mm_per_drawing_unit`; the resulting plotted height must
remain at least 1.8 mm within ordinary binary rounding.

The graph is checked through the existing `checked_graph`, including stale
revision and unresolved-unit refusal. Compute geometry is metres; anchors are
converted by dividing by resolved `graph.project.units.meters_per_unit`. Inches
therefore use 0.0254 metres per drawing unit, metres use 1. A unitless input must
first be explicitly resolved to a supported drawing unit and its matching scale
through the existing units APIs. Passing an unresolved scale never guesses one.

The layer must be 1–128 printable ASCII characters, may not be `0`, and may not
contain any of `<>/\":;?*|=` or a backslash. Labels are the existing `circuit_tag`,
1–128 printable ASCII characters without controls or line breaks. Errors are
named `GraphValidationError` codes with no raw label/request payload. Local
string errors use a collection-index path such as `strings[0]`.

## Placement contract

There is exactly one row per existing string, sorted by original string ID;
`string_ref` is preserved. Empty string collections refuse with
`STRING_TAGS_REQUIRE_STRINGS`. Graph and request are never mutated; no random ID,
timestamp, or publication side effect is introduced.

For a nonempty route, the anchor is route point `len(route) // 2`. Otherwise it is
the centre of ordered panel reference `len(ordered_panel_refs) // 2`. For even
lengths this is the later middle element, not an interpolated geometric midpoint.
Panel resolution uses the existing string-add `find_panels`/`slot_tables` helpers
once for all required midpoint references, including compact Ground slots.
Missing/unresolvable positions refuse rather than creating an origin anchor.

With common height `h`, each nominal horizontal TEXT rectangle has width
`1.2 * len(text) * h` and height `h`. Margin `m = 0.25*h`. The common grid has
width `max(nominal widths) + 2*m` and height `h + 2*m`. An anchor `(x,y)` snaps
down to column `floor(x/cell_width)` and row `floor(y/cell_height)`, including
negative coordinates. In sorted string order, reserve the first free row at or
above that row in the same column. Emit the cell origin plus `(m,m)`, at z=0 and
rotation 0. Occupied successor paths are compressed; there are no pairwise
rectangle scans. Sorting costs O(n log n); reservation uses a successor map with
path compression, and panel lookup scans stored/compact panels once.

Each nominal rectangle stays within its reserved cell, with a margin on all
sides. Distinct cells give disjoint nominal rectangles. This guarantees separation
between these tag boxes only: it does not avoid panels, routes, or other drawing
annotations, and does not bound glyphs rendered using arbitrary source styles.
A future writer must use a matching controlled TEXT style/insertion convention
or validate the resulting rendered extents. Collocated tags may move many rows
upward; this core does not promise a maximum distance from the source string.

## Practical limits and refusals

At most 10,000 strings are admitted, within the existing graph validator's own
byte/node limits. Height must be at least four times the smallest normal binary64
value and at most 1e12 drawing units; cell dimensions must also be finite and at
most 1e12. Nonzero converted coordinates below the smallest normal binary64 value
are refused. Converted anchors and emitted nominal box endpoints must have
absolute value at most 1e12 drawing units. Anchor-to-cell ratios and reserved row
indices must have absolute value at most 1e9. These generous finite limits keep
integer snapping and box gaps reliable in binary arithmetic; pathological scales
or coordinates refuse instead of clamping or overflowing.

Core refusal codes include `INVALID_STRING_TAGS_REQUEST`,
`INVALID_STRING_TAG_SCALE`, `STRING_TAG_HEIGHT_TOO_SMALL`,
`STRING_TAG_SCALE_LIMIT`, `INVALID_STRING_TAG_LAYER`,
`INVALID_STRING_TAG_LABEL`, `INVALID_STRING_TAG_UNITS`,
`STRING_TAGS_LIMIT_EXCEEDED`, `STRING_TAG_POSITION_REQUIRED`,
`INVALID_STRING_TAG_POSITION`, and `STRING_TAG_COORDINATE_LIMIT`. Existing graph,
revision, and compact-codec validation codes pass through unchanged.

## Focused verification

Run through the canonical off-host service environment (`offhost.py leaf-svc
--scope verify --no-local`), using:

```sh
python3 -P -m pytest --disable-plugin-autoload -p no:cacheprovider -o pythonpath=server server/tests/test_solar_string_tags.py server/tests/test_w2_string_add.py server/tests/test_solar_registry.py -q
```

Tests use small seeded graphs and a synthetic bounded large collection, without
real drawings. They independently calculate heights and boxes, cover equivalent
resolved physical scales and midpoint choices, compare compact slots to stored
panels, verify deterministic replay/input preservation, exercise refusal paths,
and count reservation-map operations for 10,000 collocated strings without a
timing gate. Prepared plans alone are not evidence of written or exported tags.
