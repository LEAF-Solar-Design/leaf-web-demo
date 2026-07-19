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

## The 8 new env vars
| Env | Purpose (finding) | Set on | Secret? |
|---|---|---|---|
| LEAF_BROKER_SECRET | app<->broker caller-auth (F4); same value both; unset+live -> broker 503 | app + broker | YES -> Secrets Manager |
| LEAF_HARNESS_SECRET | app<->harness caller-auth (F5); same value both | app + harness | YES -> Secrets Manager |
| LEAF_HARNESS_AUTH | enable harness gate; set =1 | harness | no |
| LEAF_OPS_SECRET | ops gate (F7); /api/ops/* needs X-Ops-Secret; unset+live -> 503 | app (+ ops client) | YES -> Secrets Manager |
| LEAF_CORS_ORIGINS | CORS allowlist (F17); prod web origin(s), e.g. https://leaf-platform-web.vercel.app | app | no |
| LEAF_SANDBOX | tenant-code sandbox (F2); set =e2b (broker tool-exec + harness author) | broker + harness | no |
| LEAF_DAILY_RUN_QUOTA | free-tier daily run cap (F12/A4); default 20; swap {10,20,50} | broker | no |
| LEAF_QA_HOOKS | test hook; leave UNSET/0 in prod (auto-off when LEAF_AUTH_LIVE=1) | (none in prod) | no |

Optional: LEAF_BROKER_TENANT_TIERS (JSON {tenant_id: tier}) on broker so F10 enforces
per-tenant tiers in hosted mode (else defaults demo/open); or provision broker_tenants.json.
E2B_API_KEY only if flipping from the shipped subprocess sandbox to the deferred full-E2B micro-VM.

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
