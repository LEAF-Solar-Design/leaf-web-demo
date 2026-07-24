# Cat-shape litmus execution record

Date: 2026-07-24

Source under test: `origin/main` at `bb7a09098671707d6b7399d920929c3f4796be1a`

## Goal

Prove that a user's conversational agent can rearrange existing drawing panels into a recognizable cat while preserving panel identity and geometry, producing a new undoable drawing version, and using the controlled author and publish path when a suitable tool does not exist.

## Readiness gates

### A. Staging environment: NOT READY

Read-only checks show that the staging app, broker, harness, and web services are running and their target groups are healthy. The public health endpoint reports source revision `bb7a09098671707d6b7399d920929c3f4796be1a`, which matches `origin/main`.

The lane is not ready for the live litmus because:

- `/api/ready` reports `degraded`, with the worker marked as an optional degraded dependency.
- The web service was in a rolling deployment during the first audit. A later
  read-only check found one running task and a completed primary deployment at
  task definition `leaf-platform-web:6`.
- The deployed task environment did not prove that the R5 and R6 customization rollout flags are enabled for a test tenant.
- No authenticated test tenant and independent publish approver were established.
- The documented `LeafOperatorReadOnly` role could not be assumed from the available SSO administrator session. The audit used read-only calls through that session and made no AWS changes.
- The AWS source manifest is stale relative to the infrastructure repository's current `origin/main`, so it cannot yet serve as current reconciliation proof.

### B. Panel transform contract: IMPLEMENTED

Use an additive `mutations.transforms` field:

```json
{
  "transforms": [
    {"handle": "9462", "dx": 120.0, "dy": -48.0, "rotation_deg": 0.0}
  ]
}
```

Each transform rotates XY coordinates around the source polygon's vertex centroid, then translates them. It preserves handle, layer, closure, vertex order, Z coordinates, metadata, and local geometry. Invalid, duplicate, unknown, non-finite, or excessive transforms fail before persistence. Existing `added` and `removed` mutations remain compatible.

### C. Cat oracle: LEVEL 0 CALIBRATED

The deterministic Level 0 oracle uses only the Python standard library. It compares the transformed panel union against frozen 96 by 96 sitting, standing, and curled cat masks. It also checks panel identity, local geometry, non-selected content, overlap multiplicity, four-connectedness, outline distance, and named cat-region recall.

Thresholds must come from a frozen positive and negative fixture calibration. A high union IoU alone is insufficient because duplicate overlapping panels can produce the same outer silhouette.

The committed synthetic Level 0 thresholds are IoU at least `0.985`, symmetric
outline Chamfer at most `0.15` pixels, minimum named-region recall at least
`0.98`, and zero overlap pixels. These thresholds separate the frozen synthetic
fixtures. They are not yet production calibration evidence.

### D. Conversational author and publish path: NOT READY

The current converse executor's `author_tool` dispatch calls the app's legacy `POST /api/author` route. In live customization mode the app stages a change, but the conversational spine has no first-class receipt, independent approval, publish, catalog refresh, and run sequence. The harness exposes `/author/stage` and `/author/publish`, but those operations are not wired into the six conversational spine tools.

The existing `register_tool` policy schema is not a shortcut. It binds only a
tool name and manifest hash, while the publication API requires the full staged
receipt, a separate R6 confirmation, and an idempotency key. Chat staging also
generates a fresh idempotency key today. A later write approval does not bind the
catalog generation, tool digest, or expected drawing head.

The production-like acceptance path therefore needs an explicit state machine:

1. Search the tenant's effective catalog.
2. Stage a new transform tool if no suitable tool exists.
3. Present the exact immutable receipt for independent approval.
4. Publish that exact receipt with compare-and-swap protection.
5. Refresh the effective catalog.
6. Propose the write run and obtain write approval.
7. Execute, persist a new drawing version, and expose undo.

The smallest safe API addition is a non-authorizing
`request_publication(change_set_id)` conversational action. The trusted control
plane must load the server-owned receipt, record an authenticated decision, and
perform publication without exposing R6 signer material to the model or harness.
The subsequent write proposal must bind the published catalog commit, tool
manifest digest, and expected drawing head.

## Implementation wave 1

- Completed the first-class panel transform mutation contract.
- Proved that a transform persists as a new version and undo restores its parent.
- Completed the deterministic cat oracle with three frozen templates, named
  region masks, hashed thresholds, and hashed calibration evidence.
- Added negative coverage for a rectangle, disconnected shape, overlap,
  resized panel, missing handle, duplicate handle, corner contact, top-level
  intake drift, boolean coordinates, and collinear self-contact.

## Required verification

From `server`:

```powershell
py -3.13 -m pytest tests/test_panel_transforms.py tests/test_cat_oracle.py tests/test_write_loop.py tests/test_wave5.py tests/test_agent_gate.py -q
```

From `harness`:

```powershell
npm run typecheck
npx vitest run test/converseLoop.test.ts test/converseRuntimeSeparation.test.ts
```

From `web`:

```powershell
npm run build
npm run check:customization
```

Observed results:

- Server combined gate: `127 passed`, with 10 existing deprecation warnings.
- Harness typecheck: passed.
- Harness focused converse tests: `42 passed`.
- Web production build: passed, with the existing large-chunk warning.
- Web customization checks: passed.

## Deployment gate

Do not deploy the litmus to staging until Lane A is ready, the source manifest is reconciled, a non-root operator identity is usable, the customization tenant and approver are named, and Level 0 passes from a clean worktree.
