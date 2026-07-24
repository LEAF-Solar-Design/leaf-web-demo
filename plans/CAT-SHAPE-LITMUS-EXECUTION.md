# Cat-shape litmus execution record

Date: 2026-07-24

Source under test: integration branch based on `origin/main` at
`99d7d188edfffd8f358024d701e13be3afa92001`

Current status: Levels 0 through 4 are implemented or proven locally. Staging
execution remains blocked by AWS read authority, customization database
authority, tenant identity, distinct approver, and product-owner contract gates.
No deployment or AWS mutation occurred.

## Goal

Prove that a user's conversational agent can rearrange existing drawing panels into a recognizable cat while preserving panel identity and geometry, producing a new undoable drawing version, and using the controlled author and publish path when a suitable tool does not exist.

## Readiness gates

### A. Staging environment: NOT CURRENTLY RE-PROVED OR AUTHORIZED FOR LITMUS

Earlier read-only checks showed healthy staging services and later reported
source revision `99d7d188edfffd8f358024d701e13be3afa92001`. A final unauthenticated
public probe returned 404, so this record does not claim current readiness.

The lane is not ready for the live litmus because:

- A later read-only check reported `/api/ready` fully ready, including the
  worker and database, with `degraded_mode` false.
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

### D. Conversational author and publish path: IMPLEMENTED LOCALLY

The spine now exposes `request_publication(change_set_id)`. The app loads the
durable staged receipt, waits for an independent trusted approval or denial,
and resumes publication without exposing confirmation material to the model or
harness. Raw registration and internal decision routes remain outside the
back-edge allowlist.

Chat staging uses a deterministic idempotency key. Write approval now binds the
tool definition digest, effective catalog commit and digest, drawing id, exact
head, and parameters. The run route requires those pins from the trusted
subject-less back-edge and rechecks them before job creation. A fresh tenant
uses a server-issued deterministic base-catalog generation.

Publication denial and expiry keep the change staged. Denial revokes approvals
issued before the decision, while a later independent approval can resume the
same durable request. Publication success invalidates the turn-local catalog so
the next search or run fetches the new effective catalog.

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

- Server full suite: `1,163 passed, 46 skipped`, with 12 warnings.
- Focused publication, pin, denial, expiry, and store suite: `38 passed`.
- Harness full suite: `301 passed, 10 skipped`.
- Harness typecheck and production build: passed.
- Web production build: passed, with the existing large-chunk warning.
- Web customization checks: passed.
- Independent read-only review: `CLEAR`.

## Deployment gate

Do not deploy the litmus to staging until Lane A is ready, the source manifest is reconciled, a non-root operator identity is usable, the customization tenant and approver are named, and Level 0 passes from a clean worktree.
