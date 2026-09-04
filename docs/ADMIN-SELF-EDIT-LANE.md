# Admin self-edit lane (W14 / R7)

Admin accounts change the platform itself from inside the platform — through
the SAME pipeline every other change rides, never around it. This is the gated
design doc the agent spine's U6 row required before any R7 rung could
activate. Lane L6 of the 2026-07-30 wire-up plan; server-side mount landed
2026-07-30.

**Scope boundary (D-2):** this is the ADMIN-trust lane. It is distinct from
the PARKED tenant fork-deploy goal (R7 tenant-facing surfaces, model bytes in
forks) — that stays design-gated per the D-2 scope-exclusion ruling. Nothing
here opens anything for non-admin tiers.

## The trust model: three independent operator gates

A `customize_platform` invocation reaches the lane only through ALL of:

1. **Tier** — the `admin` tier is the ONLY entitlement entry carrying
   `platform_customize: true` (contract/AUTH.md §11.2, freeze-gated). Live
   auth resolves tier from the STORED org row (billing authority, and
   tier-sync would overwrite an org-row grant), so `admin` is a SUBJECT-level
   elevation instead (`deps.admin_elevated_tier`) needing BOTH: the VERIFIED
   token claim `tier=admin` — minted solely from a root-level
   `app_metadata.leaf_admin === true` flag an operator sets by hand; no
   billing plan maps to it and `billing_tiers.derive_tier` can never return
   it — AND the subject on the server-owned `LEAF_PLATFORM_ADMIN_SUBJECTS`
   allowlist. Either alone grants nothing; removing either revokes.
2. **Rollout** — `LEAF_CUSTOMIZATION_R7_MODE=internal` plus the tenant on
   `LEAF_CUSTOMIZATION_INTERNAL_TENANTS`. Mode `all` deliberately reads as
   OFF for R7 (`customization_flags.py`): platform self-edit is an
   allowlisted-accounts lane and must never be one env typo away from "every
   tenant". Neither deployed env sets the mode yet, so the lane ships dark.
3. **Approval** — the catalog entry (`agent_policy.json`) stays
   `always-confirm` with `tenant_tightenable: false`: every invocation takes a
   fresh approval; no tier, flag, or overlay can soften it.

The harness back-edge allowlist (wire contract §0) is NOT widened: the R7
routes authenticate with the admin's own tenant JWT. The harness spine tool
stays unmounted (agent spine §18 unchanged).

## Branch-only writes against the platform repo

`server/platform_customize.py` targets the platform repository itself
(`LEAF_PLATFORM_REPO_DIR`), and every ref write and push refspec passes ONE
chokepoint that accepts exactly `refs/heads/admin-customize/<change-uuid>` —
`main`, `master`, tags, and everything else are refused with
`protected_ref_refused`, and pushes are refspec-pinned and never forced.

* `POST /api/platform/customize` — propose: file edits are path-validated (no
  traversal, no `.git`, no symlink escape), applied in a throwaway detached
  worktree off the `refs/heads/main` tip, committed once with the proposing
  tenant/subject in trailers, and published only at the lane ref. `main` and
  HEAD are provably untouched (test-pinned).
* `GET /api/platform/customize/{id}` — status (tenant-scoped; foreign ids read
  as absent).
* `POST /api/platform/customize/{id}/land` — the HANDOFF. The body must name
  the EXACT commit (`{"commit_sha": ...}`) — the API lane's fresh
  per-invocation approval, mirroring the catalog's always-confirm posture.
  Verifies the lane ref still names the recorded commit, then pushes
  SHA-pinned (`<commit_sha>:refs/heads/admin-customize/<id>` — a ref moved
  between check and push cannot smuggle different bytes) when
  `LEAF_PLATFORM_REPO_PUSH=1` (remote `LEAF_PLATFORM_REPO_REMOTE`;
  credential-bearing URLs refused, userinfo redacted from error detail), and
  returns the landing receipt. With push off the branch stays local and the
  receipt says so. Landing serializes on a one-shot marker AFTER delivery
  (push-then-mark), so a failed push stays retryable and double-lands
  collapse to one record transition.

Change records are one JSON file per change under
`LEAF_PLATFORM_CUSTOMIZE_STATE_DIR` (O_EXCL create, atomic replace — the
approvals-dir idiom; no database).

## The read side: the lane's eyes (2026-08-27)

An agent that can propose edits but cannot see the repository proposes BLIND —
the live failure mode was an orphan stylesheet invented for a theme change
because the agent could not find the real theme files, plus a duplicate
proposal after a session break lost the change_id. Three GET-only routes fix
both, all behind the SAME `_gate` admission chain (admin tier + R7 rollout),
none of them able to write anything:

* `GET /api/platform/customize` — tenant-scoped, newest-first change listing
  (bounded page, at most 50 rows) so a conversation recovers a lost change_id
  instead of re-proposing. Rows carry the landing essentials (id, state, exact
  commit_sha); the listing reconciles from durable markers but deliberately
  never observes GitHub — review freshness stays a per-id status concern, so a
  listing can never fan out N network round trips.
* `GET /api/platform/source?path=` — ONE file at the base-ref tip, read from
  the git OBJECT STORE only (`cat-file`), never the working tree: no
  filesystem walk, so no symlink or traversal surface, and what the agent
  reads is exactly the base a propose() builds on. Same path discipline as an
  edit, size-checked before the bytes leave git (256 KiB cap), binary content
  reported by size and never returned.
* `GET /api/platform/source/tree?dir=` — ONE directory level (bounded at 500
  entries with an honest `truncated` flag).

On the spine these ride the `customize_platform` tool as ops `list`,
`read_source`, `list_source` (plus the existing `status`), which the harness
consults as `read_platform_state` (policy `auto`) — the same read-rung mapping
`status` has always used. The always-confirm posture on propose and land is
untouched: reads widen what the agent can SEE, never what it can DO, and the
spine prompt now instructs it to look before proposing.

## The landing path is the EXISTING pipeline

The lane produces a branch. Everything after is the standing machinery,
unchanged:

```
branch → pull request → sol-critic review gate → merge
       → ECS staging canary → production
```

Rollback at the deploy step is the previous ECS task-definition revision —
the same rollback every deploy names. The lane never deploys and never
touches ECS; a compromised or mistaken admin change is stopped where every
other bad change is stopped: at the review gate. Every landing receipt embeds
this contract (`landing_path` in the response) so no session can honestly
claim a shorter path.

**Merge on operator approval (issue #422 Phase 3):** once the standing
reviewer has PASSED the exact landed commit and the PR is open, the drawer
offers Merge. It is NOT auto-merge: the operator approves freshly, naming the
exact commit (the land-ack idiom, one-shot marker), and the server then
re-verifies everything at merge time with a fresh, uncached observation —
review passed, PR open, head still equal to the recorded commit — before
issuing GitHub's own sha-pinned squash merge, which refuses if the head moved
in between. Its own kill switch (`LEAF_PLATFORM_MERGE_ENABLED`, default OFF)
and its own credential (`LEAF_PLATFORM_MERGE_TOKEN`: Contents write + Pull
requests write — necessarily the most powerful of the three tokens, so it
revokes independently; **emergency containment now names three tokens**).
Fundamental-path changes still show the durable co-sign marker before merge —
merging is never a path around co-sign. The route is NOT on the harness
back-edge: the drawer is the only door, and the approving subject is recorded
on the merge receipt.

**PR auto-open (issue #422 Phase 1):** when `LEAF_PLATFORM_PR_OPEN=1` and a
PR-scoped token is configured, a successful land also OPENS the pull request
(`LEAF_PLATFORM_PR_REPO`, head = the lane branch, base = the proposal base)
and records `pr: {number, url}` on the change record; the drawer renders the
link inline. This automates the walk to the gate, not the gate: review and
merge are untouched. It is best-effort by contract — any failure is recorded
as `pr: {error}` and the land still succeeds; replaying land with the same
exact-commit ack retries it. The token is a SEPARATE fine-grained PAT with
Pull-requests write only (never Contents), so the push credential and the
PR credential revoke independently — emergency containment now names two
tokens to revoke instead of one.

**Review observation (issue #422 Phase 2):** on status reads of a landed,
PR-carrying record, the platform OBSERVES the standing gate — the
`sol-critic-review` commit status at the PR's current head (the fleet
reviewer posts every round's verdict since 2026-08-04: pending on dispatch,
success on PASS, failure on RED, error on no-verdict) — and caches it on the
record as `review: {state, pr_state, head_sha, description}` (60s cache;
observation stops once the PR is merged or closed). Read-only by contract:
the platform never runs, simulates, or gates anything on the review. A fix
round pushed to the PR moves the head, and the observation honestly reports
that new head as unreviewed until its own round posts. Requires the same PAT
to ALSO carry **Commit statuses: read** — edit the existing fine-grained
token's permissions in place (the token value is unchanged, so no secret
rotation); until then `review.state` reads `unknown`, which is the honest
degraded mode, never an error.

## Co-sign on fundamental paths

Changes touching **auth, billing, or the agent spine** (manifest:
`server/platform_fundamental_paths.json`) enter `awaiting_cosign` and cannot
land until an independent approver presents
`LEAF_CUSTOMIZATION_APPROVAL_SECRET` — the same independent-approval
credential the R6 lane uses; the authoring harness never holds it — on
`POST /internal/platform-customize/cosign` (or `/deny`), naming the EXACT
commit sha. The verdict is claimed via a one-shot O_EXCL marker (the durable
authority — a racing approve/deny resolves to exactly one winner, and landing
re-checks the marker, not just the rewritable record). **Trust boundary,
stated honestly**: possession of the approval secret IS the co-sign authority;
`X-Approver-Subject` is the secret-holder's attested audit label, and the
self-approval refusal (`cosign_self_approval`) is hygiene against honest
mistakes, not authentication of the approver. The manifest protects itself:
it, the lane's own module, the entitlement/policy files, the CI workflows, and
`deploy/**` are all fundamental (matched case-insensitively, so a
case-variant spelling on a case-insensitive checkout cannot slip past), so
the co-sign requirement cannot be edited away without a co-sign.

Fail-closed rules: manifest ABSENT → every path is fundamental; manifest
present but corrupt → the lane refuses service (503); approval secret unset →
no co-sign can ever verify.

## Expand-contract migration gate (CI)

`scripts/migration_expand_contract_gate.py`, registered in
`scripts/run-all-gates.py` (which `test-gate.yml` runs on every PR): a
contract-phase statement in a new `platform/migrations/NNNN_*.sql` (DROP
TABLE/COLUMN/INDEX/VIEW/CONSTRAINT, RENAME, ALTER COLUMN TYPE, SET NOT NULL,
TRUNCATE, DELETE FROM) fails CI unless the file carries
`-- expand-contract: contract-of=NNNN` naming the earlier expand migration it
completes. The ECS deploy overlaps old and new tasks, so an unmarked contract
step breaks the canary window and forecloses the task-def rollback — the gate
makes that unlandable, admin lane or not. Migrations 0001–0022 are
grandfathered by number (the ledger pins their bytes); the set is frozen.

## Operator runbook

Enable (per admin account, staging first):

1. Auth0 dashboard: set `app_metadata.leaf_admin = true` (root level) on the
   account; re-paste/deploy `post-login-add-tenant-claim.js` (the Action gained
   the admin override — redeploy is manual by design).
2. App env: `LEAF_PLATFORM_ADMIN_SUBJECTS=<auth0|sub,...>` (the server-owned
   half of the elevation), `LEAF_CUSTOMIZATION_R7_MODE=internal`,
   `LEAF_CUSTOMIZATION_INTERNAL_TENANTS=<tenant_id,...>`,
   `LEAF_PLATFORM_REPO_DIR=<platform repo checkout/mirror>`; optionally
   `LEAF_PLATFORM_REPO_PUSH=1` + a push-capable remote (named remote or
   credential helper — URLs carrying userinfo are refused) and
   `LEAF_PLATFORM_CUSTOMIZE_STATE_DIR` on durable storage.
   `LEAF_CUSTOMIZATION_APPROVAL_SECRET` is already deployed in both envs.
   Optional PR auto-open + review observation (#422 Phases 1-2):
   `LEAF_PLATFORM_PR_OPEN=1`, `LEAF_PLATFORM_PR_REPO=<owner/repo>`,
   `LEAF_PLATFORM_PR_TOKEN=<fine-grained PAT with exactly two permissions:
   Pull-requests read+write AND Commit-statuses read — never Contents; it
   stays a separate token from the Contents push PAT so each revokes
   independently>`.
   Optional merge-on-approval (#422 Phase 3, blast radius = main — enable
   LAST): `LEAF_PLATFORM_MERGE_ENABLED=1`,
   `LEAF_PLATFORM_MERGE_TOKEN=<fine-grained PAT: Contents read+write AND
   Pull-requests read+write, THIRD token, never shared with the other two>`.
   Optional delivery-receipt reads (`GET /api/receipts`, slice 12a):
   `LEAF_RECEIPTS_GITHUB_TOKEN=<fine-grained PAT with exactly ONE permission:
   Actions read — FOURTH token, never shared with the other three>` and
   optionally `LEAF_RECONCILER_RECEIPT_URL` (https only) for the reconciler feed. This one is
   separate for a concrete reason rather than symmetry: the Actions artifacts
   and runs APIs the receipts reader calls need `Actions: read`, which the PR
   PAT above deliberately does not carry. Leave it unset and both artifact
   sources answer `source_unreachable` and render no rows — the honest inert
   state, and the intended one until an operator decides to grant it. Do NOT
   widen the PR PAT to make receipts work; that couples two revocations that
   are kept separate on purpose.
3. Verify dark-ness elsewhere: every non-admin tier answers 403
   `entitlement_required: platform_customize`; a non-allowlisted admin answers
   404 `platform_customize_disabled`. That same 403 covers
   `GET /api/receipts?scope=pr:|tree:|train`, which reads this repository's
   private CI state with the platform's own credential and therefore rides the
   same admission as `GET /api/platform/source`; `scope=job:` is tenant data
   and is bound to the calling tenant instead.

Disable (any one suffices; all are env-only, no deploy):
unset `LEAF_CUSTOMIZATION_R7_MODE` · remove the tenant from the allowlist ·
remove the `leaf_admin` flag · the standing agent kill file
(`LEAF_AGENT_KILL_FILE`) stops the conversational path platform-wide.

## Deliberately out of scope

* Harness spine tool mapping for `customize_platform` (§18) and any §0
  back-edge widening — a later contract revision if conversational dispatch
  is wanted; the API lane works without it.
* Tenant-facing fork-deploy (D-2: PARKED; R7 tenant surfaces need their own
  design and canon ruling on model-bytes-in-forks).
* AUTO-merge and any deploy of landed branches. PR opening is automated
  (#422 Phase 1), the review verdict is observed (#422 Phase 2), and merge
  exists behind a fresh operator approval of the exact commit (#422 Phase 3)
  — but nothing in this lane merges without that approval, and nothing here
  deploys, ever. The staged-rollout stage (#422 Phase 4) is not designed yet.
