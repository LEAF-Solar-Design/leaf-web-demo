# Production authored acceptance

- [x] Keep the existing staging driver production-denied while exposing only target-neutral execution helpers.
- [x] Add a production-only driver with exact-host, execute-only, confirmation, and signed non-customer tenant gates.
- [x] Add the Auth0 machine-token classification claim required by the production driver.
- [x] Add negative contract tests for target aliases, missing confirmation, token signature and claim failures, and receipt leaks.
- [x] Run the focused Node tests, Auth0 action tests, web build, full applicable gates, and independent review.

Risks:

- A broad environment switch could weaken the existing staging production denylist.
- Unsigned JWT decoding cannot prove that the test identities are non-customer tenants.
- Production acceptance writes two durable synthetic drawings and tools, so target and run identity must be source-fixed.

Controls:

- Use a separate production entry point and keep the staging validator unchanged.
- Verify both access tokens against the exact Auth0 issuer, audience, JWKS, short lifetime, and signed `tenant_class` claim.
- Permit only `https://leaf-platform-web.vercel.app` for the web, `https://platform.leafdesign.ai` for the API, exact run-scoped drawing IDs, and source-fixed prompts.

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

# Automatic public solve-proof renewal

- [x] Replace the canned public solve with the broker-backed solve route.
- [x] Renew the proof once per 20-hour window with a stable broker event key.
- [x] Keep cache writes atomic and collapse concurrent refreshes into one run.
- [x] Rotate the ETag when the proof timestamp changes.
- [x] Add an hourly staging canary that verifies proof age and real solve evidence.
- [ ] Prove cache, expiry, failure, concurrency, CI, and staging behavior.

Risks:

- An expired proof must fail closed if the broker is unavailable. It must not
  present stale evidence as current.
- The public request path must not hold a process lock during broker or file IO.
- GitHub schedules can be delayed, so backend request-time renewal remains the
  source of truth and the canary runs away from the start-of-hour peak.

# Multiple Claude accounts per workspace

- [x] Freeze an additive, token-free account-list contract with stable account IDs and one active account.
- [x] Migrate legacy single-grant records on read and keep the execution port bound to the active account.
- [x] Add tenant-isolation, token-redaction, selection, removal, and legacy-compatibility tests.
- [x] Update the Leaf Platform panel so a linked workspace can add, select, and remove accounts.
- [x] Run focused server, harness, and website build gates.
- [ ] Record staging deployment and rollback evidence without using the local root AWS identity.

Risks:

- Account IDs and labels must never reveal credential material.
- A tenant must not select or remove another tenant's account.
- Removing the active account must select a deterministic survivor or report no active grant.
- The existing v1 record and single-account API fields must remain compatible during rollout.

# Active tenant authority for drawing routes

- [x] Reproduce the stale JWT tenant claim selecting the demo bootstrap.
- [x] Resolve live account drawing routes through the active platform binding.
- [x] Prove account intake serves the uploaded DXF and creates no stale-tenant manifest.
- [x] Run the focused and full backend gates.

Risks:

- Guest sessions and trusted broker back-edges must keep their existing tenant identity.
- All drawing reads and mutations must use one tenant key.

# Approval drawing binding recovery

- [x] Persist the proposed drawing in the app approval row without a schema migration.
- [x] Forward the stored drawing on the server-authored confirmation wire.
- [x] Rebuild a missing harness confirmation mirror with the exact drawing-bound args.
- [x] Replace the vacuous hash-mismatch check with a real proposal, mirror loss, approval, and replay test.
- [ ] Run focused server and harness tests, type-check, build, and the full repository gate.

Risks:

- Approval arguments are security-sensitive and must remain byte-equivalent across the app and harness.
- Existing approvals without a stored drawing must fail closed instead of gaining a new target.
- The client cannot choose or alter the drawing during confirmation replay.

# Orbitable 3D cat proof

- [x] Add a browser assertion for real panel depth and camera orbit.
- [x] Render the cat version as an extruded panel sculpture without changing its drawing evidence.
- [x] Enable orbit, pan, and zoom only on the interactive cat surface.
- [x] Keep proposal, approval, version, Undo, and Redo behavior unchanged.
- [x] Run the browser proof, production build, focused server tests, and self-review.

Risks:

- The display treatment must not alter panel identity or the oracle input.
- Default CAD viewers must retain their top-down pan and zoom controls.
- Camera interaction must remain available without covering the operator controls.

# Authored CAD production readiness

- [x] Reconcile the recovered cat branch with current application main.
- [x] Verify live staging and production service, image, database, and runtime posture.
- [x] Freeze the production product, security, durability, staging, and cutover gates.
- [x] Replace the fixed-template E2B author step with a general sandboxed author boundary.
  - [x] Remove model-controlled repository writes while retaining read-only inspection.
  - [x] Accept source and manifest metadata through one structured harness tool.
  - [x] Reject duplicate, unsafe, oversized, or mismatched proposals before any write.
  - [x] Write only `tools/<name>/tool.py` and `tool.json`, then verify exact bytes before commit.
  - [x] Keep the Claude grant outside the broker and generated-code sandbox.
  - [x] Require the broker test run before a candidate can be returned.
  - [x] Prove a novel `drawing.write` proposal through hermetic boundary tests.
- [x] Add a non-mocked deployed-environment acceptance driver.
  - [x] Require an HTTPS staging target, exact source revision, two distinct tenant JWTs, and acceptance-only drawing IDs.
  - [x] Prove readiness, coherent build identity, linked grants, tenant isolation, and a blank unique browser workbench without route interception.
  - [x] Put all mutations behind an explicit staging-only execute flag and emit a secret-free receipt.
  - [x] Require separate approval authority for the exact staged change and reject generic browser approvals.
  - [x] Add hermetic negative tests for production targets, missing identity, mixed revisions, degraded dependencies, token leakage, and cross-tenant access.
  - [x] Register the driver contract tests in the canonical gate.
- [ ] Enable and verify one coherent release in staging.
- [ ] Wire the proven posture into production.
  - [ ] Add a protected main-only Vercel production workflow.
  - [ ] Consume one exact handoff receipt and its exact release web artifact.
  - [ ] Deploy the existing web bytes without rebuilding them.
  - [ ] Verify the immutable deployment URL and stable production routes.
  - [ ] Publish a sanitized run-attempt-bound deployment receipt.
- [ ] Promote exact staging digests and complete the cutover receipt.

Risks:

- A healthy service with authored execution off does not satisfy the product contract.
- The E2B author runner can propose a novel drawing-write tool, but staging has
  not yet produced the live broker execution receipt.
- Enabling authored execution without PostgreSQL sessions, an E2B credential, and an
  explicit allowlisted probe fails production startup.
- The live staging services currently use images from several source commits.
- Cloud mutation must use a named federated identity, never the root identity.
- Vercel production publication must require an exact independent issue approval,
  repository-scoped Vercel secrets, and must never accept a branch, preview, or
  unverified build artifact.

# Staging Team and Enterprise subscription mounts

- [x] Require the active tenant owner to mount, list, select, and remove Claude accounts.
- [x] Route each live turn to the least-used authorized mount within the same tenant.
- [x] Record token-free per-account usage and bounded quota cooldown state.
- [x] Keep the anonymous CAD demo local and enable real conversation for signed-in users on the same CAD surface.
- [x] Add account labels, status, automatic routing state, and removal to the mounted-account panel.
- [x] Prove tenant isolation, role denial, secret redaction, routing, quota behavior, and browser behavior.
- [ ] Deploy exact app, harness, and web commits to staging through protected GitHub workflows.
- [ ] Verify the signed-in staging flow and record rollback evidence.

Risks:

- Consumer Free, Pro, and Max credentials must not enter this commercial mounting lane.
- Routing must never pool credentials across tenants or bypass provider limits.
- Credential material must stay server-side and must never enter logs, responses, browser storage, or usage records.
- Concurrent turns must not lose usage updates or select disabled accounts.
- Anonymous demo traffic must never reach a mounted private account.

# First-login subscription mount access

- [x] Distinguish a valid but unprovisioned Auth0 identity from an expired session.
- [x] Let that signed-in user create the first Leaf workspace owner binding on the CAD surface.
- [x] Retry the live session after workspace creation and expose the Claude mount panel.
- [x] Prove the 403, bootstrap, retry, owner-only mount path in browser and backend tests.
- [ ] Merge and deploy the exact app and web commits through the protected staging workflow.
- [ ] Verify live Auth0 entry, ECS source identity, health, and rollback baselines.

Risks:

- A missing post-login tenant claim must not be mistaken for a provisionable workspace.
- An unauthenticated or expired session must never create a tenant.
- Workspace creation must remain an explicit user action and must not silently recreate an offboarded tenant.
- No Claude credential may enter browser storage, logs, or a response.

# Canonical tenant subscription routing

- [x] Resolve intake and all conversation session routes through the active platform tenant binding.
- [x] Resolve approval decisions through the same tenant authority as the mounted account.
- [x] Keep a downstream `grant_required` response from deleting the separate Leaf login token.
- [x] Prove stale JWT tenant claims cannot split mount storage from live turn routing.
- [ ] Merge, deploy the app-only fix, and complete a real mounted turn on staging.

# Version delta and restore repair

- [x] Keep `/try` on the default versions response without delta computation.
- [x] Prove delta chips and restore-as-new-head on the mounted `/app` drawer.
- [x] Preserve an unreadable-head warning through history refresh and skip the viewer refresh.
- [x] Add a PostgreSQL-authoritative raw-DWG restore proof with cache validation.
- [ ] Run that proof with an explicit test `DATABASE_URL`; this workstation has none.

# PostgreSQL authority contract repair

- [x] Port PR #187 relation, column, constraint, index, and trigger authority checks onto current main.
- [x] Preserve the upload-to-drawing selector dependency in the authority inventory.
- [x] Register the static and live PostgreSQL contract tests with measured current counts.
- [x] Run focused tests, replay, and the full hermetic gate; record the unavailable local PostgreSQL gate.
- [x] Confirm the diff preserves release, cutover, and PR #236 functionality.

Risks:

- A weak catalog check can report ready for a same-name object on the wrong relation.
- A stale test floor can hide an unregistered authority proof or fail a valid current tree.
- Stricter startup checks must not change selector state or claim that a PostgreSQL cutover occurred.

# Staging author-tool refusal recovery

- [x] Preserve the requested authoring mode through approval replay and the app back-edge.
- [x] Return a safe, actionable customization error instead of hiding every reason behind a generic refusal.
- [x] Add server and harness regressions for disabled R5, mode binding, and reason-code propagation.
- [ ] Run focused tests, type-check, build, full gates, review, and protected staging deployment.

Verification note: focused server and harness checks, type-check, and build pass.
The registered local subset passed every customization gate, but the full
harness suite hit Windows Git-worktree timeouts. Protected Linux CI remains the
canonical full-suite gate before merge and deployment.

Risks:

- Authoring approval arguments and idempotency keys must bind the requested mode.
- Disabled authored execution must remain fail closed until the E2B and independent-approval gates pass.
- Error details must explain operator state without exposing tenant data or credentials.
