# Cat-shape agent litmus test

Status: investigation ready

Source baseline: `origin/main` at `bb7a09098671707d6b7399d920929c3f4796be1a`

Environment baseline: `https://platform-staging.leafdesign.ai/api/health` returned `ok=true`, `aps_live=true`, `n_tools=6`, `n_authored=0`, and the source SHA above on 2026-07-24. `https://platform.leafdesign.ai` returned 404, so this plan treats staging as the closest current production environment. A later run must capture this evidence again.

## Decision

The cat test is the right product test, but it cannot pass end to end today.

The current system can run the user's Claude in chat, stage a tool through `author_tool`, require approval for writes, refresh the drawing after a write, create immutable versions, and undo a write. Three gaps block the intended result:

1. Chat can stage an authored tool, but its six MCP tools cannot publish the staged tool.
2. No registered tool can move many panels while it preserves their geometry and identity.
3. The APS live write path ignores authored mutation logic and always runs the fixed `LeafWriteProbe` activity.

The mock write path is useful for a close-to-production geometry proof. It applies `removed` and `added` entities to an intake and creates a new version. It does not have a first-class, identity-preserving move operation.

Two separate surfaces currently contain six items. Do not confuse them:

- The Claude conversational spine mounts six MCP tools. It includes `author_tool` but no publication tool.
- The live tenant catalog reports six registered CAD tools: four engine tools, one general catalog seed, and one write seed. It reports zero authored tools.

## User instruction

Use this exact prompt:

> Rearrange the panels in this drawing into the shape of a cat.

Do not give Claude coordinates, a panel mapping, a tool name, or a reference image. The test must show that the product can turn the user's goal into a safe, deterministic CAD change.

## Intended pass

A fresh hosted test tenant links the user's Claude account and opens the pinned drawing fixture. The user sends the instruction once. The user can click the platform's required approval chips, but must not write a follow-up instruction or switch to the Author panel.

The user's Claude must:

1. Inspect the current drawing and catalog.
2. Find no suitable registered tool.
3. Author and stage one deterministic panel-arrangement tool.
4. Present the staged manifest and code scan for publication approval.
5. Publish the approved tool into the tenant catalog.
6. Refresh the catalog and propose a run against the open drawing.
7. Run the approved write through the deterministic job and APS path, with no LLM in the execution path.
8. Inspect the terminal result and report the new version and verification score.

The run passes only if all product, geometry, and recovery checks pass.

For this test, independent publication approval means that the authoring model and authoring harness cannot mint, hold, or redeem the approval that makes staged code effective. A trusted control-plane endpoint must create and consume an approval that binds the staged receipt. The same authenticated tenant user can click the publication approval in a single-user workspace. A tenant policy can require a second human, but that is a separate dual-control option and is not part of this litmus test.

### Product checks

- The evidence bundle contains the original user message and all server events.
- Every author, publish, and write action uses the real policy gate.
- Approval records bind the exact action and arguments that execute.
- The system does not use a preinstalled cat-specific tool.
- The authoring result is a tenant-scoped, validated tool with a staged receipt and a published receipt.
- The write runs by registered tool name. The job payload contains no generated code or drawing delta.
- The whole flow needs one user message. Approval clicks do not count as extra instructions.

### Geometry checks

- The selected panel count does not change.
- Each selected handle still identifies the same panel after the move.
- Each selected panel keeps its layer, closed state, vertex count, edge lengths, area, elevation, and metadata within fixed numeric tolerances.
- Only the selected panels move.
- No selected panel is duplicated or missing.
- Panel overlap stays below the fixture threshold.
- The arrangement stays inside the fixture work area.
- The cat oracle passes.

### Version and recovery checks

- The write creates exactly one new drawing version.
- The new version names the registered tool and points to the prior head as its parent.
- Undo restores the prior head and the exact pre-run geometry digest.
- Redo restores the cat version and its exact geometry digest.

## Cat oracle

Do not let Claude grade its own result. Do not accept panel count, total area, bounding box, or aggregate centroid as proof of a cat.

Use two independent gates:

1. A deterministic geometry gate. Normalize selected panel polygons into a fixed canvas. Compare occupancy and outline against a small approved family of cat silhouettes after translation, uniform scale, and optional reflection. Record the best intersection-over-union score, outline distance, connected-component count, and required-region occupancy for head, two ears, body, legs, and tail. Freeze thresholds from positive and adversarial fixtures before the product run.
2. A blinded recognition gate. Show only the post-run render to three judges who did not author the result. At least two must answer `cat` without seeing the prompt or file name.

The geometry gate is the regression oracle. The recognition gate protects against a metric that rewards a shape people do not recognize.

Run the deterministic gate in CI and on every investigation retry. Run the recognition gate on each release-candidate Level 2 evidence bundle. Before the run, select three judges from the named QA roster and assign opaque artifact IDs. Store each independent answer, the artifact digest, and the 2-of-3 verdict in the evidence bundle. A code-fix retry creates a new artifact digest and needs a new recognition verdict before release, but exploratory geometry calibration does not need human review on every attempt.

The oracle investigation must choose and freeze the exact template family, alignment rules, raster size, tolerances, and thresholds. It must include these negative fixtures:

- a rectangle with the same panel count and area;
- a blob with the same centroid and bounds as a passing cat;
- a cat outline with a detached tail;
- a cat mask with overlapping or duplicated panels;
- a visually plausible cat that changes panel size or loses handles.

## Test levels

### Level 0: geometry substrate

Run at `APS_LIVE=0` with a pre-seeded generic mover. This level proves the move contract, cat oracle, version creation, undo, redo, and browser refresh. It does not prove live DWG mutation or live authoring.

### Level 1: assisted staging

Use the user's Claude to author and stage the tool in the live staging stack. An operator can publish it through the existing controlled publication surface. Then chat proposes and runs it. This level proves the user's Claude authoring path, but the handoff must be disclosed. It cannot count as the intended pass.

### Level 2: full staging pass

Run the exact user instruction on the live staging stack. Claude stages, requests publication, sees the published catalog entry, proposes the write, and verifies the result. Only approval clicks are allowed. The APS output DWG must contain the moved panels when re-extracted.

Level 2 is the release litmus test.

## Current evidence and gaps

| Surface | Current state at the source baseline | Evidence | Gap to close |
| --- | --- | --- | --- |
| Closest live environment | Staging is healthy and reports the baseline SHA with APS live | Live `/api/health`, 2026-07-24 | Recheck image digest, service health, test tenant, entitlements, and Claude grant before every run |
| User Claude chat | Implemented | `harness/src/ports/impl/converseSdkRunner.ts`, `server/routers/sessions.py`, `server/turn_runner.py` | Prove one live session with a test tenant |
| In-chat authoring | Implemented on `origin/main`; it calls the app `/api/author` back-edge after approval | `harness/src/agent/converseLoop.ts`, case `author_tool`; `harness/src/ports/impl/appRunClient.ts`, `authorTool` | The active working branch is stale and still contains the old stub, so all work must branch from current main |
| Authenticated author lifecycle | `/api/author` stages a change | `server/routers/author.py`, `author`; `harness/src/agent/authorLoop.ts`, `stage` | Chat has no publication tool and cannot complete the staged-to-effective transition |
| Publication policy | Controlled register and publish endpoints exist | `server/routers/author.py`, `register`; customization authority and service | Add a safe spine action that presents server truth and redeems an independent publication approval |
| Registered mover | Absent | `origin/main` has four tools in `engine/registry.json`, one in `server/catalog_tools.json`, one in `server/write_tools.json`, and zero authored tools in live health | Define a generic panel transform tool contract |
| Mock mutation | Partial | `server/write_loop.py`, `apply_mutations` and `run_write_mock` | Add first-class, handle-preserving transforms and validation |
| APS live write | Fixed probe only | `server/write_loop.py`, `run_write_live`, `WRITE_ACTIVITY` | Execute the approved registered tool or generic transform AppBundle with its bound parameters |
| Render refresh | Implemented | `web/src/App.jsx`, refresh on `result.new_version` | Prove a large multi-panel update and capture a stable screenshot |
| Versions and undo | Implemented | `da/store.py`; `server/routers/drawings.py` | Add exact geometry digest checks for move, undo, and redo |
| Cat verification | Absent | No current semantic layout evaluator | Build and freeze the two-gate oracle |

## Parallel investigation

Run the first five lanes in parallel. Each lane is read-only or writes only to its named plan or test-fixture surface. Do not let parallel lanes edit the same file.

### Lane A: environment truth

Owner surface: deployment evidence only.

Question: What exact code and configuration does the closest production-like stack run?

Binary deliverable:

- `READY` if public health, ECS task definitions, image digests, source SHA, harness health, broker health, APS mode, customization waves, test-tenant tier, and Claude-grant status all agree.
- `NOT READY` with the first conflicting or missing fact.

Verification: capture read-only evidence with timestamp, account `807034087062`, region `us-east-1`, environment `staging`, and the named federated identity. Do not use the observed root identity for AWS reads or writes.

### Lane B: panel transform contract

Owner surface: a new contract note and isolated unit-test fixture. No production implementation files in this investigation.

Question: What is the smallest safe operation that can move many panels and preserve identity?

Candidate contract:

```json
{
  "transforms": [
    {"handle": "9462", "dx": 120.0, "dy": -48.0, "rotation_deg": 0.0}
  ]
}
```

Binary deliverable:

- `SUFFICIENT` if the contract can represent the cat fixture, validate unique existing handles, preserve local geometry, reject non-finite values, bound the work area, and produce a deterministic ordered result.
- `REVISE` with a counterexample that the contract cannot represent or validate.

Verification: property tests over translation, optional rotation, duplicate handles, unknown handles, empty sets, numeric limits, and stable serialization.

### Lane C: APS generic write route

Owner surface: write-path design and an isolated APS probe package. No edits to the shared write loop during investigation.

Question: Can APS apply a bound list of panel transforms to the input DWG and return a re-extractable output DWG?

Binary deliverable:

- `PROVEN` with a WorkItem receipt where at least ten named panels move to exact target positions, retain identity, create one output DWG, and pass re-extraction.
- `BLOCKED` with the failing API, package, parameter, or identity constraint.

Verification: compare pre-run and post-run intake by handle. Prove that the activity uses the approved registered tool or generic transform package, not `LeafWriteProbe`.

### Lane D: conversational author and publish loop

Owner surface: harness and app contract design only during investigation.

Question: How does one chat session move from no matching tool to a published catalog entry without letting the authoring model or harness approve its own staged code?

Binary deliverable:

- `CLOSED LOOP` if the proposed spine contract can stage, render server-owned receipt data, obtain independent publication approval, publish once, refresh the catalog, and run the new tool without code entering the run payload.
- `OPEN GAP` with the missing state or trust transition.

Verification: sequence test with replay, expiry, denial, argument drift, stale base SHA, duplicate confirmation, catalog collision, and SDK session resume. Lane D can design and test the state machine with an abstract registered tool while Lane B runs. Its final `CLOSED LOOP` verdict must use Lane B's frozen transform manifest and parameter schema.

### Lane E: cat oracle and fixture

Owner surface: a new fixture directory and evaluator design. It must not edit runtime code during investigation.

Question: Can a fixed evaluator reject the false passes and accept at least three distinct valid cats?

Binary deliverable:

- `CALIBRATED` if all positive fixtures pass, all adversarial fixtures fail, and thresholds are frozen before the agent run.
- `NOT CALIBRATED` with the smallest ambiguous fixture.

Verification: emit a machine-readable report with input digest, metrics, thresholds, selected template, alignment, and verdict. Save a render for human review.

### Lane F: end-to-end observer

Owner surface: test harness and evidence bundle only.

Start condition: lanes A through E are complete.

Question: Does the exact user instruction pass Level 2 without hidden operator work?

Binary deliverable:

- `PASS` only when every intended-pass check and both cat gates pass.
- `FAIL` with the first failed stage and links to the full evidence bundle.

Verification: record source SHA, image digests, tenant, drawing digest, transcript events, approval records, staged and published receipts, tool manifest digest, job id, APS WorkItem id, version chain, geometry reports, undo and redo digests, and final render. Redact credentials and tokens.

## Dependency graph

```text
Lane A: environment truth ------------------------------+
                                                         |
Lane B: transform contract ---> Lane C: APS write proof -+--> Lane F: Level 2 run
              |                                          |
              +------------------> Lane E: cat oracle ----+
                                                         |
Lane D: author and publish loop -------------------------+
```

Lanes A, B, D, and E can start now. Lane C can investigate APS packaging at once, but its final proof depends on Lane B's frozen transform contract. Lane D can investigate the publication state machine at once, but its final verdict also depends on Lane B's frozen manifest and parameter schema. Lane F starts only after A through E pass.

## Integration order

1. Freeze the transform contract and cat fixture.
2. Implement and verify the mock transform path.
3. Implement and verify the APS generic write path.
4. Close the chat stage-to-publish loop.
5. Add the cat evaluator to the evidence harness.
6. Run Level 0, then Level 1, then Level 2.

## Stop rules

- Stop if the live environment source SHA does not match the reviewed source.
- Stop if a worker uses account root credentials for routine AWS work.
- Stop if the proposed mutation changes panel count, identity, or local geometry outside tolerance.
- Stop if publication can occur without independent approval bound to the staged receipt.
- Stop if live execution falls back to `LeafWriteProbe`, local Python, mock mode, or an unregistered code payload.
- Stop if the cat verdict uses Claude's own prose or only centroid, area, count, or bounds.
- Stop if undo cannot restore the exact pre-run geometry digest.

## Investigation exit criteria

The investigation phase ends when lanes A through E each return their binary deliverable with primary evidence. At that point, the root owner writes one implementation plan with disjoint file ownership and a final Level 2 gate. No lane may claim the product litmus test passed before Lane F produces the complete evidence bundle.
