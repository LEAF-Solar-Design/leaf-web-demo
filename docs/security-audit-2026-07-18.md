# Leaf web-CAD platform — adversarial security audit (red-hat)

**Date:** 2026-07-18
**Auditor role:** read-only adversarial reviewer (no code modified, no servers run)
**Target:** `C:/tmp/leaf-web-demo` — multi-tenant + credential + entitlement surface
**Method:** static trace of every scoped file; each claim cites `file:line`. CONFIRMED = I traced the code path end-to-end; SUSPECTED = the path is present but needs a live repro to be certain.

> **Framing.** The demo is *by design* single-operator and auth-off (`LEAF_AUTH_LIVE=0`), and much of what follows is *documented* as such (contract/AUTH.md, CONTRACT-ADDENDUM §8). The audit question is **not** "is the demo safe" — it is **"what breaks the moment this stack is public and multi-tenant on AWS."** Several holes are correctly labeled ACCEPTABLE-FOR-LOCAL-DEMO but MUST-FIX-BEFORE-DEPLOY. **Two are neither** — they survive turning auth on and must be fixed regardless.

---

## The single scariest confirmed finding, stated plainly

**`GET /api/jobs` has no authentication dependency at all, and takes its tenant scope from an unbound query parameter.** Any unauthenticated caller can read *any* tenant's job records — or, with no parameter, the 20 most recent jobs across *all* tenants — including each job's input `params` and full result envelope (drawing intake data + computed results). **Turning on `LEAF_AUTH_LIVE=1` does not close this**, because the endpoint never calls `require_tenant`; it reads `tenant_id` straight from the URL. It is a cross-tenant data leak that is live in the demo and in the "hardened" auth-on deployment alike.

```
GET /api/jobs                       -> 20 most recent jobs of ALL tenants
GET /api/jobs?tenant_id=<victim>    -> that tenant's jobs (params + results + job_ids)
```
`server/routers/jobs.py:140-142` → `server/jobs.py:153-162` (`tenant_id` falsy ⇒ `SELECT * FROM jobs ... LIMIT` with no tenant predicate).

---

## Count by severity

| Severity | Confirmed | Suspected | Total |
|----------|-----------|-----------|-------|
| CRITICAL | 2 | 0 | 2 |
| HIGH     | 5 | 0 | 5 |
| MEDIUM   | 6 | 1 | 7 |
| LOW      | 3 | 1 | 4 |
| **Total**| **16** | **2** | **18** |

---

## MUST-FIX-BEFORE-DEPLOY shortlist (ranked)

1. **F1 — `GET /api/jobs*` auth/scoping bypass** (CRITICAL). Add `Depends(require_tenant)`, derive scope from the identity, drop the client `tenant_id` param. *Not* an auth-off-only issue.
2. **F2 — Tenant tool code runs in-process in the credential broker** (CRITICAL). Nothing sandboxes authored Python; it can read `~/.aps/credentials.json` and other tenants' `.token`s and exfiltrate around the (requests-only) egress allowlist. The E2B/container substrate is the fix; until then this is the crown-jewel exposure.
3. **F3 — Deploy ships auth-OFF** (HIGH). `docker-compose.yml` never sets `LEAF_AUTH_LIVE=1`; the app binds `0.0.0.0:8130`. Set auth live + set the JWT env before any public deploy.
4. **F4 — Broker (`:8140`) has no auth, trusts caller `tenant_id` + full tool dict** (HIGH; CRITICAL if ever reachable). Keep it network-isolated; add a shared-secret/mTLS between app↔broker; stop resolving absolute `entry`/`script` paths.
5. **F5 — Harness (`:8150`) has no auth, trusts body `tenant_id`, exposes `/grants/{tid}`** (HIGH). Same isolation + caller-auth requirement; the grant admin routes let anyone overwrite/delete a tenant's Claude token.
6. **F6 — Platform API trusts `X-Org-Id`** (HIGH). `get_org_id` must be replaced by verified identity before the platform router is public; `POST /api/orgs` must be gated.
7. **F7 — `X-Internal-Role: qa` ops gate is a plain header** (HIGH). Anyone can list all tenants' spend and flip any tenant's kill-switch. Replace with a real internal credential.

---

## Findings — CONFIRMED (ranked by severity)

### F1 (CRITICAL) — `GET /api/jobs` / `/api/jobs/{id}` / `/stream`: unauthenticated cross-tenant job read
- **file:line:** `server/routers/jobs.py:140-142` (list), `:98-104` (get), `:107-137` (stream); `server/jobs.py:153-162`, `:148-150`.
- **Claim broken:** Tenant isolation #1 — "can tenant A see tenant B's jobs, results, usage?"
- **Exploit:** `curl http://host:8130/api/jobs` → all tenants' recent jobs. `curl 'http://host:8130/api/jobs?tenant_id=acme'` → acme's jobs. Each record includes `params`, `dwg`, `tenant_id`, and the full §3 `result` envelope (drawing geometry, computed panel counts, etc.). `GET /api/jobs/{job_id}` and `/stream` likewise have no `require_tenant` and no ownership check — contrast `close_job` at `:145-159`, which *does* check `str(tenant_id) != rec["tenant_id"]` and 404s. The hardening was applied to close but not to get/list/stream.
- **Blast radius:** Full cross-tenant read of all run inputs and outputs. Works unauthenticated and **remains open with `LEAF_AUTH_LIVE=1`** (no `require_tenant` on the route).
- **One-line fix:** Add `tenant = Depends(deps.require_tenant)` to all three routes; scope by `str(tenant)`; return 404 for non-owned `job_id`.

### F2 (CRITICAL) — Tenant-authored tool code executes in-process inside the credential-holding broker
- **file:line:** `server/broker.py:457` (`run_tool_dynamic(...)` on the mock path, in-broker) and `:382-397` (write path); `server/tool_loader.py:52-66` (`_load_module` → `exec_module`), `:200,210` (`mod.run(...)`); `server/broker.py:100-114` (egress guard patches only `requests.adapters.HTTPAdapter.send`); `da/client.py:53` (`CRED_PATH = ~/.aps/credentials.json`); `harness/.../oauthGrantProvider.ts:215` + `docker-compose.yml:57` (`.token` files on `leaf-grants`).
- **Claim broken:** Broker boundary #5 and credential safety #2 — "can tenant-authored tool code exfiltrate creds / escape the tenant dir / reach arbitrary egress?"
- **Exploit:** Author a tool whose `run(intake, params)` body does `open(os.path.expanduser('~/.aps/credentials.json')).read()` (or reads `/data/grants/<other>.token`, or the broker ledger) and ships the bytes out via `socket`, `urllib`, `http.client`, `subprocess`, or DNS — none of which the `requests`-only monkeypatch intercepts. The tool executes in the **broker** process, which is the *only* process meant to hold the APS credential. There is no `seccomp`, no filesystem jail, no import allowlist, no separate UID.
- **Blast radius:** Full compromise of the APS crown-jewel credential and every tenant's stored Claude token; arbitrary egress from the broker; arbitrary file read on the broker host. This is the exact gap E2B/container isolation is meant to close.
- **Status of the gap:** *Documented* as a v1 assumption — CONTRACT-ADDENDUM §8: "the demo is single-process, so 'tenant container' == in-process… Network-layer enforcement (container/proxy) is another session." The audit adds two teeth: (a) the egress allowlist gives a **false** sense of containment — it guards one library, not the process; (b) the containers run as **root** (F11), so the escape is root-in-container.
- **One-line fix:** Execute authored tool bodies in a real sandbox (E2B / gVisor / a locked-down subprocess with seccomp + read-only FS + no network), never in the broker process.

### F3 (HIGH) — The deployable stack ships auth-OFF and internet-bound
- **file:line:** `docker-compose.yml:76-79,88-91` (app `ports: 8130:8130`, no `LEAF_AUTH_LIVE`), `:114-115` (web `8080:8080`); `server/deps.py:260-262` (`auth_live()` false ⇒ `require_tenant` returns `x_tenant_id or DEFAULT_TENANT`); `server/app.py:109` (`host="0.0.0.0"`).
- **Claim broken:** Tenant isolation #1 (X-Tenant-Id spoof) and Deploy exposure #6.
- **Exploit:** Against the composed stack as written, `curl -H 'X-Tenant-Id: victim' http://host:8130/api/tools|/api/session|/api/usage|/api/drawings/<id>/intake|/api/run` impersonates any tenant — reading their catalog, drawings, versions, usage, and running/authoring on their behalf. `8130:8130` binds all host interfaces; on AWS only the security group stands between this and the internet.
- **Blast radius:** With auth off, *every* tenant-scoped endpoint is a free-for-all keyed on a spoofable header. This is the documented demo default, but the compose file is the deploy artifact and does not flip it.
- **One-line fix:** Set `LEAF_AUTH_LIVE=1` + the `LEAF_AUTH0_*` env in the app service (and the harness's tenant trust), and front the app with TLS + the SG; never expose `8130` raw.

### F4 (HIGH — CRITICAL if reachable) — Broker `:8140` has no caller auth and trusts the whole request
- **file:line:** `server/broker.py:274-283` (`/broker/tenants/{tid}/disable|enable`, no auth), `:315-343` (`/broker/run`, no auth), `:255-260` (`tenant_id` + full `tool` dict are caller-supplied); `server/tool_loader.py:93-116` (`resolve_local_file` honors `tool['entry']`/`tool['script']` incl. `Path(entry)` absolute paths).
- **Claim broken:** Broker boundary #5, kill-switch #4, entitlement bypass #3.
- **Exploit (requires reaching :8140):**
  - `POST /broker/tenants/<victim>/disable` → kill-switch any tenant (or `enable` to lift your own).
  - `POST /broker/run {tenant_id:"<victim>", tool:{...}}` → attribute your spend to a victim, or bypass *your* cap by changing `tenant_id` (the cap and ledger key on the caller-supplied id, `:223-245`).
  - `POST /broker/run {tool:{name:"x", entry:"C:/any/file.py"}}` → `resolve_local_file` returns `Path(entry)` if it is an existing `.py`, and `_load_module` imports/executes it → arbitrary local `.py` execution in the broker. (The **app** path is safe here: `/api/run` takes a tool *name* and resolves the dict server-side from the tenant catalog — `routers/jobs.py:59`. Only *direct* broker access injects a full tool dict.)
- **Blast radius:** Attribution fraud, cap evasion, cross-tenant denial-of-service, and an arbitrary-code primitive — all unauthenticated. **Mitigant:** in `docker-compose.yml` the broker publishes **no** host port, so it is compose-network-internal (good). The risk is realized by an SSRF in the app, a co-located/compromised container, or an ECS task/SG that maps the port.
- **One-line fix:** Require a shared secret / mTLS on every `/broker/*` call, and reject non-relative / non-repo `entry`/`script` paths.

### F5 (HIGH) — Harness `:8150` has no caller auth; `/grants/{tid}` lets anyone rewrite a tenant's Claude token
- **file:line:** `harness/src/server.ts:30-42` (`tenantForRequest`: body `tenant_id` wins, else header), `:88-118` (`PUT/GET/DELETE /grants/{tid}` gated only by store presence), `:120-149` (`/author`, `/run-registered` — no auth).
- **Claim broken:** Entitlement bypass #3 (direct harness), credential safety #2.
- **Exploit (requires reaching :8150):** `POST /author {description, tenant_id:"<victim>"}` authors/commits into the victim's repo on the victim's Claude grant/credit. `PUT /grants/<victim> {token:"attacker-token"}` overwrites a victim's linked grant (their authoring then bills the attacker's account — or `DELETE` to deny-of-service their Build lane). `GET` never returns the token (good — `oauthGrantProvider.ts:220-234` returns only `{linked, linked_at, kind}`).
- **Blast radius:** Cross-tenant authoring on someone else's credit, and tamper/DoS of any tenant's grant. **Mitigant:** harness publishes no host port in compose (internal-only). Same realized-risk conditions as F4.
- **One-line fix:** Authenticate app→harness (shared secret/mTLS) and never accept a tenant identity from the request body/header on an internet-reachable harness.

### F6 (HIGH) — Platform Project/Job API trusts `X-Org-Id`; mounted on the public app port
- **file:line:** `platform/deps.py:15-25` (`get_org_id` reads `X-Org-Id`, no verification); `server/app.py:70-90` (`_mount_platform_router()` mounts `/api/orgs`, `/api/projects` onto the same `:8130` app); `platform/api.py:59-65` (`POST /api/orgs` no auth), `:68-84,90-139` (all org-scoped reads keyed on the header-derived org).
- **Claim broken:** Tenant isolation #1 — "the platform X-Org-Id trust boundary."
- **Exploit:** With `DATABASE_URL` set, `curl -H 'X-Org-Id: <victim-uuid>' http://host:8130/api/projects` (and `/api/projects/{id}`, `/api/jobs/{id}`) returns the victim's projects, drawing versions, jobs (params + results), and built tools. `POST /api/orgs` mints orgs unauthenticated (spam / bootstrap abuse).
- **Blast radius:** Full cross-org read of the canonical persistence layer, gated only by knowing a UUID. **Mitigants:** the store layer itself is sound — every read is parameterized and `WHERE org_id`-scoped, statically enforced (`platform/store.py`, `tests/test_store_guard.py`), and reads are 404-not-403 to avoid existence leaks. So the *only* defense is `get_org_id`, which trusts the client. Documented as a dev seam ("production MUST gate," `platform/README.md`, CONTRACT-ADDENDUM §-orgs).
- **One-line fix:** Replace `get_org_id`'s body with an org derived from the verified session/JWT; gate `POST /api/orgs` behind provisioning.

### F7 (HIGH) — Ops surface is "protected" by a plain `X-Internal-Role: qa` header
- **file:line:** `server/routers/ops.py:47-57` (`_require_qa` accepts the literal header), `:142-161` (list ALL tenants' spend/runs/disabled), `:164-177` (disable/enable proxy to broker).
- **Claim broken:** Kill-switch / ops #4.
- **Exploit:** `curl -H 'X-Internal-Role: qa' http://host:8130/api/ops/tenants` → every tenant's id, run count, USD spend, and kill-switch state (cross-tenant business-intelligence leak). `-X POST .../api/ops/tenants/<victim>/disable` → disable any tenant. The header is trivially set by anyone; it is a stub (documented) but is the whole gate. (The proxied broker call is itself unauthenticated — F4 — so this also works by hitting the broker directly if reachable.)
- **Blast radius:** Cross-tenant spend disclosure + arbitrary kill-switch control. Same GET also leaks the full tenant roster.
- **One-line fix:** Gate the ops router on a real internal credential (signed role claim / network ACL / separate admin service), not a client-set header.

---

## Findings — CONFIRMED (MEDIUM)

### F8 (MED) — `/api/jobs/{job_id}` + `/stream` IDOR (no ownership check)
- **file:line:** `server/routers/jobs.py:98-104,107-137`; job ids are `uuid4` (`server/jobs.py:133`).
- **Claim broken:** Tenant isolation #1. Subset of F1; called out separately because even after F1's list route is fixed, these two must also enforce ownership. Blast radius is limited by uuid unguessability — but F1's list route hands out the uuids, so treat them together.
- **One-line fix:** `require_tenant` + 404 when `rec["tenant_id"] != str(tenant)`.

### F9 (MED) — Fail-OPEN entitlement default: missing `tier` claim ⇒ full access
- **file:line:** `server/entitlements.py:70-74` (`resolve_tier`: `return str(tier) if tier else DEFAULT_TIER`, `DEFAULT_TIER="demo"`), `:40-45` (`demo` = `{run_read, run_write, build}` all True), `:77-86` (unknown tier → demo; per-key omission → `True`).
- **Claim broken:** Entitlement bypass #3.
- **Exploit:** A *verified* Auth0 tenant whose JWT lacks the namespaced `tier` claim (Auth0 action misconfig, a token minted before the action, a partial claim) resolves to `tier="demo"` and is granted `run_write` **and** `build` — the paid capabilities. The enforcement layer fails open, and every per-key default is permissive, so a partial policy entry silently un-restricts.
- **Blast radius:** Restricted-tier tenants get write + author for free; a business-model bypass with a security face (a `hosted_starter` tenant authoring/running writes they should not).
- **One-line fix:** In live-auth mode, an absent/unknown tier must resolve to the **most restrictive** tier (or reject), never "demo"; default per-key to `False`.

### F10 (MED) — Entitlement enforced only in the app router; broker/harness bypass it
- **file:line:** enforced at `server/routers/jobs.py:68-71` and `server/routers/author.py:71-73`; **absent** in `server/broker.py` (`/broker/run`) and `harness/src/server.ts` (`/run-registered`, `/author`).
- **Claim broken:** Entitlement bypass #3 — "a missing check on any code path that reaches execution."
- **Exploit:** Any path that reaches the broker or harness directly (F4/F5) skips the tier gate entirely. Also, `POST /api/run?wait=1` vs async are *both* gated identically (good — no wait/async split hole), so the bypass is specifically the direct-service path.
- **One-line fix:** Re-check the tier capability at the broker (it already receives `tenant_id`), so enforcement is defense-in-depth, not perimeter-only.

### F11 (MED) — Broker + harness containers run as root; broker runs untrusted tenant code
- **file:line:** `deploy/Dockerfile.broker` (no `USER`; `python:3.12-slim` default root), `deploy/Dockerfile.harness` (no `USER`; `node:22-slim` default root).
- **Claim broken:** Deploy exposure #6 (hardening).
- **Exploit:** Amplifies F2 — the in-process tenant-code escape is root-in-container, widening the blast radius to the whole container filesystem and any host mount.
- **One-line fix:** Add a non-root `USER` to both images; drop Linux capabilities; read-only rootfs where possible.

### F12 (MED) — Noisy-neighbor DoS: shared 4-worker job pool, no per-tenant limit, 30s sleep hook
- **file:line:** `server/jobs.py:46` (`MAX_WORKERS=4`), `:144` (every submit goes to the one shared pool), no rate limit anywhere; `server/broker.py:367,447-449` (`_qa_sleep_s` honored up to `QA_SLEEP_CAP_S=30`).
- **Claim broken:** Injection/DoS #7.
- **Exploit:** One tenant submits ~4 runs with `params={"_qa_sleep_s":30}` (threaded through `/api/run`) and starves the shared executor for *all* tenants; unbounded submissions fill `jobs.db` and the ledger.
- **One-line fix:** Per-tenant concurrency/rate quota; strip `_qa_sleep_s` unless an explicit QA flag is set; bound queue depth.

### F13 (MED) — Inconsistent tenant-id normalization ⇒ drawing-store namespace collision
- **file:line:** `da/store.py:52-62` (`sanitize_id` reduces to `[a-z0-9-]`, **collapsing** `_`, `/`, `.`, space, unicode to `-`); `server/tenant_paths.py:31-40` (`_safe_component` allows odd chars incl. NUL — no charset regex); vs. `harness/.../oauthGrantProvider.ts:117-124` (`safeBase` **rejects** with `^[A-Za-z0-9._-]+$`).
- **Claim broken:** Tenant isolation #1 (path handling), consistency.
- **Exploit:** Distinct tenant identities that differ only in non-`[a-z0-9-]` characters — e.g. `acme.co` vs `acme-co`, or `a/b` vs `a-b` — sanitize to the **same** drawing-store prefix `tenants/<same>/...`, so they read and write each other's drawings and versions. Traversal itself is blocked (sanitize + `FilesystemBackend._path` prefix check, `da/store.py:179-184`), so this is collision, not escape. The three subsystems normalize a tenant id three different ways (collapse / permit / reject), so a value legal in one store is illegal or aliased in another.
- **One-line fix:** One shared, reject-don't-collapse tenant-id validator across store, grant store, and repo resolver; require server-issued opaque ids.

---

## Findings — SUSPECTED

### F14 (MED, SUSPECTED) — `dwg` path traversal in the broker live path
- **file:line:** `server/broker.py:406` (`local = str(DATA_DIR / f"{req.dwg}.dwg")`), reached with caller-controlled `req.dwg` via `RunRequest.dwg` → `jobs.submit_job` → `broker_client.run_via_broker(dwg=...)`.
- **Claim broken:** Injection #7 / credential-adjacent read.
- **Exploit (needs `APS_LIVE=1`):** `POST /api/run {tool, dwg:"../../../../some/secret"}` → `da.run_tool` uploads `DATA_DIR/../../../../some/secret.dwg` to APS OSS — an arbitrary `.dwg`-suffixed file read + exfil to Autodesk storage. On the mock/write paths `dwg` is not used as a filesystem path (fixed `DATA_FILE`), so this is live-path-only; I could not run the live path to confirm, hence SUSPECTED.
- **One-line fix:** Validate `dwg` against `^[a-z0-9_-]+$` (or a known allowlist) before joining.

### F15 (LOW, partly SUSPECTED) — Unbounded request inputs (no ReDoS)
- **file:line:** `server/routers/prompt.py:30-44` (`text` no max, no `require_tenant`), `server/routers/author.py:38` (`description` no max), `server/routers/jobs.py:28-31` (`params` unbounded).
- **Claim broken:** Injection/DoS #7.
- **Note:** I specifically attacked the `nl_router` regexes for ReDoS and they **hold** — all patterns are linear (`\btools?\s+(that|to|which|for)\b`, the explicit-build alternation, `[0-9A-Fa-f]{2,}`, `[a-z0-9]+`); no nested quantifiers / overlapping alternation. The residual risk is plain CPU/memory/storage from megabyte-scale bodies (classification, harness forwarding, SQLite/ledger growth).
- **One-line fix:** `max_length` on `text`/`description`, a body-size limit at the app, and a params size cap.

### F16 (LOW) — Non-constant-time admin-token compare (platform offboard)
- **file:line:** `platform/api.py:152-158` (`if x_admin_token != admin_token`).
- **Claim broken:** Deploy exposure #6.
- **Note:** Timing side-channel on `PLATFORM_ADMIN_TOKEN`. Fails *closed* when the env is unset (503, `:154-156`), which is good.
- **One-line fix:** `hmac.compare_digest`.

### F17 (LOW) — CORS `allow_origins=["*"]`
- **file:line:** `server/app.py:42-48` (`allow_origins=["*"]`, `allow_credentials=False`).
- **Claim broken:** Deploy exposure #6.
- **Note:** Benign in isolation (auth is header/bearer, not cookie, and credentials are off), but it lets *any* website drive the API from a victim's browser, amplifying the missing-auth findings (F1/F3/F6/F7 all work from a hostile origin).
- **One-line fix:** Restrict origins to the known web app once auth is live.

### F18 (LOW) — Grant tokens on a named volume; mode-0600 only where honored
- **file:line:** `harness/.../oauthGrantProvider.ts:214-216` (`writeFileSync(..., {mode:0o600})`), `docker-compose.yml:57,131` (`leaf-grants` volume, harness-only mount).
- **Claim broken:** Credential safety #2.
- **Note:** Currently only the harness mounts `leaf-grants` (good), and the token dir is separate from the git repo dir so tokens are never committed (verified — `LEAF_GRANTS_DIR` ≠ `LEAF_TENANTS_DIR`). Residual risk: any future container that mounts the volume, or root on the host/another container, reads every tenant's Claude token in cleartext. On Windows the 0600 mode is ignored.
- **One-line fix:** Move to a vault/DPAPI/KMS-backed secret store (the interface already anticipates this); never share the volume.

---

## What I attacked and could NOT break (holds)

- **Auth0 JWT verification** (`server/auth.py:123-152`): RS256 **pinned** (`algorithms=["RS256"]` — no `none`/HS256 downgrade), audience + issuer + `exp`/`iat` required, correct 401/403 split. The kid-tolerant single-key fallback is scoped to the local-JWKS **test** path only (`:100-107`). Tenant/tier/org claims are read from the *verified* payload and are namespaced (Auth0-minted), so a user can't self-assert their tier — **except** the fail-open default of F9 when the claim is absent.
- **Platform SQL** (`platform/store.py`, `server/platform_link.py:184-198`): every statement is parameterized (`%(...)s` / `Jsonb`); every read binds `WHERE org_id`; `test_store_guard.py` statically enforces both. No injection found.
- **Drawing-store path traversal** (`da/store.py:52-62,179-184`): `sanitize_id` + `normpath` prefix check block escape; `drawing_id` is sanitized before use.
- **Grant-store path traversal** (`oauthGrantProvider.ts:117-124`): strict `^[A-Za-z0-9._-]+$` + `base !== tid` reject; `..`, separators, and NUL are all rejected.
- **SDK credential discipline** (`agentSdkRunner.ts:74-94,199-341`): scrubbed child env (ambient cred keys deleted), the tenant's grant injected *explicitly* into the SDK `env`, `settingSources:[]`, `cwd` scoped, tools deny-by-default via `canUseTool`; the token is never logged/echoed. The app proxy and harness status never return the token (`routers/tenant.py:62-70`, `oauthGrantProvider.ts:220-234`).
- **Secret bake / image hygiene**: `.dockerignore` excludes the ledger, kill-switch file, jobs.db, authored store, `.env*`, `.secrets/`, `.grant/`, logs; the APS credential and Claude grant are runtime mounts, never baked.
- **Compose network posture**: broker and harness publish **no** host ports (internal-only); only `app:8130` and `web:8080` are published. (The gap is that internal ≠ authenticated — F4/F5.)
- **Git flow**: `execFileSync`/worker use argument arrays, not a shell; identity is a constant, not user input — no git/shell injection.
- **ReDoS**: none in `nl_router.py` (all linear).

---

## Acceptable-for-local-demo vs must-fix-before-deploy

| Finding | Local demo (single-op, auth-off) | Public multi-tenant on AWS |
|---|---|---|
| F1 jobs read | tolerable | **MUST FIX** (survives auth-on) |
| F2 in-process tenant code | tolerable (you author your own tools) | **MUST FIX** (E2B/sandbox) |
| F3 auth-off deploy | *is* the demo | **MUST FIX** (flip `LEAF_AUTH_LIVE=1`) |
| F4 broker no-auth | tolerable (loopback) | **MUST FIX** (isolate + auth) |
| F5 harness no-auth | tolerable (loopback) | **MUST FIX** (isolate + auth) |
| F6 X-Org-Id trust | tolerable (dev seam) | **MUST FIX** (verified org) |
| F7 ops header gate | tolerable | **MUST FIX** (real role) |
| F9 fail-open tier | n/a (demo=full by design) | **MUST FIX** (fail closed) |
| F10 broker no entitlement | tolerable | **MUST FIX** (defense-in-depth) |
| F11 root containers | tolerable | **SHOULD FIX** (non-root) |
| F12 shared job pool | tolerable | **SHOULD FIX** (per-tenant quota) |
| F13 id-collision | unlikely (one tenant) | **SHOULD FIX** (unified validator) |
| F14 dwg traversal | tolerable | **SHOULD FIX** (validate `dwg`) |
| F15–F18 | tolerable | **SHOULD FIX** (defense-in-depth) |

**Bottom line:** the perimeter is honestly documented as auth-off, and turning `LEAF_AUTH_LIVE=1` closes the `X-Tenant-Id` spoof family (F3). **But F1, F2, F4, F5, F6, F7, and F9 are all independent of that flip** — the `/api/jobs` routes, broker, harness, platform `X-Org-Id`, and ops `X-Internal-Role` gate never consult `auth_live`, and F9 fails open precisely *when* auth is on. Fix F1 (a ~3-line dependency change), design F2's sandbox, and add real caller-auth to the broker/harness/platform/ops boundaries before this goes multi-tenant.
