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

Source baseline: `origin/main` at `6fbc2c1d8029aaeec5ace8d258a6f7ede0fb1e3d`.

Baseline gate:

- `LEAF_AUTOFILL_SOLVER_ABSENT_OK=1 python scripts/run-all-gates.py`
- 68 suites passed, 0 failed, 3 environment skips
- 1,098 tests passed

## Wave 0

- [x] Freeze `leaf.customization.v1` wire and storage contract.
- [x] Freeze `/api/author/register` as the only R6 publish route.
- [x] Freeze state transitions, approval binding, audit receipts, and feature flags.
- [x] Freeze platform-owned mutability and desired/effective release authority.
- [x] Add contract-freeze tests.

## Wave 1

- [ ] Tenant Git change-set adapter with isolated refs and compare-and-swap updates.
- [ ] SQLite coordination store with idempotency and recovery.
- [ ] Platform release policy loader with strict path normalization.

## Wave 2

- [ ] Split authoring into stage and publish operations.
- [ ] Add desired/effective platform reconciliation.
- [ ] Add tenant approval and staff authority separation.

## Wave 3

- [ ] Add canonical R6 server route and close live direct-publish fallbacks.
- [ ] Connect in-app R5 staging and R6 publish confirmation.
- [ ] Extend deployment rollback to include effective catalog state.

## Wave 4

- [ ] Frozen-path, self-approval, expiry, and prompt-injection falsification.
- [ ] Git/SQLite crash, replay, and concurrent publication falsification.
- [ ] Reconcile, deploy, and idempotent rollback falsification.

## Wave 5

- [ ] Dark deploy with R5, R6, and R7 disabled.
- [ ] Internal-tenant R5 activation and evidence.
- [ ] Independent-approval R6 activation and rollback evidence.
- [ ] Controlled tenant expansion.
- [ ] Keep R7 disabled until the platform-admin path is separately proven.

## Risks

- Existing `POST /api/author` publishes directly through both harness and template paths.
- Existing `AuthorLoop.build()` registers and commits in one call.
- Tenant Git in-place mode has no isolated change ref or compare-and-swap update.
- Git and SQLite cannot share one transaction, so recovery must be explicit.
- Existing ECS rollback restores images but not the effective tenant catalog.
- The harness dependency tree currently reports nine audit findings. Review them before activation without bulk upgrading unrelated packages.

## Adopted main repairs

- [x] Preserve the completed live edge-contract repair from `origin/main`.
- [x] Preserve authored-execution containment from `origin/main`.
