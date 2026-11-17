# Solar string tags in source drawings

The normal registered `solar-string-tags` drawing.write tool publishes visible
circuit labels as top-level DXF TEXT through `project_drawing_editor.save_dxf`.
The pure placement core in `server/solar_string_tags.py` still prepares a plan;
the trusted builtin reads the exact stored `solar_design_graph` and verifies its
companion SHA-256. The source writer splices that plan into the exact parent
source bytes. No graph, string list, or drawing identity is accepted in params.
This adds no graph text kind and never fabricates `tag_text_ref`.

## Normal catalog publication

Discover `solar-string-tags` in the normal catalog and use that row's current
`catalog_digest`. Obtain a checkout with
`POST /api/projects/{project_id}/drawings/{drawing_id}/checkout`, retaining its
`checkout_capability`. Confirm the exact canonical parent version UUID with:

```http
POST /api/run
Content-Type: application/json
X-Org-Id: <organization UUID>
X-Project-Id: <project UUID>
X-Checkout-Capability: <checkout_capability>
Idempotency-Key: <unique confirmation key>

{
  "tool": "solar-string-tags",
  "dwg": "<canonical parent drawing version UUID>",
  "catalog_digest": "<digest from the normal catalog row>",
  "params": {
    "expected_rev": 0,
    "paper_mm_per_drawing_unit": 0.254,
    "paper_height_mm": 2.0,
    "tag_layer": "LEAF_STRING_TAGS"
  }
}
```

`expected_rev` must match the stored graph revision; it is independent of the
drawing version sequence. The normal HTTP response includes result, overlay,
producer, and the immutable child in `version.version_id`; the server writer
also returns its existing publication receipt internally.
The result includes `tag_layer`, `tag_count`, and the internal proposed
`string_tag_plan`. Read the exact child bytes at
`GET /api/projects/{project_id}/drawings/{drawing_id}/versions/{version_id}/dxf`.
`X-Leaf-Source-Sha256` binds that ordinary export to the published source bytes;
the receipt and producer proof bind source and intake hashes. The original version remains readable
and unchanged. There is no separate exporter or direct file-store write.

The same key, parent, and params replay the same child, including after checkout
release when the original capability remains verifiable. Changed params under
that key conflict. A fresh key against a stale parent refuses. Tenant, catalog,
entitlement, checkout, and parent compare-and-set guards remain the ordinary ones.

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
annotations. The source writer uses controlled style `LEAF_STRING_TAGS`: stock
`txt` font, width factor 1, fixed height 0, zero oblique and generation flags,
left/baseline justification, zero rotation, and modelspace insertion at z=0.
These are controlled nominal boxes, not a glyph-extents or obstacle-avoidance
guarantee. Collocated tags may move many rows
upward; this core does not promise a maximum distance from the source string.

## Source publication boundaries

This slice admits source-intake drawings with standard ASCII R2000-or-newer DXF
HEADER/TABLES/BLOCKS/ENTITIES topology, unique relevant tables/records/handles,
and an unambiguous `*Model_Space` BLOCK_RECORD with matching BLOCK/ENDBLK owners.
Required symbol tables are LAYER, STYLE, APPID, BLOCK_RECORD, and LTYPE; the
Continuous linetype must actually be a continuous pattern.
Missing or ambiguous topology refuses before publication. Binary DXF and native
`solar_physical_layout` parents explicitly refuse; the native decoder and
materializer are unchanged. Native support is not implied by this feature.

Source `$INSUNITS` 1 requires graph scale .0254; 6 requires scale 1. Source 0
requires explicitly resolved graph units. Unsupported or inconsistent source
units refuse. The source header units are never rewritten. Paper scale remains
required, without a catalog default: it describes intended plotting and cannot
be inferred from the drawing's physical units.

The named tag layer is on, thawed, unlocked, and plot-enabled. A conflicting
existing definition refuses instead of changing customer layers. Controlled
STYLE and APPID definitions are added or safely reused; unsafe definitions
refuse. Each new TEXT has a fresh unique hexadecimal handle, genuine modelspace
owner, and XDATA `LEAF_STRING_TAGS` / `leaf.solar-string-tags.v1` / stable
`string_ref`. Only exact schema-marked controlled TEXT is replaced on a later
confirmation, so a child upsert retains one tag per current string. A customer's
label on the same layer without that ownership marker survives. Reactors,
external application data, or references to an owned removal refuse.

All other source records preserve their raw bytes and order, including latin-1
groups and newline encodings. Only owned tag records, required new symbol-table
records/counts, and HANDSEED change. Input/output bytes are bounded by the
editor's 16 MiB maximum; plans admit at most 10,000 tags. Parsed source geometry
must match the stored intake before splicing; parsed unrelated geometry must
survive afterward. The server derives a merged intake from the new parsed CAD
fields and the original non-CAD material, preserving the exact validated graph,
companion digest, validity, and semantic revision. Deterministic handle/plan
metadata lives outside the graph as `leaf_string_tags`, without timestamps or
randomness. Ordinary raw editor writes retain their existing invalidation rules.

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
python3 -P -m pytest --disable-plugin-autoload -p no:cacheprovider -o pythonpath=server server/tests/test_solar_string_tags.py server/tests/test_project_string_tags.py server/tests/test_project_drawing_write.py server/tests/test_solar_registry.py -q
```

The pure core suite checks bounded placement and compact slots independently.
The source writer suite uses real hand-authored DXF tables and modelspace
topology for inches, metres, and explicitly resolved unitless inputs. It checks
TEXT, controlled symbols, ownership, physical heights, independent nominal box
separation, raw preservation, deterministic replay, child upsert, and refusals.
Mounted `/api/run` and ordinary `/dxf` cases use the existing lane/checkout/
versions fixtures with the genuine disposable PostgreSQL producer. They cover
catalog binding, immutable publication, graph preservation, confirmation replay,
and admission controls. A skipped integration is not publication evidence.

Prepared-core evidence, mounted local HTTP evidence, and actual staging delivery
are separate milestones. This documentation describes the implemented contract;
it makes no live-delivery claim. Staging completion requires the root's semantic
receipt from the delivered normal API and exported child.
