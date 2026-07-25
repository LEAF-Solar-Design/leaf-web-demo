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
