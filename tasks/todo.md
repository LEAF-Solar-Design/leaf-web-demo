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
