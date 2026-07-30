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
   `platform_customize: true` (contract/AUTH.md §11.2, freeze-gated). The tier
   is minted solely from a root-level `app_metadata.leaf_admin === true` flag
   an operator sets by hand in the Auth0 dashboard; no billing plan maps to it
   and `billing_tiers.derive_tier` can never return it. Revocation = remove
   the flag.
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
* `POST /api/platform/customize/{id}/land` — the HANDOFF: verifies the lane
  ref still names the recorded commit, optionally pushes
  (`LEAF_PLATFORM_REPO_PUSH=1`, remote `LEAF_PLATFORM_REPO_REMOTE`), and
  returns the landing receipt. With push off the branch stays local and the
  receipt says so.

Change records are one JSON file per change under
`LEAF_PLATFORM_CUSTOMIZE_STATE_DIR` (O_EXCL create, atomic replace — the
approvals-dir idiom; no database).

## The landing path is the EXISTING pipeline

The lane produces a branch. Everything after is the standing machinery,
unchanged:

```
branch → pull request → sol-critic review gate → merge
       → ECS staging canary → production
```

Rollback at the deploy step is the previous ECS task-definition revision —
the same rollback every deploy names. The lane itself never merges, never
deploys, never touches ECS; a compromised or mistaken admin change is stopped
where every other bad change is stopped: at the PR gate. Every landing receipt
embeds this contract (`landing_path` in the response) so no session can
honestly claim a shorter path.

## Co-sign on fundamental paths

Changes touching **auth, billing, or the agent spine** (manifest:
`server/platform_fundamental_paths.json`) enter `awaiting_cosign` and cannot
land until an independent approver presents
`LEAF_CUSTOMIZATION_APPROVAL_SECRET` — the same independent-approval
credential the R6 lane uses; the authoring harness never holds it — on
`POST /internal/platform-customize/cosign` (or `/deny`), naming the EXACT
commit sha. Self-approval is refused (`cosign_self_approval`). The manifest
protects itself: it, the lane's own module, the entitlement/policy files, the
CI workflows, and `deploy/**` are all fundamental, so the co-sign requirement
cannot be edited away without a co-sign.

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
2. App env: `LEAF_CUSTOMIZATION_R7_MODE=internal`,
   `LEAF_CUSTOMIZATION_INTERNAL_TENANTS=<tenant_id,...>`,
   `LEAF_PLATFORM_REPO_DIR=<platform repo checkout/mirror>`; optionally
   `LEAF_PLATFORM_REPO_PUSH=1` + a push-capable remote and
   `LEAF_PLATFORM_CUSTOMIZE_STATE_DIR` on durable storage.
   `LEAF_CUSTOMIZATION_APPROVAL_SECRET` is already deployed in both envs.
3. Verify dark-ness elsewhere: every non-admin tier answers 403
   `entitlement_required: platform_customize`; a non-allowlisted admin answers
   404 `platform_customize_disabled`.

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
* Auto-merge or auto-deploy of landed branches — the PR gate is the point.
