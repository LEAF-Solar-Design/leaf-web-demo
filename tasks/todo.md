# Staging failed-test repair

- [x] Wave 0: capture the current gate baseline and add failing regression checks.
- [x] Wave 1A: route catalog actions through fail-closed run intents.
- [x] Wave 1B: implement honest every-N authoring and unsupported declines.
- [x] Wave 1C: bound startup fetches and use one session mock catalog.
- [x] Wave 2: integrate and pass the full local application gate.
- [x] Wave 3: repair the staging web build contract and add canonical-worker infrastructure.
- [ ] Wave 4: deploy staging only and replay the complete browser and readiness gate.
- [ ] Audit the final diffs, CI, deployment receipts, rollback evidence, and ledger.

Risks:

- Confirmation is safety-sensitive. Stale or changed intents must never execute.
- Authenticated staging must never silently fall back to mock tenant data.
- Web build arguments must remain staging-only and must never move production aliases.
- Database and worker rollout stays operator-gated and uses named or federated AWS identity.

# Standards surface cat operator flow

- [x] Replace the `/try` command-bar redirect with an in-scene request flow.
- [x] Show proposal approval, execution, result, version, Undo, and Redo in the same scene.
- [x] Add browser coverage that proves `/try` stays on `/try` throughout the flow.
- [x] Run the frontend build and focused browser proof, then inspect the final scene.

Risks:

- Keep `/app` and its backend contracts unchanged.
- Keep the cat data labeled as a deterministic proof fixture, not a live Claude or APS run.
- Preserve the committed motion and calm-surface standards.

# Unified surface end-to-end program

- [x] Inventory the legacy `/app`, current `/try`, API seams, and design standards.
- [x] Reconcile the Opus 5 critique into the traceability ledger and wave plan.
- [x] Wave 0: add the three-tier proof ladder, behavior pins, receipts, and capability test IDs.
- [ ] Wave 1: extract shared controllers and mount them under both routes. Conversation is the first integrated slice.
- [ ] Wave 2: integrate and prove the core workspace, command, job, drawing, and version journey.
- [ ] Wave 3: integrate authoring and trust controls, then add the missing ingest seam.
- [ ] Wave 4: prove accessibility, keyboard, responsive, motion, and visual standards.
- [ ] Wave 5: record the aggregate walk and promote authorized flows to staging proof.

Current additive progress:

- [x] Prove a real account-scoped uploaded DXF remains the target of catalog
  review and execution on the managed local stack.
- [x] Prove cross-tenant upload access, unknown uploads, guest bootstrap, expired
  sessions, tampered sessions, and bearer precedence fail closed at the server.
- [x] Prove the live-auth signed-out guest path in the unified browser: upload
  and view are allowed, run remains visibly gated, and no dispatch occurs.
- [x] Prove a real running local job reattaches, completes once, and submits no
  duplicate run through the unified surface.
- [x] Prove Escape detaches the unified UI without sending the page-close reap
  beacon or stopping the durable job.
- [x] Prove page hide sends one deduplicated close beacon and the real orphan
  reaper fails the abandoned job once.
- [x] Prove two real drawing writes create a three-version chain, then drive two
  undos and two redos through the unified surface and authoritative API.
- [x] Prove a real local conversation session, proposal, approval record, and
  resume turn in the unified scene, with the scripted agent limit in the receipt.
- [x] Make the managed proof runner return Playwright failures and isolate its
  conversation metering ledger from the worktree.
- [x] Prove real checkout conflict, non-holder denial, expiry, take, release,
  write gating, and final authoritative state in the unified browser.
- [x] Prove real local health, usage, entitlement, refresh, Claude grant link,
  secret non-echo, and destructive unlink in the unified Trust rail.
- [x] Isolate managed proof runs from ambient Claude and Anthropic credentials.
- [x] Prove the real local drawing's layer count, zoom, Fit, resident canvas,
  entity picking, layer visibility, and selection clearing in the unified View
  rail.
- [x] Render the shared controller's real capability families in the unified
  Catalog tab and prove totals, collapse, detail, review, execution, and durable
  restoration on the managed local stack.
- [x] Prove a real natural-language catalog match from the unified command bar
  through immutable review, exactly one dispatch, execution result, and durable
  job truth without creating a Claude session.
- [x] Prove the real local internal Operations drawer lists isolated tenant
  usage, cancels before mutation, disables and restores through broker
  authority, scopes its disposable credential, and fails closed after removal.
- [ ] Promote protected author stage, independent approval, publish, catalog
  refresh, and use in an authorized environment with live signed identity,
  trusted tenant binding, isolated authoring authority, and R5/R6 rollout.

- [x] Integrate projects, catalog, authoring, upload, viewer, selection, checkout,
  trust, account, results, details, versions, operations, and responsive controls
  into `/try`.
- [x] Prove structured and submit failures require fresh approval before retry.
- [x] Prove initial and mid-session 401, sign-out, token clearing, and poll stop.
- [x] Persist drawing controller state across `/try` to site to `/try` recasts.
- [x] Recover a failed post-write viewer refresh without rerunning the write.
- [x] Route ordinary language through catalog classification before Claude, with
  visible local fallback and honest no-match recovery.
- [x] Add named landmarks, visible focus, proposal Escape priority, and modal
  drawer focus ownership to the unified scene.
- [x] Mount quota, degraded-result, and degraded-backend notices with usage and
  health refresh actions in the unified scene.
- [x] Enable resident viewer pan and zoom, wire Fit, and expose recoverable WebGL
  fallback without remounting the healthy canvas.
- [x] Move the guided cat walkthrough onto `/try?demo=tour` and record the whole
  request, approval, version, history, and trust sequence in one scene.
- [x] Bind both tablists to named tab panels and make resolver choices fully
  keyboard focusable.
- [ ] Reconcile remaining visible and standards gaps against the capability ledger.
- [x] Run the expanded 37-test aggregate fixture suite.
- [x] Run the managed local real-stack catalog, confirm, dispatch, receipt, and
  reload-persistence proof.
- [ ] Run any authorized staging proof.

Files and ownership are defined in `plans/UNIFIED-SURFACE-E2E-EXECUTION.md`.

Risks:

- Do not copy production state machines into another fixture-only surface.
- Do not label deterministic browser fixtures as local-stack or staging proof.
- Preserve exact approval, tenant, checkout, and version bindings.
- Keep the existing staging-fixes failure visible until its root cause is fixed.

# Live edge-contract repair

- [x] Bound `drawing_state` summary output while preserving useful counts.
- [x] Reject low-confidence or destructive nonsense catalog matches.
- [x] Make `author_tool` approval replay use the app-minted confirmation ID.
- [x] Dispatch approved chat authoring through the existing author service.
- [x] Restore the SDK pre-tool permission callback.
- [x] Add focused negative and positive regression tests.
- [x] Run harness, server, type, build, and full repository gates.
- [x] Review the final diff and document contract changes in code and tests.

Risks: approval binding is security-sensitive; catalog changes must not hide valid tools; output bounding must not break existing clients.

# PostgreSQL concurrency execution

- [x] Record the operator boundary: keep both production domains pinned to the
  current deployment and treat all rollout work as staging-only.
- [x] Wave 0: add shared PostgreSQL transaction and counter primitives.
- [x] Wave 0: add a PostgreSQL `SessionStore` implementation and contract tests.
- [x] Wave 0: produce a create-only Aurora and RDS Proxy Terraform design.
- [x] Wave 0: refresh read-only ECS, EFS, target-group, and traffic facts.
- [x] Integrate Wave 0 and rerun platform, focused server, and harness checks.
- [x] Before wiring `PgSessionStore`, make `ConverseLoop` fail closed when a
  concurrent opposite confirmation decision wins the atomic update.
- [x] Add legacy-default PostgreSQL seams for app sessions and approvals.
- [x] Add a legacy-default PostgreSQL seam for agent gate state.
- [x] Add a production-safe legacy-default PostgreSQL seam for broker state.
- [x] Add a legacy-default PostgreSQL seam for jobs and callback replay nonces.
- [x] Add a legacy-default PostgreSQL seam for guest caps.
- [x] Add a legacy-default PostgreSQL seam for agent metering and tenant ops.
- [x] Add legacy-default PostgreSQL seams for drawing, upload, extraction, and purge authority.
- [x] Add fleet-wide PostgreSQL APS admission and concurrency slots.
- [x] Add explicit PostgreSQL selection for the harness session store.
- [x] Add fenced PostgreSQL harness repo leases and default-disabled authoring.
- [x] Prove empty and upgraded schema behavior against real PostgreSQL.
- [x] Add fail-closed app, broker, and harness schema readiness before health.
- [x] Build all three final runtime images.
- [ ] Run dual-write, shadow-read, restore, canary, and two-writer gates.

Risks:

- Local AWS credentials resolve to the account root. Do not mutate AWS.
- The AWS checkout is dirty and behind `origin/main`. Use a clean worktree from
  `origin/main` for Terraform changes.
- The full server suite has unrelated baseline failures. Use the recorded
  focused 107-test baseline plus subsystem tests added by each lane.
- Migration numbers are centrally allocated from `0011` through `0017`.

# Customization delivery waves

Source baseline: `origin/main` at `4b8771bd351f17526d69ba498136f91b79b161e1`.

Baseline gate:

- `LEAF_AUTOFILL_SOLVER_ABSENT_OK=1 python scripts/run-all-gates.py`
- 80 suites passed, 0 failed, 3 environment skips
- 1,178 tests passed

## Wave 0

- [x] Freeze `leaf.customization.v1` wire and storage contract.
- [x] Freeze `/api/author/register` as the only R6 publish route.
- [x] Freeze state transitions, approval binding, audit receipts, and feature flags.
- [x] Freeze platform-owned mutability and desired/effective release authority.
- [x] Add contract-freeze tests.

## Wave 1

- [x] Tenant Git change-set adapter with isolated refs and compare-and-swap updates.
- [x] SQLite coordination store with idempotency and recovery.
- [x] Platform release policy loader with strict path normalization.

## Wave 2

- [x] Split authoring into stage and publish operations.
- [x] Add desired/effective platform reconciliation.
- [x] Add tenant approval and staff authority separation.

## Wave 3

- [x] Add canonical R6 server route and close live direct-publish fallbacks when R5 is enabled.
- [x] Connect in-app R5 staging and R6 publish confirmation.
- [x] Extend deployment rollback to include effective catalog state.

## Wave 4

- [x] Frozen-path, self-approval, expiry, and prompt-injection falsification.
- [x] Git/SQLite crash, replay, and concurrent publication falsification.
- [x] Reconcile, deploy, and idempotent rollback falsification.

## Wave 5

- [ ] Merge and verify the staging-only code and infrastructure PRs.
- [ ] Build immutable images without production promotion.
- [ ] Internal-tenant R5 activation and evidence.
- [ ] Independent-approval R6 activation and rollback evidence.
- [ ] Controlled tenant expansion.
- [ ] Keep R7 disabled until the platform-admin path is separately proven.
- [ ] Keep `leafautomation.ai` and `www.leafautomation.ai` pinned until staging is 100% verified.

## Operator gates

- Provision a dedicated platform Postgres database and populate verified identity bindings before R5 activation.
- Select and enforce an independent required reviewer before R6 activation.
- Provision E2B credentials and prove the authored execution path before R6 execution.
- Keep the production aliases and production backend unchanged until the staging sign-off.
- Keep R7 absent until the platform-admin path is separately proven.
- Track the three moderate transitive Agent SDK audit findings. The exposed Hono/MCP server code is not imported or mounted by this harness.

## Adopted main repairs

- [x] Preserve the completed live edge-contract repair from `origin/main`.
- [x] Preserve authored-execution containment from `origin/main`.
