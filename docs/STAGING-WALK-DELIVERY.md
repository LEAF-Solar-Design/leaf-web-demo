# Staging walk delivery preflight

Set `LEAF_E2E_EXPECTED_SHA` to the full, lowercase 40-character Git commit intended
for this particular staging walk, using the existing staging walk invocation.
For example, in PowerShell, set `$env:LEAF_E2E_EXPECTED_SHA = '<40-character commit>'`
before invoking the staging walk. The runner never infers this value from HEAD
or origin/main. An invalid value fails configuration. Unset or empty means
`not_configured`: the existing walk runs without delivery HTTP or Git calls.

After the existing staging host and token identity guards, and before installing
browser monitors or accessing project state, the walk reads public `/api/health`
and `/health.json` at the exact staging origin. These are unauthenticated Node
GETs, with credentials omitted, redirects disabled, no-store caching, no retry,
a five-second bound including each body and a 4096-byte body limit.

The decision is `current` when both baked source SHAs equal the expected SHA.
It is `stale` only when both are equal or locally proven strict ancestors and at
least one is an ancestor. That decision persists evidence and an
`infra_stale_delivery` annotation before calling Playwright's actual skip.
`mismatch` means a component is proven not to be an ancestor; `unverified` means
the observations or ancestry cannot establish the comparison. Both retain the
existing UI verdict. Missing, unhealthy, malformed, oversized, timed-out or
redirect-refused health responses cannot prove stale delivery. A wrong response
origin remains a security failure.

App adoption metadata is a conservative veto: `adopted`, `build_unknown`, invalid
or inconsistent metadata yields `unverified`. `built` requires matching release
and baked SHAs. `unattested` with null release and legacy responses omitting both
metadata fields remain eligible for comparison. The release SHA never replaces
the baked app SHA. Digest-bound adoption receipts are outside this preflight.

Ancestry uses only local commit objects in this helper's repository, with lazy
fetch and replacement objects disabled. Both commits are probed before
`merge-base --is-ancestor`. The total ancestry budget is two seconds; no fetch,
checkout or repair occurs. Missing commits, timeouts, unavailable Git, and
negative results from shallow repositories yield unknown. A positive strict
ancestry proof is sufficient even in shallow history.

Existing per-test JSON artifact filenames under `web/artifacts/walk-staging/`
remain unchanged. `projectSolar.delivery` has schema `leaf.staging-delivery.v1`,
UTC `checkedAt`, `expected_sha`, app and web `source_sha`, fixed observation and
relation labels, HTTP status and overall decision. App also retains validated
`release_source_sha` and a known `source_identity` enum or `unknown`. Missing or
invalid SHAs are null. Raw bodies, rejected values, headers and exception text
are never included. Observation labels are `not_configured`, `ok`, `invalid`,
`unavailable` and `identity_unverified`; relation labels are `equal`, `ancestor`,
`not_ancestor` and `unknown`. `projectSolar.skipReason` and classification reasonCode are
`infra_stale_delivery` for the explicit preflight skip. Evidence write failures
cannot change skipped, passed or failed verdicts. Delivery evidence cannot turn
a later UI failure into a skip; there is no post-failure health check.

This is a point-in-time observation, not an atomic guarantee across replicas or
a deployment changing during a walk. A skip makes no product correctness or
availability claim, and an all-skipped suite is not successful feature verification.

Offline verification: `npm --prefix web run check:walk-unit`. Tests inject health,
Git, browser and persistence adapters; they need no staging access or Git history.
Rollback: revert this change. With no expected SHA configured, delivery preflight
leaves the existing walk behavior intact.
