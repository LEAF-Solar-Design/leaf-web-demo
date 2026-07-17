# Coarse Per-Tenant Run-Quota — v1 Spec (implementable)

> **Mission pointer.** Canon: `~/.claude/MISSION.md` (absolute: `C:/Users/ehaug/.claude/MISSION.md`).
> Leaf is a WEB platform where each user gets a hosted Claude-Code-style agent harness (web auth =
> the user's own Claude subscription via the Agent SDK credit program; enterprise = API key), a
> per-tenant "mushy codebase" of deterministic tool files, an in-browser three.js CAD render, and
> CAD engine ops on Autodesk APS Design Automation. Registered tools run with ZERO LLM. This spec is
> the cheapest guard on the APS money path — a liability cap standing in until a priced SKU exists.

## What this is (and is not)
This is a concrete, implementable v1 spec for a **coarse server-side per-tenant run quota** on the
endpoints that spend real Autodesk money. It is explicitly **NOT full billing** — it is the liability
guard called out in `C:/tmp/mushy-platform/MATRIX.md` line 67 ("an autonomous CAD agent can loop and
burn LLM+APS+compute unbounded ... uncapped liability from one runaway stranger"), standing in until
the billing trigger in `BILLING-COMPLIANCE-LATER.md` (Doc 1) fires. This document is a spec; it does
**not** implement the quota — no code is added to `app.py` by this work item.

## Grounding artifacts (read; cited)
- `C:/tmp/leaf-web-demo/server/app.py` — the endpoints to gate: `GET /api/session` (`app.py:174`) and
  `POST /api/run` (`app.py:197`), each with an `if APS_LIVE:` branch (`app.py:177` and `app.py:210`).
  `APS_LIVE = os.environ.get("APS_LIVE","0")=="1"` (`app.py:47`); default `APS_LIVE=0` runs pure-python/
  mock and costs nothing.
- `C:/Users/ehaug/claudewalk-build/cadwalk-studio/src/lib/tenancy/types.ts:3` — `DeploymentTier =
  "self_hosted" | "hosted_starter" | "hosted_pro"`, the real union the limit map is keyed on.
- `C:/Users/ehaug/claudewalk-build/cadwalk-studio/src/lib/tenancy/postgres.ts:80-92` — the `deployments`
  table (`CREATE_DEPLOYMENTS_TABLE_SQL`): persists `tier` but has **no** usage/quota/counter column.
  MATRIX ¶22: the tier schema is "0% enforced (nothing branches on tier)" — this spec is the first
  thing that branches on tier.
- `C:/tmp/leaf-web-demo/contract/CONTRACT.md` §3 (result envelope, lines 58-76) and §4 (HTTP API,
  lines 78-86) — the over-quota response must fit this frozen envelope so the frontend needs no new
  error contract.

---

## (a) EXACTLY what it gates
The quota gates **only the two APS-money endpoints, and only on the `APS_LIVE=1` branch**:

| Endpoint | Location (pre-split) | Location (post-split, see concurrency note) | Gated when |
|---|---|---|---|
| `POST /api/run`    | `server/app.py:197` | `server/routers/tools.py`   | `APS_LIVE=1` (the `da.run_tool` path, `app.py:210-220`) |
| `GET /api/session` | `server/app.py:174` | `server/routers/session.py` | `APS_LIVE=1` (the `da.extract` path, `app.py:177-186`) |

- **`APS_LIVE=0` is un-metered (free/mock).** The default branch runs pure-python (`app.py:222-264`)
  and the cached-intake read (`app.py:187-189`); it spends no Autodesk money, so it is never counted
  against quota. Only the `APS_LIVE=1` branches — the ones that call `da.extract` / `da.run_tool` and
  spend the proven ~$0.007/run — decrement the quota.
- **Counting rule:** a quota unit = one *successful entry into the `APS_LIVE=1` branch* of either
  endpoint. Check-then-increment on entry to the branch (before dispatching the WorkItem); do not count
  `APS_LIVE=0` calls, and do not count requests already rejected as over-quota.

## (b) Tenant key
- **Tenant key = `deployment_id`** from the tenancy layer (`.../tenancy/types.ts:35`,
  `Deployment.deployment_id: UuidV7`).
- **Honest seam, stated plainly:** the demo `server/app.py` is single-tenant today — it has no auth,
  no tenant identity, and no run counter (confirmed: nothing in `app.py` reads a tenant id).
  Per-tenant enforcement **ACTIVATES once tenant identity/auth lands** (owned by other siblings — the
  auth/identity lane and the `project-job-schema` `org_id`/`deployment_id` anchor). This spec defines
  the seam; it does not pretend identity exists. Concretely: the enforcement middleware resolves
  `deployment_id` from the authenticated request context (production) or an `X-Deployment-Id` / dev
  header (development). Until that resolver is wired, the quota can run in a **single-bucket
  "demo" degraded mode** (one shared counter under a fixed key) so the guard is testable now and
  becomes per-tenant automatically when identity arrives — no code change at the call site.

## (c) Tier → daily-run-limit mapping
Keyed on the real `DeploymentTier` union (`.../tenancy/types.ts:3`). Limits are **runs per tenant per
UTC day** against the gated endpoints above:

| Tier (`DeploymentTier`) | Daily run limit (v1) | Rationale |
|---|---|---|
| `self_hosted`     | **N = 20/day** (mapped to the free coarse cap) | Self-hosted deployments still hit the *shared* APS app credential and Flex cap; cap them at the free number unless the operator declares self-hosted unmetered. |
| `hosted_starter`  | **200/day** (10x free) | Paid-ish starter headroom; a placeholder multiple, tunable. |
| `hosted_pro`      | **unmetered** (no daily cap; still logged) | Pro is trusted/high-tier; still counted for future billing metering, but not blocked. |
| *(unknown / unauthenticated)* | **N = 20/day** (free default) | Fail closed to the free cap; never fail open. |

- **Free-tier default: N = 20 runs/tenant/day.** *OPERATOR-ASSUMPTION, pending confirmation.*
- **Swap menu for the operator: {10, 20, 50}.** Changing N is a single constant, not a redesign.
- **`self_hosted` → free coarse cap** by default. *OPERATOR-ASSUMPTION, pending confirmation:* if the
  operator declares self-hosted unmetered (self-hosters carry their own APS bill), move it to the
  `hosted_pro` "unmetered" row.
- The `hosted_starter`/`hosted_pro` numbers are placeholders that harden into real values when the
  billing trigger (Doc 1) fires and the SKUs get priced; only the free-tier N and self_hosted mapping
  are decisions needed now.

## (d) Counter storage + daily reset
**Simplest durable thing — no cron, no scheduler.**
- **Storage:** a per-tenant, per-UTC-day counter. A small table adjacent to `deployments` (or a JSON
  row / KV entry keyed the same way). Suggested table:

  ```sql
  CREATE TABLE IF NOT EXISTS run_quota_counters (
    deployment_id UUID NOT NULL,
    day_utc       DATE NOT NULL,           -- YYYY-MM-DD in UTC
    used          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (deployment_id, day_utc)
  );
  ```
  (Mirrors the `CREATE TABLE IF NOT EXISTS` / injectable-client idiom of
  `.../tenancy/postgres.ts:80-92`; portable to the `platform/` Postgres the schema sibling owns.)

- **Reset by keying the bucket on the day, not by wiping it.** The bucket key is
  `(deployment_id, day_utc=<current UTC YYYY-MM-DD>)`. **A new UTC day = a new key = a fresh count of
  zero.** No cron job, no midnight sweep, no reset task — yesterday's row simply stops being read.
  (Old rows can be pruned lazily/opportunistically; retaining them costs nothing and doubles as raw
  usage history for the future billing meter in Doc 1.)
- **Increment:** atomic upsert `INSERT ... (deployment_id, day_utc, used) VALUES ($1,$2,1)
  ON CONFLICT (deployment_id, day_utc) DO UPDATE SET used = run_quota_counters.used + 1 RETURNING used`
  — check the returned `used` against the tier limit; race-safe because the DB does the increment.

## (e) Over-quota response (envelope-compatible)
When a request would exceed the tier's daily limit, the endpoint returns **HTTP 429** with a body that
fits the frozen result envelope (`contract/CONTRACT.md` §3) so the frontend renders "over quota —
upgrade" without a new error contract:

```jsonc
{
  "ok": false,
  "error": {
    "code": "quota_exceeded",
    "message": "Daily run limit reached for your plan (20/20). Resets 00:00 UTC. Upgrade for more.",
    "retryable": true,
    "tier": "self_hosted",
    "limit": 20,
    "used": 20
  }
}
```
- Shape matches the envelope's `{ok, ..., error}` contract (`CONTRACT.md` §3, lines 58-76): `ok:false`
  and a populated `error` object. `retryable:true` reflects that the cap lifts at the next UTC day.
  `tier`/`limit`/`used` give the frontend everything it needs to show an accurate upgrade prompt.
- HTTP status is **429 Too Many Requests** (distinct from the endpoint's existing `404`/`500`/`502`
  raised via `HTTPException` in `app.py`), so the frontend can branch on status without parsing.

## (f) Enforcement invariant
- **Enforcement is server-side and non-bypassable.** The check-and-increment happens inside the server,
  at the `APS_LIVE=1` branch, before any APS WorkItem is dispatched. A client cannot skip it, reset it,
  or spoof the count.
- **The frontend count is display-only.** Any usage number shown in the UI is advisory; the server is
  the sole source of truth and re-checks on every gated call. A tampered or absent client count changes
  nothing about enforcement.
- **Fail closed.** If tenant identity cannot be resolved, treat the request as the free/unknown tier
  (N=20) rather than allowing it uncounted; never fail open on the money path.

---

## Relationship to billing (explicit)
This coarse cap is **not** billing. It is the MATRIX-line-67 liability guard. When Doc 1's billing
trigger fires, the same per-tenant/per-day counter defined here becomes the metering source for
usage-based line items — cap-now and bill-later are one counter, two lenses. Until then, this is the
only thing standing between "one runaway stranger" and an uncapped Autodesk bill.

## Concurrency notes (stated facts as of 2026-07-17, not TODOs)

**(1) The backbone sibling is splitting `server/app.py` into routers TODAY.** The gate attaches to
`POST /api/run` and `GET /api/session`, which currently live in `C:/tmp/leaf-web-demo/server/app.py`
(`app.py:197` and `app.py:174`). After the split they move into `server/routers/tools.py`
(`POST /api/run`) and `server/routers/session.py` (`GET /api/session`). This spec is written against
those post-split names; the pre-split origin is `server/app.py`. The enforcement point is the
`APS_LIVE=1` branch inside each handler wherever it ends up living.

**(2) The `project-job-schema` sibling is concurrently building `platform/migrations/0001_project_job.sql`**
(brief: `C:/tmp/mushy-platform/plans/project-job-schema.md`). The `run_quota_counters` table above is
new and additive — it can live in the same `platform/` Postgres the schema sibling owns, keyed on the
same tenant anchor (`deployment_id`/`org_id`). It does not modify that sibling's five-table migration;
it is a sibling table added when the quota is actually implemented (out of scope for this spec doc).
