# 1G deploy-config hand-off (Codex lane) — harden-staging

The Claude fleet closed 12 of 13 open security findings in code on branch
`claude/harden-staging-20260719` (commits `ab940f8`, `657f5c3`, `de1c8c0`). The
remaining two findings (F3, F11) and the ENFORCEMENT wiring (the env vars +
secrets that make the new caller-auth/sandbox actually bite) are deploy-config —
Codex's lane. This is the turnkey spec.

NOTE: all security code is fail-open-safe when these env vars are unset (demo stays
byte-identical — that's why the gate is green without them). Setting them turns
hardening ON. Nothing here changes app source.

## F11 — non-root containers
deploy/Dockerfile.broker (before CMD):
    RUN useradd --system --uid 10001 --home /home/leaf --create-home leaf
    USER 10001
deploy/Dockerfile.harness (before CMD):
    RUN useradd --system --uid 10002 --home /home/leaf --create-home leaf
    USER 10002
Ensure writable dirs (jobs.db dir, LEAF_TENANTS_DIR, LEAF_GRANTS_DIR, LEAF_STORE_DIR)
are owned/writable by that uid. Drop caps + RO rootfs where mounts allow.

## F3 — auth ON (app service env, compose + ECS)
    LEAF_AUTH_LIVE=1
    LEAF_AUTH0_ISSUER=https://leafautomation.us.auth0.com/
    LEAF_AUTH0_AUDIENCE=https://api.leafdesign.ai
    LEAF_AUTH0_JWKS_URL=https://leafautomation.us.auth0.com/.well-known/jwks.json
(Proven live — docs/auth0-live-server-receipt.json.) Keep :8130 behind ALB/TLS;
broker :8140 + harness :8150 stay internal-only.

## Production security environment
| Env | Purpose (finding) | Set on | Secret? |
|---|---|---|---|
| LEAF_BROKER_SECRET | app<->broker caller-auth (F4); same value both; unset+live -> broker 503 | app + broker | YES -> Secrets Manager |
| LEAF_HARNESS_SECRET | app<->harness caller-auth (F5); same value both | app + harness | YES -> Secrets Manager |
| LEAF_HARNESS_AUTH | enable harness gate; set =1 | harness | no |
| LEAF_OPS_SECRET | ops gate (F7); /api/ops/* needs X-Ops-Secret; unset+live -> 503 | app (+ ops client) | YES -> Secrets Manager |
| LEAF_CORS_ORIGINS | CORS allowlist (F17); prod web origin(s), e.g. https://leaf-platform-web.vercel.app | app | no |
| LEAF_AUTHOR_SANDBOX_PROVIDER | design-time author boundary; `e2b` selects the structured E2B adapter | harness | no |
| LEAF_TOOL_SANDBOX_PROVIDER | authored-tool boundary; `e2b` selects only the E2B microVM helper | broker | no |
| LEAF_DAILY_RUN_QUOTA | free-tier daily run cap (F12/A4); default 20; swap {10,20,50} | broker | no |
| LEAF_QA_HOOKS | test hook; leave UNSET/0 in prod (auto-off when LEAF_AUTH_LIVE=1) | (none in prod) | no |
| E2B_API_KEY | E2B microVM launcher authentication | broker + harness | YES -> Secrets Manager |
| LEAF_E2B_HELPER | path to the Node tool-exec helper; baked into the broker image as /app/harness/scripts/e2b-tool-exec.mjs — override only for local dev | broker | no |

Optional: LEAF_BROKER_TENANT_TIERS (JSON {tenant_id: tier}) on broker so F10 enforces
per-tenant tiers in hosted mode (else defaults demo/open); or provision broker_tenants.json.
The two selectors are intentionally independent. Production authored execution
requires both to be `e2b`. `LEAF_AUTHORED_EXECUTION` remains off by default.
When `LEAF_AUTHOR_SANDBOX_PROVIDER` is absent, local environments preserve the
documented `LEAF_SANDBOX=e2b` author fallback. An explicit new selector always
wins. The legacy value never satisfies the production startup gate.

Source pins sandbox policy `leaf.sandbox-policy.v1` and E2B template
`leaf-python-2026-07-29-v2` (template ID `r0kto3ypd1sgylx4tkz4`, build ID
`273367ae-6a5b-47da-ba46-7782c2fa5d6b`). Production activation remains blocked until the
operator creates and approves that template, its region and retention, the
E2B account and budget, and the explicit author broker gateway.

The template definition is tracked at repository root in `e2b.Dockerfile`.
Build the pinned alias from an authenticated E2B operator session:

```bash
npx -y @e2b/cli@2.16.0 template create leaf-python-2026-07-29-v2 \
  --dockerfile e2b.Dockerfile
```

After the build succeeds, run `harness/scripts/e2b-tool-exec.mjs` with the
same egress-lock policy used by the broker. Do not enable the broker selector
until the receipt reports `passed: true`.

E2B's platform-local controller can answer the trusted helper with HTTP 401.
It also answers the AWS token-acquisition shape with HTTP 200, so the receipt
does not treat controller status codes as isolation. The trusted wrapper starts
the tenant interpreter with `unshare -Urn`, which gives it a new user and
network namespace with no interfaces or routes. Before execution, the helper
uses the same command to prove transport-level blocking for every denied target
and the link-local controller. The receipt also requires no controller
credential in the sandbox environment. The separate red-team boot proves that
untrusted tenant code cannot connect to the controller and cannot restore the
fixed resource hard limits.

Result hashes use one canonical byte contract in Python and JavaScript:
sort object keys by their UTF-8 bytes, preserve array order, emit compact JSON
with raw Unicode encoded as UTF-8, and render every finite IEEE-754 number in
scientific notation with 17 significant digits and a normalized exponent.
Non-finite numbers and non-JSON values are rejected. The receipt stores SHA-256
of those bytes. It also binds the complete execution job, intake, template ID,
template build ID, tenant, source, and policy. The broker revalidates every hash
for both micro-VM selectors. Fixed Unicode, reordered-key, exponent, replay, and
tamper vectors gate both implementations.

The runner lowers both the soft and hard CPU, address-space, process, file-size,
and file-count limits without raising a stricter platform limit. Tenant code
cannot restore any cap. The live red-team receipt also proves memory exhaustion
at 600 MiB and file-descriptor exhaustion before 256 opens.

Tenant stdout and stderr terminate in files owned by a trusted wrapper, not in
the broker or E2B SDK capture stream. The wrapper applies a 1 MiB `RLIMIT_FSIZE`
on Linux, checks the files on every platform, and relays at most 1 MiB. A limit
breach or nonzero tenant process exit relays only a fixed small error.

## Migration 0002
platform/db.py apply_migration() (no arg) now applies EVERY NNNN_*.sql in order
(idempotent), so the deploy migration step auto-picks-up 0002_deletion_columns.sql.
If the deploy runs a specific file, switch to the no-arg apply_migration().

## Verify after applying (enforcement really ON)
- curl https://<app>/api/jobs (no bearer) -> 401 (F1)
- curl -H 'X-Org-Id: <victim>' .../api/projects -> 404 (F6)
- curl .../api/ops/tenants (no X-Ops-Secret) -> 403 (F7)
- curl <broker>:8140/broker/run (no X-Broker-Secret, in-VPC) -> 401 (F4)
- author with LEAF_SANDBOX=e2b; red-team tool can't read the APS cred (F2) — docs/e2b-tool-exec-receipt.json
- re-run the docs/aws-staging-deploy-receipt.json verification block; auth matrix 401/200/200/403 still passes.
