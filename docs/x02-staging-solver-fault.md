# X02 staging solver fault seam

Parent: Studio X-bench X02, frozen cases 5 and 6.

Use normal authenticated `POST /api/run` for `solar-solve-proposal`, with the existing exact catalog, explicit organization/project, version, checkout, grant and idempotency inputs. Add `X-XBench-Solver-Fault: upstream-400` or `X-XBench-Solver-Fault: timeout`. The deployment identity must validate as staging and the runtime must be deployed and non-production. Local, production, unauthenticated, conversational-authority and noncanonical requests cannot activate the seam. Missing header preserves ordinary behavior. No settings or provider grants are changed.

The scoped request mode is copied into `execution_context.xbench_solver_fault` and the canonical submission fingerprint. Reusing a key with a different mode conflicts. Normal replay returns the existing job. The worker rechecks posture before touching the exchange, labels the reservation synthetic and records send intent through the existing durable exchange before running a per-call transport stub. No global transport is patched and no socket or provider is contacted by the stub. Existing grant authorization still runs.

`upstream-400` makes the normal cloud adapter see HTTP 400 and return `CLOUD_REQUEST_REJECTED`. `timeout` fires a deterministic transport deadline, returning `solve_outcome_unknown` through the normal adapter. The unknown exchange is preserved; reclaim calls `resume_exchange` and cannot send again. Fault jobs use single-send custody even when normal async protocol is enabled. This is a deterministic fault seam, not a real indefinite network wait. Split requests remain unsupported for this seam.

The tests in the existing registered proposal files cover context/fingerprint binding, strict staging guard, per-call transport outcomes, durable unknown custody and no second send. PostgreSQL assertions run in selected CI; local protocol doubles are not database proof. UI/DOM and a scored staging walk still need their own exact-source receipt. The hook does not claim general engine provenance or fix AddAllInverters.
