# W1 rooftop acceptance replay

Parent: W9 / slice 015a, record 2, `sf-solar-rooftop-acceptance`.
Implementation source base: `dcbd12ab80d127808c5810f990053fee4073b4cf`
(module-power tree, branch `sf/solar-rooftop-acceptance`). The acceptance edits
are additional to that revision. Tested as the uncommitted slice (these five files)
on top of source base `dcbd12ab80d127808c5810f990053fee4073b4cf`. The planner measured
6 executed, 6 passed, 0 skipped, 0 xfailed, with full-suite wall clocks of 164 seconds
in the planner's first run (a scratch copy of the suite with these fixes) and
135 seconds in the record's verify run, both on the planner's Windows host and
each dominated by the one module-scoped scenario, well inside the gate runner's
900-second attempt bound.

## Fixture and evidence boundary

The upload is a generated-DXF representation of `data/rooftop_unsplit.intake.json`.
The test hashes its raw bytes before parsing and requires SHA256
`52506008e5cde459c8d3f695eb787a8021f2cacf4a02f499ea390cdc4d891a9a`.
`server/intake_dxf.py:intake_to_dxf` returns the LF-encoded bytes uploaded through
`io.BytesIO` as `rooftop-unsplit.dxf`. The uploaded intake must preserve all 882
panel rectangles by normalized handle: layer, closed state, vertex count/order,
and XYZ coordinates within 0.001 drawing units. This is not the original DWG.
Its upload identity is the actual intake-version SHA256 returned by `/versions`,
which becomes the seed's `source_intake_sha256` and graph's `source_hash`.

Required local recordings are:

- `server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json`: explicitly
  synthetic sizing, ZIP 44224, recommendation 14, module power 595 W.
- `server/tests/fixtures/w1_rooftop_unsplit_solve.json`: all seven rooftop groups,
  their complete memberships, matrices, responses, and ordered plugin strings.

Solve replay uses exactly
`scripts/solar_w1_studio_solve.py:replay_responses` and `matching_response`.
Only source handles are mapped to persisted application IDs. The full wire grid
must match one unused response, with all seven responses consumed exactly once.
`rooftop_demo.dwg` is the recorded wire-grid echo filename; it is not the upload
identity. Piece 21 is checked in recorded final-grid string order, not visited-path order.

This proves local HTTP/broker acceptance with recorded cloud responses. It does
not prove a browser walkthrough, licensed DWG modification, staging deployment,
or live cloud sizing. Seven synthetic inverter configurations exercise the local
equipment contract, not a manufacturer's specification. The explicit 10 AWG
choice is fixture input, not ampacity or voltage-drop certification. Routes are
direct terminal leads using persisted panel centres and inverter positions;
points are metres, lengths feet, and length is distance divided by 0.3048.
Display layout, viewport, and video fields are not applicable to this headless acceptance.

## Hermetic lifecycle and requests

`server/tests/test_w1_rooftop_acceptance.py` enters the existing
`test_w1_seed_product_path.product`, `isolated_jobs`, and `no_network` support
with one module-owned `pytest.MonkeyPatch`, one temporary root, inline execution,
and guaranteed generator teardown. It adds the real panel-import and conductor
registry records and the catalog's solve-proposal record with executor lanes.
The suite runs under an injected account principal: the reused product fixture
replaces `require_tenant` and `require_active_tenant` with one fixed,
authority-resolved principal (tier `demo`, subject `fixture-subject`), with
`auth_live` off through upload and checkout and on afterwards. It does not cover
signed-in admission (bearer-token verification, active account binding, tier
resolution) or guest uploads. Authorization that remains real under that principal
includes checkout capability minting and verification bound to the principal's
subject, capability availability and input readiness, proposal validation, and
entitlement checks against the principal's own tier. The in-process broker and
graph publication remain real. Sizing/solve grant resolution and their two
outbound transports are replaced with fixture responses; the in-process broker
call replaces its HTTP transport, and inline executors replace queued workers.
External network and APS execution are prohibited.

Upload uses `POST /api/drawings/upload` (202), followed by ready status, account
tenant ownership, H1, one version, no embedded graph, and zero jobs. Checkout
uses `POST /api/drawings/D/checkout` and requires `acquired: true`.
Every tool request is `POST /api/run?wait=1`, with `X-Tenant-Id` and the acquired
`X-Checkout-Capability`. Its body contains `tool`, `dwg: D`, current integer
`dwg_version`, the actual record's `catalog_digest`, and the following `params`.
The test reads versions and the active head's intake after every request and
requires a complete durable job, HTTP 200, `ok: true`, and the result schema.

| Operation | Params | Head / graph revision after |
| --- | --- | --- |
| Seed settings | `expected_rev: 0`, `changes: {panels_in_sequence: 14}`, `initialize: {schema_version: 1, source_intake_sha256: I, units: U}` | H2 / R1 |
| Project settings | `expected_rev: 1`, `project_changes: {name: "Rooftop unsplit", zip_code: "44224", latitude: null, longitude: null}` | H3 / R2 |
| `solar-panels-from-drawing` | `expected_rev: 2` | H4 / R3 |
| `solar-size-strings` | `expected_rev: 3`, `mode: "global"`, `requests: {Q: SR}`, `grant_ref: "fixture-grant"`, `confirm: true` | H5 / R4 |
| `solar-panel-groups` | `expected_rev: 4`, `groups: [G1..G7]` | H6 / R5 |
| Proposal i, 1..7 | `grant_ref: "fixture-grant"`, `request: REQ_i`; top-level `solve_context: {frame_ref: F_i, expected_rev: i+4, phase: "initial"}` | H(i+5) / R(i+4), unchanged |
| Commit i, immediately after its proposal | `expected_rev: i+4`; top-level `proposal_job_id: J_i` | H(i+6) / R(i+5) |
| `solar-string-conductors` | `operation: "set-conductors"`, `expected_rev: 12`, assignments for every string with `wire_gauge: "10 AWG"` | H14 / R13 |
| `solar-assign-equipment` | `expected_rev: 13`, seven synthetic equipment records and all 66 assignments | H15 / R14 |
| `solar-homeruns` | `expected_rev: 14` | H16 / R15 |
| `solar-schedule` | `expected_rev: 15`, `insertion_point: [10,20]` | H17 / R16 |

`U` is `{drawing_units: "in", wcs_to_ucs: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
elevation_datum: "unrecorded", crs: null}`. `Q` is the persisted settings ID;
`SR` is the complete unchanged sizing recording's request. Each `G_i` uses name
`Group i`, every recording panel in recording order mapped to an application ID,
alignment tolerance 0.5, and width/height derived from the first uploaded panel's
XY edges 0→1 and 1→2. No recorded matrix is inserted into the graph.
`REQ_i` is built from the current graph with `build_stringer_request`, max length
14 and echo filename `rooftop_demo.dwg`.

| Group / piece | Panels | Rows × columns | Strings |
| --- | ---: | ---: | ---: |
| 1 / 12 | 111 | 9 × 15 | 8 |
| 2 / 13 | 104 | 18 × 7 | 8 |
| 3 / 16 | 99 | 13 × 10 | 8 |
| 4 / 19 | 137 | 10 × 15 | 10 |
| 5 / 20 | 134 | 16 × 13 | 10 |
| 6 / 21 | 123 | 21 × 13 | 9 |
| 7 / 22 | 174 | 15 × 13 | 13 |

Each synthetic inverter has one MPPT A, exactly its group's string count in
inputs, 1500 V maximum DC, 150 kW AC/DC limits, origin position, unit scale,
zero rotation, model `Synthetic rooftop inverter`, type `fixture`, block `INV`,
layer `Inverters`, and `is_solaredge: false`. Stable IDs end in the group number;
input numbers are zero-based in persisted string order. Group powers are
66.045, 61.880, 58.905, 81.515, 79.730, 73.185, 103.530 kW, totalling 524.790 kW.
Cold voltage is computed from persisted sizing fields, and each string is at
most 14 modules and below 1500 V.

The successful schedule snapshot has **23 complete jobs, 17 versions, H17/R16**:
882 panels, seven frames, 66 strings, seven valid inverters, 132 valid routes,
and one valid 66-row schedule. Headers are `Circuit, Modules, Conductor,
Start homerun, End homerun, Total wire`; rows match both source leads and their sum.

## Correction and restoration

At H17/R16, `solar-correct-string` reverses the persisted `S1` membership through
`memberships: [{string_ref: S1, ordered_panel_refs: reversed_saved_refs}]` and
`expected_rev: 16`. H18/R17 has 24 jobs and 18 versions. Panel/frame sequence
views must agree. The pre-correction graph determines the exact stale closure:
S1, its inverter, all 16 routes feeding that inverter, and the schedule. Other
65 strings, six inverters, and 116 routes retain valid state.
Persisted availability must report `valid_strings_required` for equipment,
`equipment_assignment_required` for homeruns, and `complete_routing_required`
for schedule.

An old-head settings request against H17/R16 (`changes: {num_mppt: 2}`) must
return HTTP 409 with top-level `reason_code: not_current_head` before admission,
without adding a job/version or changing persisted bytes. This does not exercise
an admitted queued-job race.

Bodyless `POST /api/drawings/D/undo` and `/redo` with the same capability restore
H17/R16 and H18/R17. Active intake graphs, version bytes and SHA256, intake
digests, embedded graph digests, and graph equality must match the saved states.
Neither operation adds jobs or versions. Final state is deliberately corrected
and stale, H18/R17 with 24 jobs and 18 versions; it is distinct from the successful
scheduled snapshot. Teardown releases checkout even when an intermediate check fails.

## Replay and measurement

From `server/`, run the full scenario once:

```powershell
python -B -m pytest -q -p no:cacheprovider tests/test_w1_rooftop_acceptance.py --durations=10
```

The six unparameterized checkpoints are independently selectable with the same
command plus `-k` and the corresponding name:

1. `test_rooftop_R1_fixture_identity`: upload identity and faithful rectangles.
2. `test_rooftop_R2_product_chain`: complete scheduled product state.
3. `test_rooftop_R3_correction`: reversed membership, exact stale closure and readiness.
4. `test_rooftop_R4_stale_submission`: pre-admission refusal leaves state unchanged.
5. `test_rooftop_R5_undo_redo`: active intake and immutable version restoration.
6. `test_rooftop_R6_w1_coverage`: 15 duty rows / 12 capabilities from the ledger's
   production, T/F, wave ≤1 rules; five executed chain capabilities and seven
   reasoned exemptions. Schedule is not cable-export evidence.

Selecting a checkpoint still executes the shared full scenario; normal replay
should run all six together. The registered target floor is **six executed,
six passed, zero skipped, zero xfailed**, with no allowed skip reasons.
The gate runner's pytest parser counts xfailed tests toward a suite's floor
(pre-existing runner behaviour, not changed here); this suite contains no xfail
marker, so its floor of six is met only by six passing checkpoints.
Measured executed floor: **6 executed, 6 passed, 0 skipped, 0 xfailed**.
Measured full-suite wall clocks: **164 seconds in the planner's first run**
(a scratch copy of the suite with these fixes) and **135 seconds in the record's
verify run**, both on the planner's Windows host and each dominated by the one
module-scoped scenario, well inside the gate runner's 900-second attempt bound.
The fixture records scenario elapsed time and per-stage durations; `--durations=10`
reports setup time. The existing gate timeout remains 900 seconds. An over-budget
run must report the slow stage, without reducing coverage.

From the repository root, the registered gate is:

```powershell
python -P scripts/run-all-gates.py --continue --only server-w1-rooftop-acceptance
```

The planner's authoritative verification command is:

```powershell
python C:/tmp/goals/verify-w9-solar.py C:/tmp/goals/verify-w9-015a.json
```

It supplies acceptance, neighbouring harness/fixture/replay suites, gate runner
and registration evidence. The executor is prohibited from running shell commands
or tests; the source base, six-case result, and wall clock above are the planner's
reported measurements.
