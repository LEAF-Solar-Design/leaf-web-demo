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

- [x] Wave 0: add shared PostgreSQL transaction and counter primitives.
- [x] Wave 0: add a PostgreSQL `SessionStore` implementation and contract tests.
- [ ] Wave 0: produce a create-only Aurora and RDS Proxy Terraform design.
- [x] Wave 0: refresh read-only ECS, EFS, target-group, and traffic facts.
- [ ] Integrate Wave 0 and rerun platform, focused server, and harness checks.
- [ ] Before wiring `PgSessionStore`, make `ConverseLoop` fail closed when a
  concurrent opposite confirmation decision wins the atomic update.
- [ ] Implement each remaining mutable authority behind a legacy-default feature flag.
- [ ] Prove empty and upgraded schema behavior against real PostgreSQL.
- [ ] Run dual-write, shadow-read, restore, canary, and two-writer gates.

Risks:

- Local AWS credentials resolve to the account root. Do not mutate AWS.
- The AWS checkout is dirty and behind `origin/main`. Use a clean worktree from
  `origin/main` for Terraform changes.
- The full server suite has unrelated baseline failures. Use the recorded
  focused 107-test baseline plus subsystem tests added by each lane.
- Migration numbers are centrally allocated from `0011` through `0017`.
