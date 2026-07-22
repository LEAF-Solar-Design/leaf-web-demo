# AUTH.md — Leaf platform identity (Auth0) addendum

Addendum to the **frozen** `contract/CONTRACT.md` (§1–§6, unchanged) and the
`server/CONTRACT-ADDENDUM.md` backbone (§7–§10, unchanged). This document owns
**Concern 1 — Leaf platform identity**. It is behind an env toggle; with the
toggle off the demo is byte-identical to today. Nothing here is frozen until an
operator promotes it.

Lane: `auth0-identity-signup`. Session date: 2026-07-17.

---

## 0. Two auth concerns — keep them separate (task-critical)

| | **Concern 1 — Leaf PLATFORM identity** (this doc) | **Concern 2 — the user's Claude login** (NOT this doc) |
|---|---|---|
| Question | *Who is this tenant?* | *Whose Anthropic credit runs the agent?* |
| Mechanism | **Auth0** RS256 access token → namespaced tenant claim | **Agent SDK "sign in with Claude" OAuth** |
| Owner | this lane (`auth0-identity-signup`) | sibling **`hosted-oauth-spike`** |
| Carried in | the JWT tenant claim | the tenant's own per-user Claude OAuth token store |

**INVARIANT (hard):** the Auth0 tenant claim **NEVER carries a Claude/Anthropic
credential**, and this lane touches nothing of Concern 2. A verified tenant
identity says *which workspace* a request belongs to; it does not grant, embed,
or reference any Claude token.

> W1 finding (`research/agentsdk-usage-visibility.md`): Claude subscription
> OAuth tokens are **individual-use per user** — a tenant can never share one
> pooled Claude credential. This reinforces the separation: platform identity
> (one Auth0 tenant, many users) and Claude credit (one OAuth per user) are
> different cardinalities and must not be conflated in a single claim.

---

## 1. The namespaced JWT claim shape

Auth0 custom claims must be **namespaced** (Auth0 silently drops non-namespaced
custom claims on tokens). The Post-Login Action
(`server/auth0-actions/post-login-add-tenant-claim.js`) stamps three claims onto
the **access token**:

```
https://leafdesign.ai/tenant_id   (string, required)  e.g. "org_acme_solar"
https://leafdesign.ai/org_id      (string | null)      e.g. "org_acme_solar"
https://leafdesign.ai/tier        (string enum)        "self_hosted" | "hosted_starter" | "hosted_pro"
```

Namespace prefix is configurable via `LEAF_TENANT_CLAIM_NS` (default
`https://leafdesign.ai/`; a trailing slash is added if omitted).

**Derivation** (in the Action, from `event.user.app_metadata.leaf`, the shape
leaf_website already stores — see `leaf_website/lib/auth0.ts SubscriptionMetadata`):

- `tenant_id` = `leaf.organization_id`, else the Auth0 `sub` (a single-user
  tenant until they create/join an org).
- `org_id` = `leaf.organization_id` (null for a solo user).
- `tier` = `PLAN_TIER[leaf.plan]` mapped onto the cadwalk-studio `DeploymentTier`
  enum (default `hosted_starter`). **Surface-only** — this lane does NOT enforce
  entitlement; downstream (credential broker / metering) consumes the tier.

> **Update (Wave 5, §17):** the `tier` claim now **DRIVES entitlement enforcement**
> server-side — `run_read` / `run_write` / `build` per tier via `server/entitlements.py`
> + `server/entitlements.json`, enforced in `POST /api/run` and `POST /api/author` (403),
> readable at `GET /api/entitlements`. See `server/CONTRACT-ADDENDUM.md` §17. Off-auth
> tenants resolve tier `demo` (full access).

Standard verified claims used by the server: `iss`, `aud`, `exp`, `iat`.

---

## 2. Server verification contract

`server/auth.py` verifies an RS256 access token (near-direct port of the fleet's
proven path, `aws-ai-manager/app/api/deps.py`):

- Signature via JWKS (`LEAF_AUTH0_JWKS_URL`, cached `PyJWKClient`), algorithm
  pinned to `RS256`.
- `audience == LEAF_AUTH0_AUDIENCE`, `issuer == LEAF_AUTH0_ISSUER`, `exp`/`iat`
  required and honored.

**Status-code contract (precise):**

| Condition | HTTP | Meaning |
|---|---|---|
| No / malformed `Authorization` header | **401** | unauthenticated |
| Bad signature / expired / wrong aud / wrong iss | **401** | invalid token |
| Verified token **but no** `…/tenant_id` claim | **403** | authenticated, **not provisioned** for a workspace |
| Verified token **with** tenant claim | **200** | resolves to a workspace |

The 401-vs-403 split is deliberate: *bad token* (retry auth) is distinct from
*good token, no workspace* (needs provisioning).

> **Envelope note (frozen-enum constraint):** the shared error envelope
> (`server/envelopes.py` §10) has a **frozen** `error_code` enum with no AUTH
> code, and this lane does not edit it. Auth rejections therefore surface via the
> app-wide `HTTPException` handler as HTTP **401/403** with the enveloped body
> `{"error":{"error_code":"INTERNAL","message":"<reason>", "retryable":false},
> "degraded_mode":false}`. The **HTTP status** is the machine-readable signal;
> the human-readable reason is in `error.message`. Adding a dedicated
> `UNAUTHENTICATED` / `FORBIDDEN` code to the frozen enum is a follow-up
> OPERATOR action (promote alongside §7–§10).

---

## 3. token → workspace mapping contract

`server/tenancy.py` maps `tenant_id` → `Workspace`:

```
Workspace { tenant_id, org_id, tier, workspace_dir }
```

- Default impl `JsonTenantStore` reads `data/tenants.sample.json`
  (`{"tenants":[{tenant_id, org_id, tier, workspace_dir}, ...]}`).
- An unknown-but-**verified** tenant is **auto-provisioned** a default workspace
  (`LEAF_WORKSPACE_BASE/<tenant_id>`) rather than rejected — a verified identity
  always maps to a workspace in the demo.
- **Production impl** backs onto the cadwalk-studio Postgres tenancy tables
  (`Deployment`/`DeploymentTier`, `src/lib/tenancy/types.ts`) and may choose a
  stricter policy (e.g. deny until a `Deployment` row exists). Swap the store
  without touching the verifier or routers.

`workspace_dir` records where the tenant's hosted agent harness (mushy git repo
+ three.js render + APS ops) will live; **provisioning that dir is out of scope**
here (downstream credential-broker job).

---

## 4. The env toggle + precedence (backward compat)

| `LEAF_AUTH_LIVE` | Behavior |
|---|---|
| unset / `0` (default) | **Byte-identical** to today. No `Authorization` required. `require_tenant` returns the plain `X-Tenant-Id` header stub string (default `demo-tenant`) that the jobs/broker chain relies on. PyJWT is never imported (`auth.py` is imported lazily only on the live path — the default demo does **not** need `pip install -r requirements-auth.txt`). |
| `1` | `require_tenant` **verifies the Bearer token**, extracts the tenant claim, resolves a workspace, and returns a `TenantContext`. |

**Precedence (live mode):** the **verified JWT `tenant_id` claim SUPERSEDES the
`X-Tenant-Id` header**. `X-Tenant-Id` is ignored when `LEAF_AUTH_LIVE=1`. As a
result, broker **ledger attribution and per-tenant kill-switch** (ADDENDUM §7/§8)
automatically key off the *verified* tenant in live mode, closing the header-spoof
gap the v1 stub left open.

`TenantContext` subclasses `str` and its string value **is** the `tenant_id`, so
every legacy consumer (`jobs.submit_job`, the broker ledger, SQLite `TEXT` binds,
`==` / dict-key / `json.dumps`) keeps working unchanged even in live mode — which
is why wiring the dependency into a route is a **one-line change** (§5).

**Rollback:** unset `LEAF_AUTH_LIVE` (or `=0`); the backend reverts to today's
open demo with zero code changes. No Auth0 dashboard change is required for
rollback — the toggle-off path never calls the verifier.

---

## 5. Integration — exact one-line wiring per route (root applies at wave-merge)

`require_tenant` lives in `server/deps.py` (the shared router seam). Adding auth
to a route is a one-liner: add the dependency, and (optionally) echo the tenant
into the success body via `deps.tenant_echo(body, tenant)` (a no-op when the
toggle is off → byte-identical).

**Claim echo (live only):** under `LEAF_AUTH_LIVE=1`, `deps.tenant_echo` additively
stamps **`tenant_id`, `org_id`, and `tier`** (all three §1 claims) into the success
body so the UI can render an honest tier chip; off-auth the body is unchanged.

**`GET /api/session`** — DONE by this lane (`server/routers/session.py`):
```python
def session(dwg: str = "rooftop_demo", tenant=Depends(deps.require_tenant)):
    ...
    return deps.tenant_echo(with_envelope_fields({"intake": ...}), tenant)
```

**`POST /api/run`** (`server/routers/jobs.py`) — the dependency is **ALREADY
present** (the backbone uses it for the `X-Tenant-Id` stub):
```python
def run(req: RunRequest, wait: int = 0, tenant_id: str = Depends(deps.require_tenant)):
```
In live mode `tenant_id` is a verified `TenantContext` automatically (still a
str, so `jobs.submit_job(tenant_id, ...)` is unchanged). To ALSO echo the tenant
into the 202 body, change the one return line to:
```python
content=deps.tenant_echo(with_envelope_fields({"job_id": job_id, "status": "submitted"}), tenant_id)
```
(The `?wait=1` path returns a frozen §3 run envelope; echoing there is optional
and the root's call.)

**`POST /api/author`** (`server/routers/author.py`) — add the dependency + echo:
```python
def author(req: AuthorRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    ...
    return deps.tenant_echo(with_envelope_fields({"tool": tool, "code": code, "preview": preview}), tenant)
```

> This lane deliberately did **not** edit `routers/jobs.py` / `routers/author.py`
> (a concurrent sibling owns the tools/author/execution chain this wave). The
> lines above are verbatim for the root to apply at the wave-integration step.
> Gating `/api/run` was **not** part of this lane's automated gate (scoped to
> `/api/session`).

---

## 6. Self-serve signup flow

Today (single-operator) signup is **blocked**: the production Pre-User-Registration
action (`leaf_website/docs/auth0-actions/pre-user-registration-bypass-for-invitations.js`)
denies personal-email domains unless the user has a pending invitation. Enabling
self-serve means:

1. **Auth0 Dashboard**: on the platform application's Database connection, toggle
   **"Disable Sign Ups" OFF**.
2. Deploy the self-serve Pre-User-Registration variant
   (`server/auth0-actions/pre-user-registration-selfserve.js`): personal emails
   are **allowed** by default; the invitation check is retained only to *attach*
   org metadata (never to gate), fail-open. An optional, default-empty
   `BLOCKED_DOMAINS` abuse list remains as a knob.
3. On login, the Post-Login action stamps the tenant/org/tier claims (§1). A
   brand-new self-serve user with no `organization_id` gets `tenant_id = sub`
   (solo tenant) and `tier = hosted_starter` until they create/join an org.

Sign-up → first-login → tenant claim → workspace resolution is the full path
from "new user" to "addressable Leaf workspace".

---

## 7. Config (env)

See `server/.env.auth.example`. Keys: `LEAF_AUTH_LIVE`, `LEAF_AUTH0_ISSUER`,
`LEAF_AUTH0_AUDIENCE`, `LEAF_AUTH0_JWKS_URL`, `LEAF_TENANT_CLAIM_NS`; optional
`LEAF_AUTH0_JWKS_FILE` (offline/test — verify against a local JWKS, no live
Auth0; used by `server/test_auth.py`), `LEAF_TENANTS_FILE`, `LEAF_WORKSPACE_BASE`.
**None of these is a Claude/Anthropic credential** (§0 invariant).

---

## 8. Operator: Auth0 dashboard steps

The automated gate (`server/test_auth.py`) uses a locally-generated RS256 keypair
and needs **no live Auth0**. The following are the human steps to make auth *live*
in production. Do not block the executor on them.

1. **DECISION — tenant/audience** *(ROOT-ASSUMED DEFAULT: reuse existing)*:
   reuse `leafautomation.us.auth0.com` + `api.leafdesign.ai` audience (matches the
   `aws-ai-manager` verify path), rather than a dedicated platform tenant. If
   reusing, **create a distinct Auth0 Application** (SPA/Regular Web App) client
   for the web platform. → confirm.
2. **DECISION — org model** *(ROOT-ASSUMED DEFAULT: free path)*: model orgs via
   **`app_metadata.organization_id`** (free; already populated by leaf_website),
   **not** paid Auth0 Organizations. The claim shape works with either; picking
   free avoids a plan bump. → confirm.
3. **Enable self-serve signup**: Authentication → Database → the platform
   connection → toggle **"Disable Sign Ups" OFF**.
4. **Deploy Pre-User-Registration (self-serve)**: Actions → replace/reconfigure
   the Pre-User-Registration action with
   `server/auth0-actions/pre-user-registration-selfserve.js`. **This RELAXES the
   live B2B gate — confirm intent.**
5. **Deploy Post-Login (tenant claim)**: Actions → Library → build from
   `server/auth0-actions/post-login-add-tenant-claim.js` → Deploy → add to the
   **Login** flow. No secrets required.
6. **MAU / plan headroom**: self-serve signup (+ Organizations, if chosen) may
   push past the current free tier (≈7,500 MAU) or require a B2B/Enterprise plan.
   **Approve any billing change.**
7. **Server env**: set `LEAF_AUTH_LIVE=1` and the `LEAF_AUTH0_*` values on the
   platform backend (defaults in `server/.env.auth.example` already match items
   1–2). `pip install -r server/requirements-auth.txt`.

---

## 9. Verification (this lane)

- `cd server && python test_auth.py` → **exit 0** (also `python -m pytest test_auth.py`):
  local-RS256 valid+claim ACCEPTED; tampered / foreign-key / expired / wrong-aud /
  wrong-iss / no-bearer REJECTED **401**; verified-but-missing-claim REJECTED
  **403**. Plus a `LEAF_AUTH_LIVE=1` TestClient matrix over `/api/session`
  (401 / 200+echo / 403).
- `node server/auth0-actions/post-login-add-tenant-claim.js` → prints the three
  namespaced claims for sample events.
- `cd server && python -m pytest tests/test_backbone.py -q` → **10/10** with the
  toggle off (byte-identical backbone; `X-Tenant-Id` stub preserved).

---

## 10. Machine-to-machine tenant claims

Client Credentials tokens do not run the Post-Login action because they have no
user. Deploy `server/auth0-actions/credentials-exchange-add-tenant-claim.js` on
the `credentials-exchange` trigger for approved machine clients.

The action does nothing unless the client has exact Auth0 metadata values for
`leaf_tenant_id` and `leaf_tenant_audience`. It also requires the requested
resource-server identifier to equal `leaf_tenant_audience`. Optional metadata
keys are `leaf_org_id` and `leaf_tier`. An absent or invalid tier becomes
`restricted`, which keeps the client read-only. Tenant and org IDs must match
the canonical `^[a-z0-9][a-z0-9_-]{0,62}$` rule.

Verify locally with:

```bash
node server/auth0-actions/credentials-exchange-add-tenant-claim.test.js
```

Before deployment, capture the current client metadata and the current
`credentials-exchange` bindings. Roll back by restoring both captured values.
