# Cat-shape litmus clearance plan

Date: 2026-07-24

Claude review: Opus 4.8, run `20260724-091747-ee562015`, verdict `REVISE`

Root approval status: executable locally. Staging mutation and independent
publication approval remain human-authority gates.

## Execution status

- Wave 0 is complete on integration branch
  `codex/cat-litmus-clearance-20260724`, based on live source line `99d7d18`.
- Wave 1 is complete. Live customization authoring now requires a stable,
  server-visible idempotency key.
- Wave 2 is complete locally. The model can request publication with only a
  durable change-set id. Approval, denial, and confirmation material remain on
  trusted internal routes that the harness cannot reach.
- Wave 3 is implemented locally. Write approvals bind the tool definition,
  effective catalog generation, drawing id, head version, and parameters. The
  subject-less trusted back-edge must supply the complete pin set, and the job
  route checks it before creating a job. Fresh tenants receive a deterministic
  base-catalog generation pin.
- Wave 4 application proof is complete offline for non-demo tenant
  `cat-litmus-tenant`. It produced version 2, passed the frozen sitting-cat
  oracle at IoU 1.0 with zero overlap, and restored version 1 through undo.
  This is not an APS or staging proof.
- Wave 5 is blocked before mutation. The default AWS identity is root, the
  non-root planning identity lacks ECS read access, and the configured SSO
  profile is expired. No AWS change was made.

## Objective

Prove that a user's conversational agent can find or author a panel-layout tool,
publish it through a trusted control path, rearrange existing drawing panels into
a recognizable cat, persist a new drawing version, and undo it. The proof must
bind every approval to the exact catalog, tool, drawing head, and parameters that
will execute.

## Current truth

- Earlier read-only evidence reported staging ready at source
  `99d7d188edfffd8f358024d701e13be3afa92001`. A later unauthenticated public
  probe returned 404, so current readiness has not been re-proved.
- The clean integration worktree is based on `origin/main` at
  `99d7d188edfffd8f358024d701e13be3afa92001`, the source revision reported by
  staging during the read-only audit.
- The integration branch now threads server-issued tool, effective catalog,
  and drawing-head pins through approval and run submission.
- The infrastructure checkout is dirty and behind `origin/main`. Its current
  source manifest reports repository head `7fc0e402108021562ce7d38124c98064efc328cd`,
  while fetched `origin/main` is `0d21a71afb731965155a0569eab83eef49249872`.
- Available AWS profile evidence does not prove assumption of
  `LeafOperatorReadOnly`. No AWS mutation is authorized through root or an
  unproved identity.

## Safety invariants

1. The model and harness never receive or submit the R6 publication secret or
   hidden publication confirmation.
2. A publication request is correlation and continuation only. The trusted app
   loads the staged receipt from durable server state and performs publication.
3. Author retries use one stable idempotency key.
4. Write approval binds the tool name, parameters, drawing id, expected drawing
   head, effective catalog commit, catalog digest, and tool manifest digest.
5. Dispatch rejects any stale binding before a job is created.
6. Static identity bindings, a shared author and approver subject, demo tenants,
   and `APS_LIVE=0` may prove the offline loop only. They cannot prove staging.
7. No staging mutation uses root. No secret value appears in logs or artifacts.

## Wave 0: preserve and integrate the Level 0 work

Owner: root.

1. Commit the already verified transform contract, oracle, fixtures, tests, and
   investigation records on the isolated feature branch.
2. Create a new clean worktree from current `origin/main`.
3. Cherry-pick the Level 0 commit and resolve only evidence-backed conflicts.
4. Run the complete baseline before publication-path edits.

Acceptance:

- The integrated branch contains the first-class panel transform contract.
- The deterministic oracle retains three templates and hashed calibration.
- Transform persistence creates version 2 and undo restores version 1.
- Baseline server, harness, and web gates pass.

Rollback: delete the new worktree and branch. The original feature branch and
commit remain recoverable.

## Wave 1: stable author staging

Owner group A: harness client and converse loop. Owner group B: app author route.
Serialize files shared with later waves.

1. Derive a stable author idempotency key from trusted tenant, session, approved
   action, and normalized description.
2. Send it as `Idempotency-Key` through the app client.
3. Reject missing keys on the live customization path instead of generating a
   random UUID. Preserve rollout-off legacy behavior.

Acceptance:

- Replaying the same approved author action returns the same change set.
- Changed tenant, session, action, or description does not reuse that key.
- A missing key on the live customization path returns a bounded 422 response.
- No raw prompt, token, secret, or generated code enters the idempotency key.

Rollback: revert the additive harness interface and author-route validation.

## Wave 2: trusted publication continuation

Owner group A: harness spine and prompt. Owner group B: new app publication
router, app registration, back-edge allowlist, policy, and tests.

1. Add a non-authorizing `request_publication(change_set_id)` spine action.
2. The app loads the durable staged receipt. The model supplies no receipt fields.
3. Add one narrowly scoped back-edge route for creating or checking the
   publication request. Keep the raw register and approval routes unreachable
   from the harness.
4. A trusted authenticated control-plane decision approves or denies the request.
   Approval internally issues and consumes R6 authority, then publishes the
   server-owned receipt. Denial publishes nothing.
5. Resume the conversation only after the effective catalog pointer reports the
   published commit and digest. Fetch a fresh catalog before proposing a run.

Acceptance:

- The harness never sees the R6 secret, confirmation, or mutable receipt fields.
- Replay produces one durable request and at most one publish.
- Denial and expiry leave the change staged.
- Receipt drift, stale base, or catalog collision fails without moving main.
- Transcript recovery restores the exact action and args, not only run requests.

Rollback: remove the new router and spine action, then revert the additive policy
and allowlist entries. Existing author and run paths remain intact.

## Wave 3: exact write approval binding

Owner: server policy, gate, job route, drawing state helper, and tests.

1. Extend the write proposal and gate schema with server-issued
   `expected_head_version`, `catalog_commit`, `catalog_digest`, and
   `tool_manifest_sha256`.
2. Resolve these values from trusted server state, not model input.
3. Recheck all pins at job submission before writing the job row.

Acceptance:

- Any drawing-head, catalog, digest, or manifest drift rejects the approved run.
- A rejected stale run creates no job and no drawing version.
- Exact replay remains single-use and idempotent.
- Existing read tools and rollout-off paths remain compatible.

Merge gate: treat this as an additive amendment to the frozen conversational
approval contract. Do not deploy it until the product owner accepts the added
server-issued fields.

Rollback: revert the additive fields and dispatch checks together.

## Wave 4: honest offline end-to-end proof

Owner: root. No AWS mutation.

Use a non-demo tenant, real platform identity storage, separate author and
approver subjects, static bindings off, and the new publication path. `APS_LIVE=0`
is allowed only to prove the application loop. The completed local proof covers
the deterministic write, immutable version, oracle, and undo. The authenticated
identity and publication proof remains part of the staging gate.

Sequence:

1. Search the effective catalog.
2. Stage a transform tool if none matches.
3. Record an independent trusted publication decision.
4. Publish and refresh the exact effective catalog.
5. Propose and approve a pinned write.
6. Run the registered tool without invoking the author SDK.
7. Verify the new drawing version and undo.
8. Evaluate the output with the frozen Level 0 oracle.

Acceptance:

- IoU is at least `0.985`.
- Symmetric outline Chamfer is at most `0.15` pixels.
- Minimum named-region recall is at least `0.98`.
- Overlap pixels are zero.
- The new version's parent is the approved head and undo restores it.
- The receipt labels this as an offline application proof, not staging proof.

## Wave 5: staging authority and tenant preparation

Owner: root prepares evidence and source changes. Human operators hold credentials
and independent decisions.

1. Prove the deployed `LEAF_CUSTOMIZATION_DB` metadata without reading secrets.
2. If it is under shared `/data/state`, choose and review one supported authority:
   PostgreSQL, or a task-local single-writer path with an explicit durability and
   failover decision. Do not enable R5/R6 on unsupported shared SQLite.
3. Name a non-customer tenant, author subject, and distinct approver subject.
4. Enable R5 and R6 only for that tenant through reviewed deployment source.
5. Prove a non-root named or federated operator identity and the rollback path.
6. Deploy the exact integrated source revision and immutable image digest.

Acceptance:

- Staging remains fully ready.
- Customization health does not report shared-SQLite rejection.
- Another tenant cannot access the rollout.
- The author cannot approve the publication.
- The exact source revision, task definition, image digests, tenant catalog pin,
  and previous rollback artifacts are recorded.

Stop conditions:

- Any use of root for routine work.
- Unsupported customization authority storage.
- Missing distinct approver.
- Source, image, catalog, manifest, or drawing-head mismatch.
- Any required readiness dependency not ready.
- R5/R6 visible outside the named test tenant.

Rollback restores the prior task definitions and image digests, prior rollout
flags, prior effective catalog pin, and prior platform release. It records one
sanitized audit event and changes no unrelated service.

## Human authority gates

1. Contract amendment:
   "Approve adding server-issued expected drawing head, effective catalog commit
   and digest, and tool manifest digest to the exact write-approval binding?"
2. Staging authority choice, only if live metadata proves shared SQLite:
   "Choose PostgreSQL or a reviewed task-local single-writer customization
   authority, and authorize the corresponding staging infrastructure plan."
3. Tenant and approver:
   "Provide the non-customer staging tenant id, author subject, and distinct
   approver subject. Set the approval credential through the trusted operator
   path without sharing it with the agent."
4. AWS access:
   "Grant or name a non-root federated identity that can assume
   LeafOperatorReadOnly for verification and the reviewed deployment role for the
   exact staging change."
5. Publication decision:
   The independent approver must accept or deny the exact staged publication
   request. The authoring agent cannot perform this act.

## Verification matrix

From `server`:

```powershell
py -3.13 -m pytest tests/test_panel_transforms.py tests/test_cat_oracle.py tests/test_write_loop.py tests/test_wave5.py tests/test_agent_gate.py tests/test_agent_router.py tests/test_sessions_router.py tests/test_contract_freeze.py -q
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

Add negative tests for missing and drifting idempotency keys, hidden publication
authority, denial, expiry, replay, stale base, catalog collision, transcript
recovery, stale drawing head, stale catalog pin, and stale tool manifest.

Observed local results:

- Harness full suite: 301 passed, 10 skipped. Typecheck and build passed.
- Web production build and customization checks passed.
- Focused publication, exact-pin, expiry, denial, and store proof: 38 passed.
- Full server suite: 1,163 passed, 46 skipped, with 12 existing warnings.
- Offline cat proof: sitting-cat IoU 1.0, zero overlap, version 2 parent 1,
  undo restored version 1.
- Independent read-only review returned `CLEAR` after two correction rounds.

## Strongest false-positive counterexample

An offline run can pass every geometry test while using a demo tenant, static
identity bindings, the same author and approver, and intake JSON instead of live
DWG bytes. That proves only local plumbing. A production-like staging claim
requires a non-customer tenant, real identity storage, distinct subjects, trusted
publication, immutable runtime artifacts, `APS_LIVE=1`, and the exact pin checks.
