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
