# W1 authoring contract: authored tools that read the pinned design graph

A tenant-authored tool can read the tenant's own stored W1 design graph. It declares
`graph_input: "solar-w1-graph"` on its record, and the broker hands it one pinned,
immutable snapshot of that graph as its intake, through the same sandbox seam every
authored tool already runs in. There is no second authoring service: the tool is
authored, persisted and listed by the ordinary `POST /api/author` lane.

Ledger row: `solar-parity-016`. Proof: `server/tests/test_w1_authored_reports.py`,
suite `server-w1-authored-reports` in `scripts/run-all-gates.py`.

## The record field

| Field | Valid value | Anything else |
| --- | --- | --- |
| `graph_input` | exactly `solar-w1-graph` (14 characters, `[a-z0-9-]`) | `ToolRecordFieldError` on the authoring path (422 naming the field); dropped with one warning on a fold tier read |

The field is optional. A record without it behaves exactly as before, and no committed
record carries it. `AuthorRequest.graph_input` is typed `Literal["solar-w1-graph"] | None`
and `_validated_record_fields` forwards it with `icon` and `placement`
(`server/routers/author.py`). Validation lives in one place,
`server/tool_record_fields.py`.

`server/solar_authored_graph.py::reads_graph(tool)` is true only for a record that
declares the field and is not owned by the solar registry (no `solar` block, no
registry declaration, no adapter kind) and is not a `drawing.write` tool. Trusted
read and commit kinds never take this path.

## The intake

`build_graph_intake(backend, tenant_id, drawing_id, source_version)` returns:

```json
{
  "schema": "leaf.solar-graph-intake.v1",
  "drawing_id": "<the job's drawing>",
  "source_version": 7,
  "graph_sha256": "<sha256 of canonical graph bytes>",
  "graph": { "...": "a deep copy of the stored graph at exactly that version" }
}
```

- The graph is read once through `resolve_graph_context`, scoped to the requesting
  tenant, so another tenant's drawing resolves nothing.
- `graph_sha256` is sha256 over `leaf_cloud_client.canonical_bytes(graph)`, the same
  digest the trusted read kind's receipts carry.
- The intake is bounded by the sandbox's own input limit, 8388608 bytes, measured
  before any sandbox starts on exactly the bytes the sandbox measures:
  `json.dumps(intake, separators=(",", ":"))` encoded as UTF-8.

## Execution path

1. Jobs router (`server/routers/jobs.py`): a graph intake tool is treated like the
   trusted read kind. A caller `drawing_id` that disagrees with `dwg` is a 409
   `DRAWING_ID_CONFLICT`; an unpinned run is pinned to the current head (an
   unreadable head is a 409 `GRAPH_CONTEXT_UNAVAILABLE`); `params.drawing_id`
   defaults to `dwg`; the holder is anonymous; `aps_live` is always false.
2. Broker (`server/broker.py`), after the deployed-posture gate, which is unchanged:
   a graph intake tool with authored execution off, or with no sandbox tier engaged,
   is still refused `TENANT_DISABLED` in staging and production.
3. Broker refusals before any read, each with `reason_code`:

| Condition | Reason | Status |
| --- | --- | --- |
| no job id | `JOB_IDENTITY_MISSING` | 400 |
| `dwg_version` not an int of at least 1 | `INVALID_SOURCE_VERSION` | 400 |
| `params.drawing_id` differs from `dwg`, or no `dwg` | `DRAWING_ID_CONFLICT` | 400 |
| no readable graph at that version for this tenant | `GRAPH_CONTEXT_UNAVAILABLE` (or the context's own code) | 409 |
| intake over 8388608 bytes | `GRAPH_INTAKE_TOO_LARGE` | 400 |
| APS, a staged test source, or a file-only run | `authored_graph_input_invalid` | 400 |

4. The intake replaces the drawing intake in the broker's existing `run_dynamic` call.
   The routing key `drawing_id` is removed from the params first, so the tool's own
   params schema never has to admit it. The authored body runs only through
   `tool_loader.run_tool_dynamic`, never through `solar_local_read`, whose loader is
   for trusted builtins only.

A run pinned to version N answers from version N forever: a later graph edit makes a
new version and leaves N untouched, so re-running pinned to N returns the same output
byte for byte.

## Writing an authored graph report

The body is `run(intake, params)` and returns `(result, None)` with a JSON object
result under 1048576 output bytes. The sandbox runs the standard library only. The
three proof reports are in `server/tests/fixtures/w1_authored/`:

| Report | Output |
| --- | --- |
| `zone_schedule.py` | `zones`: one `{zone, panels, strings}` row per electrical zone, in stored order |
| `unassigned_panels.py` | `panel_refs`, `handles` (panel source handles) and `count` for grouped panels no string carries, in stored order |
| `string_length_exceptions.py` | `exceptions`: `{string, panels, rule, min or max}` for each string outside the bounds; `above_max` wins over `below_min` |

String bounds come from the params (`min_length`, `max_length`), else
`settings.extra.string_length_min` and `settings.extra.string_length_max`, with the
committed `settings.panels_in_sequence` as the maximum when none is given.

## Which authoring paths accept the field

Graph-input authoring is supported on the templated path only: `POST /api/author`
with auth off, the protected rollout off and no `LEAF_AUTHOR_HARNESS_URL`, where the
router itself writes and registers the record. Every other path refuses a request
carrying `graph_input` with HTTP 422 and `error_code` `GRAPH_INPUT_UNSUPPORTED`,
before any harness call or stage, and creates nothing:

| Path | Why it refuses |
| --- | --- |
| `POST /api/author` with `LEAF_AUTHOR_HARNESS_URL` set | the harness registers the record in the tenant repo from the description alone, so the field would never reach the catalog record |
| `POST /api/author` on the protected (R5) lane | the staged record does not carry the field |
| `POST /api/author/stage` | the staged record does not carry the field |

A request without `graph_input` behaves exactly as before on every path. Carrying the
field through the harness record (its `/author`, `/author/stage` and
`/author/publish` routes and the authoring loop) is a follow-up.

## Staging

Staging arms `LEAF_AUTHORED_EXECUTION=1` with a sandbox tier, so these tools execute
there. Staging also sets `LEAF_AUTHOR_HARNESS_URL`, so model-authored graph reports
cannot be registered there until the harness follow-up above lands; until then a
staging `POST /api/author` with `"graph_input": "solar-w1-graph"` answers 422
`GRAPH_INPUT_UNSUPPORTED`. The staging walk
(`npm --prefix web run proof:staging -- w1-solar-authoring.spec.mjs --workers=1`,
folded into `solar-parity-015`'s staging run) depends on that follow-up.
