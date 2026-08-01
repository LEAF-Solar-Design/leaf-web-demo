# Honest live cat flow

- [x] Refuse PostgreSQL drawing writes without an active checkout before durable job submission.
- [x] Keep the worker checkout preflight as the race-closing second gate.
- [x] Start normal `/try` with no drawing ID, version, sample intake, or session request.
- [x] Preserve explicit `?demo=1`, `?proof=1`, and protected seeded-drawing behavior.
- [x] Bind a successful upload receipt to the shared drawing, conversation, and version controllers.
- [x] Add negative route tests, live empty-state browser tests, and upload-to-session binding tests.
- [x] Run focused tests, web build, and an independent final diff review.
- [ ] Rebase onto current main and pass the full repository gate.

Risks:

- Checkout capability is security-sensitive. Missing, stale, or cross-drawing proof must not create a job.
- Authenticated staging must never fall back to `rooftop_demo` or a generated drawing identity.
- Upload must switch all shared controllers to the receipt drawing without stale conversation or version state.
- This branch must not alter PR #380 or dispatch a staging workflow.
