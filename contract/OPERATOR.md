# OPERATOR.md — operator control plane contract (v1)

Addendum to the frozen `contract/CONTRACT.md` and `contract/AUTH.md`. Owns the
operator control plane vocabulary. Nothing here modifies a tenant surface:
the AUTH.md §11 vocabularies, the tenant agent catalog
(`server/agent_policy.json`), the tenant session wire (§18 / the pinned wire
contract), `SPINE_TOOL_NAMES`, and the harness back-edge allowlist are all
byte-identical under this contract, and the freeze gates named in §8 prove it.

Status at first landing: CONTRACT ONLY. No operator behavior ships with this
file; Wave 1 lanes implement against it. Promoting later growth of any set
below follows the same operator ritual as AUTH.md §11: amend the section and
its gate test in one PR.

## 1. Operator principal

Operator authority is never carried in a token. A verified Auth0 RS256 JWT
(the existing `server/auth.py` verifier, unchanged) supplies only the
`sub` claim as `subject`. The sole grant is a server-owned PostgreSQL record:

```
operator_principals (
  subject        TEXT PRIMARY KEY,   -- verified Auth0 sub
  role           TEXT NOT NULL,      -- 'operator' (the only v1 role)
  role_revision  INTEGER NOT NULL,   -- monotonic; bumped on ANY row change
  status         TEXT NOT NULL,      -- 'active' | 'suspended' | 'revoked'
  profiles       TEXT[] NOT NULL,    -- allowed operator profiles
  environment    TEXT NOT NULL,      -- 'staging' | 'production'
  granted_by TEXT, granted_at, updated_at
)
```

Rules (normative):

- The table's only writer is the out-of-band DB-credentialed CLI
  (`scripts/operator_principal_admin.py`). No HTTP route creates, edits, or
  deletes principals in v1 — not the operator surface, not ops, not any agent.
- Every operator request re-resolves the principal. `status != 'active'`
  denies. A `role_revision` newer than the one bound into an in-flight
  approval or execution authority denies that artifact's redemption.
- No claim value, tier (including `admin`), role (including
  `platform_admin`), ops secret, dispatch header, browser flag, or model
  output can mint, imply, or extend operator authority.

## 2. Operator sessions

- App surface: `/api/operator/sessions` (+ `/messages`, `/stream`,
  `/transcript`, `/approvals`, `/audit` under the same namespace). Harness
  surface: `POST /operator/turn` and `/operator/sessions/*`, gated by the
  existing harness shared-secret check.
- Session key: `UNIQUE(subject, profile, environment)`. Personal sessions
  only in v1; no shared-session form exists. The key never contains or
  reuses `(tenant_id, drawing_id)`.
- Storage: PostgreSQL only (`operator_sessions`, `operator_turns`,
  `operator_events` with per-session monotonic `seq`). There is no SQLite or
  file fallback. PostgreSQL, schema validation, the authority store, or the
  security-audit store being unavailable makes every operator WRITE surface
  answer 503; reads may degrade read-only.
- Turn snapshot: each turn row records `(subject, role_revision, profiles,
  environment)` at turn start. Mid-turn authority checks validate BOTH the
  snapshot and the live principal row; either mismatch denies. A profile
  change never resumes another profile's SDK session.
- No existence oracle: a probe of another principal's session — including by
  a same-tenant non-operator — answers 404 `operator_session_not_found`,
  byte-identical to an unknown id.
- Mixed-version rule: the namespace is additive. An app or harness revision
  without the operator control plane answers 404 to the entire namespace — a
  DENIAL. No operator field rides any tenant request shape, so no old
  component can misroute an operator request as a tenant request.

## 3. Operator wire

Distinct event vocabulary, exact strings, pinned by test — separate
constants from the tenant `ConverseEventType`/`ConverseStopReason`, which do
not grow:

- events: `operator_turn_started`, `operator_text_delta`,
  `operator_tool_call`, `operator_tool_result`, `operator_proposed_action`,
  `operator_authority_minted`, `operator_authority_redeemed`,
  `operator_turn_usage`, `operator_turn_complete`, `operator_session_state`,
  `operator_error`
- stop reasons: `end_turn`, `awaiting_approval`, `cap_hit`, `error`,
  `timeout`

## 4. Operator catalog

`server/operator_policy.json` (env `LEAF_OPERATOR_POLICY_FILE`), parsed by
`server/operator_policy.py`:

- Action names are namespaced `operator.*` and never appear in
  `agent_policy.json`.
- Rungs reuse the integer 0–7 scale; policies are exactly
  `auto` | `confirm-once` | `always-confirm` (live tenant spellings).
- Unknown fields are load errors. Security booleans refuse coercion.
  Overlays only tighten. Missing/unreadable file = fail-safe deny.
- `policy_revision` = SHA-256 over the canonical-JSON policy bytes; it binds
  into every approval and authority; drift denies redemption.
- Startup seal: every mounted operator tool maps to exactly one action and
  one handler in a static registry. An unknown tool, missing schema
  validator, or unmapped handler prevents startup.
- No action accepts a free-form shell string, raw SQL, or arbitrary URL for
  any live surface. The only free-form execution is the disposable worker's
  job envelope, and it executes solely inside the disposable workspace.
- No action, route list, or handler names a production deployment route.
  Release preparation stops at the staging runbook (`operator.
  stage_release_candidate`); production promotion is NOT MOUNTED and stays
  with the canonical production deployment transaction and a separate owner.

### 4.1 The v1 action matrix (normative, self-contained)

The complete action set and its security-critical fields are declared here
and in the machine-readable normative copy `contract/operator_action_matrix.v1.json`
(the two must agree; the freeze gate pins the JSON). No entry is reachable to
production, and production promotion is not an action at all — it is absent
from the matrix (`operator.promote_production` is listed under `not_mounted`),
enforcing §7.

| Action | Class | Rung | Policy | Rate | Spend | Timeout(s) | Precondition | Handler | Reversal | Prod-reachable | v1 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `operator.read_fleet_state` | O1 | 1 | auto | low | none | 10 | none | `read_fleet_state` | n/a (read-only) | no | on |
| `operator.read_tenant_state` | O1 | 1 | auto | low | none | 10 | none | `read_tenant_state` | n/a (read-only) | no | on |
| `operator.read_jobs` | O1 | 1 | auto | low | none | 10 | none | `read_jobs` | n/a (read-only) | no | on |
| `operator.read_sessions` | O1 | 1 | auto | low | none | 10 | none | `read_sessions` | n/a (read-only) | no | on |
| `operator.read_audit` | O1 | 1 | auto | low | none | 10 | none | `read_audit` | n/a (read-only) | no | on |
| `operator.read_worker_status` | O1 | 1 | auto | low | none | 10 | none | `read_worker_status` | n/a (read-only) | no | on |
| `operator.worker_submit_job` | O2 | 2 | auto | medium | cost_tokens | 1800 | no prod credential in job env (structural); disposable workspace | `worker_submit_job` | cancel job; workspace is disposable and destroyed on completion | no | on |
| `operator.worker_cancel_job` | O2 | 2 | auto | medium | none | 30 | job exists and is owned by the principal | `worker_cancel_job` | n/a (idempotent) | no | on |
| `operator.repo_propose_change` | O3 | 3 | auto | medium | none | 1800 | branch namespace operator/<subject>/<uuid>; base SHA named | `repo_propose_change` | delete the operator/<subject>/<uuid> branch; never touches main | no | on |
| `operator.tenant_agent_pause` | O4 | 4 | always-confirm | high | none | 30 | tenant exists; current enabled-state revision named | `tenant_agent_pause` | operator.tenant_agent_resume | no | off |
| `operator.tenant_agent_resume` | O4 | 4 | always-confirm | high | none | 30 | tenant exists; current disabled-state revision named | `tenant_agent_resume` | operator.tenant_agent_pause | no | off |
| `operator.tenant_overlay_set` | O4 | 4 | always-confirm | high | none | 30 | overlay revision guard (compare-and-set) | `tenant_overlay_set` | restore the prior overlay recorded on the authority receipt | no | off |
| `operator.worker_credential_rotate` | O4 | 4 | always-confirm | high | none | 60 | credential is non-production-scoped (broker-verified) | `worker_credential_rotate` | re-issue the previous scope (not the previous secret) | no | off |
| `operator.external_write` | O5 | 5 | always-confirm | high | usd | 600 | allowlisted destination; scoped short-lived token handle | `external_write` | per-adapter documented reversal, or the adapter does not ship | no | off |
| `operator.stage_release_candidate` | O6 | 6 | always-confirm | high | usd | 3600 | exact source SHA named; candidate immutable; staging only | `stage_release_candidate` | staging auto-rollback to the previous ECS task-def revision | no | off |
| (O7 production promotion) | O7 | — | — | — | — | — | — | — | — | — | **not mounted** |

"v1 on" = enabled in the first release (the read-only O1 surface plus the
disposable-worker O2/O3 lanes); "v1 off" = declared in the contract but ships
dark, enabled per its own Wave gate. Every mounted action maps to exactly one
sealed handler named above (the §4 startup seal). This table and
`operator_action_matrix.v1.json` carry the identical values for every column;
`test_operator_vocab_freeze.py` pins the JSON by canonical SHA-256, asserts
each per-action field (class, rung, policy, rate, spend, timeout, precondition,
handler, reversal, production-reachability), AND parses this table to confirm
its rung/policy/handler/reversal/prod-reachable cells match the JSON — so
mutating a field in either the JSON or this Markdown table, or adding a
production-reachable action, fails the gate.

## 5. Execution authority

One-use execution authority record (PostgreSQL), minted by the server gate
on an authenticated operator approval and redeemed atomically in the
handler transaction:

```
{authority_id, subject, role_revision, profile, session_id, turn_id,
 action, args_hash,            -- SHA-256 over canonical-JSON args
 target_revision_or_digest, policy_revision, environment,
 minted_at, expires_at,        -- TTL 300 s
 nonce, max_uses = 1, used_count, idempotency_key, status}
```

- Mint: admission (kill switches → catalog → args schema → principal
  revalidation → rate reservation → spend reservation) + authority insert +
  security-audit row commit in ONE transaction.
- Redeem: conditional single-row consume inside the handler transaction;
  concurrent redemptions admit exactly one.
- Deny at redemption on any of: expiry, replay, kill switch active,
  `policy_revision` drift, target drift, subject mismatch, `role_revision`
  mismatch, environment mismatch. Each denial writes a distinct audit
  reason.
- Kill switches: file-presence `LEAF_OPERATOR_KILL_FILE` (no API off-toggle,
  the tenant idiom) and per-principal `status` suspend. Both are admission
  step 1 and redemption re-checks.

## 6. Disposable workers

- Broad O2/O3 capability executes only inside a disposable workspace with
  no production credentials: worker env is scrubbed to an explicit
  allowlist; no AWS, ops-secret, dispatch-secret, broker, or deploy-scoped
  variable exists inside a job.
- Network: default-deny with a per-job allowlist; cloud metadata endpoints,
  internal CIDRs, and production hosts are always denied.
- Repo mutations land only on `operator/<subject>/<uuid>` branches through
  one refspec chokepoint; every change carries branch, commit, diff, and
  test receipts in the artifact manifest.
- Duplicate submission under one idempotency key yields one logical job.
  Timeout terminates the full process tree. Cleanup removes the workspace,
  preserving approved artifacts and logs.

## 7. Production unreachability

Provable at every landing:

1. No production deployment credential exists in the model process or any
   disposable worker environment.
2. No operator manifest, allowlist, or generic handler names a production
   deploy tool or route.
3. Staging produces an immutable, receipted release candidate; production
   promotion requires the existing canonical deployment transaction and a
   separate deployment owner, outside every operator surface.

## 8. Freeze gates

| Gate | Pins |
|---|---|
| `server/tests/test_operator_vocab_freeze.py` | tier/capability/role vocabularies gained no operator entry; tenant agent catalog exact; `/api/operator/*` absent-or-denying (mixed-version rule) |
| `harness/test/spineConstants.freeze.test.ts` | `SPINE_TOOL_NAMES` equals its ten literals; tenant event/stop vocabularies exact (compile-time, `npm run typecheck`) |
| (Wave 1, with Lane A/B/C) | principal-writer uniqueness, authority deny matrix, operator wire literals, operator catalog seal |

Existing gates that must stay green and unmodified:
`server/tests/test_contract_freeze.py`, `test_auth_vocab_freeze.py`,
`harness/test/converseSdkRunner.test.ts`.
