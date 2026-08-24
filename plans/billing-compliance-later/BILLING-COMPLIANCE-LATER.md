# Billing & Compliance — Deferred, Trigger-Gated Build Plans

> **Mission pointer.** Canon: `~/.claude/MISSION.md` (absolute: `C:/Users/ehaug/.claude/MISSION.md`).
> Leaf is a WEB platform where each user gets a hosted Claude-Code-style agent harness authenticated
> with the user's OWN Claude subscription via the Agent SDK credit program (web = OAuth-via-Agent-SDK
> drawing the user's monthly credit; enterprise = API key). Per tenant: a "mushy codebase" (a git repo
> of deterministic heuristic tool files the agent edits), an in-browser three.js CAD render, and CAD
> engine ops on Autodesk APS Design Automation. The LLM is a **design-time tool factory, not a
> per-turn runtime** — registered tools execute with ZERO LLM. Billing and compliance are subsets of
> that mission's revenue/enterprise-readiness surface; both are correctly deferred until a named
> business fact makes them load-bearing.

## Grounding artifacts (read; cited, not re-derived)
- `C:/tmp/mushy-platform/MATRIX.md` — line 33 ("billing; spend-cap kill-switch — all net-new"),
  line 66 ("Compliance/offboarding — B2B PE-stamp buyers will demand SOC2, audit logs,
  deletion-on-request (which conflicts with the fleet's 'never hard-delete' rule)"), line 67
  (per-tenant spend-cap kill switch / uncapped liability), MATRIX ¶22 (the tenancy tier schema is
  "0% enforced (nothing branches on tier)").
- the orchestration platform's `src/lib/tenancy/types.ts:3` — the `DeploymentTier`
  literal union `"self_hosted" | "hosted_starter" | "hosted_pro"` that Stripe products map onto 1:1.
- the orchestration platform's `src/lib/tenancy/postgres.ts:104-126` — the
  `UPSERT_DEPLOYMENT_SQL` that already round-trips the `tier` column; the billing webhook flips
  `tier` here, no schema change needed.
- `C:/tmp/leaf-web-demo/server/app.py` — the APS-money endpoints (`POST /api/run`, `GET /api/session`
  on the `APS_LIVE=1` branch) whose usage the metered-billing build would source from the Doc 2 quota
  counter. (See sibling concurrency note below — these move into routers this same day.)
- Companion docs in this directory: `COARSE-QUOTA-V1-SPEC.md` (Doc 2) and
  `DELETION-OFFBOARDING-DESIGN.md` (Doc 3).

## Why deferred — the frame
Neither Stripe billing nor SOC2/compliance is built now, and neither should be, because each is gated
on a business fact that does not yet exist. Building either early is speculative infrastructure against
an unsigned customer. What we do instead: (1) name each build's **binary trigger condition** so the
decision to build is mechanical, not a judgment call re-litigated every planning cycle; (2) sketch what
each build contains **when** its trigger fires; (3) pull forward the one control that must exist before
either trigger (the coarse run-quota, Doc 2) and the one design that must be honored from day one (the
deletion/offboarding design, Doc 3), because retrofitting them later is expensive or impossible.

Everything below is a sketch of a future build, not an implementation plan. No Stripe code, no SOC2
infra, and no account-creation or payment action is taken by this work item.

---

## Section 1 — Billing (Stripe)

### Status: DEFERRED
**No priced SKU exists yet.** This is an operator-confirmable assumption
(*OPERATOR-ASSUMPTION, pending confirmation*: there is currently no priced SKU and no paying-customer
intent, which is what keeps full Stripe billing out of scope). Until that changes, the only money guard
worth having is the coarse per-tenant run-quota in Doc 2 — a **liability cap** (MATRIX line 67), not a
billing system.

### Binary trigger condition (named)
> **BUILD BILLING WHEN:** *a priced SKU is defined AND there is at least one paying-customer intent*
> (a customer who has verbally or contractually committed to pay for a named tier at a named price).

Both clauses are required. A priced SKU with zero buyers is a pricing-page exercise, not a reason to
wire Stripe; a willing buyer with no priced SKU has nothing to check out against.

### What the build contains when the trigger fires (sketch)
- **Stripe products/prices mapped 1:1 to the existing `DeploymentTier` values** — `self_hosted`,
  `hosted_starter`, `hosted_pro` (`.../tenancy/types.ts:3`). The tier enum is the SKU catalog; do not
  invent a parallel plan taxonomy.
- **Checkout + Customer Portal** — Stripe-hosted Checkout for signup/upgrade, Customer Portal for
  self-serve plan changes and payment-method management. No custom card handling.
- **A webhook that flips the `tier` column on the `deployments` row.** On
  `checkout.session.completed` / `customer.subscription.updated`, resolve the deployment and update
  `tier` via the already-existing round-trip in `.../tenancy/postgres.ts:104-126`
  (`UPSERT_DEPLOYMENT_SQL` already selects/updates `tier`; no migration needed to change plans).
- **Metered usage sourced from the Doc 2 quota counter.** The per-tenant per-UTC-day run counter
  specified in `COARSE-QUOTA-V1-SPEC.md` is the metering source of truth. Billing reads that counter
  for usage-based line items or overage — it does not stand up a second, independent meter. This is the
  seam that makes Doc 2 worth building now: the coarse cap and the future billing meter are the same
  counter viewed through two lenses (cap-now, bill-later).
- **Tier→limit enforcement stays server-side** (Doc 2's invariant). Billing changes *what* a tenant
  is entitled to (their tier); the quota layer enforces it. Stripe never becomes the enforcement point.

### Operator action DEFERRED to trigger-time (not now)
- Create/authorize the Stripe account and the product/price objects. This is an
  **account-creation + payment action that only the human operator can perform**, and it is explicitly
  deferred until the billing trigger fires — do not do it now.

---

## Section 2 — Compliance (SOC2 / audit-log / data-residency)

### Status: DEFERRED (enterprise-sales-gated)
Compliance work (SOC2 Type II, append-only audit logging, data-residency/region pinning, Vanta-style
evidence automation) is enterprise-sales-gated. MATRIX line 66 names the buyer and the demand:
"B2B PE-stamp buyers will demand SOC2, audit logs, deletion-on-request (which conflicts with the
fleet's 'never hard-delete' rule)." That conflict is resolved by Doc 3, which is pulled forward
precisely because it is a compliance prerequisite that cannot be retrofitted.

### Binary trigger condition (named)
> **BUILD COMPLIANCE WHEN:** *a signed enterprise LOI or contract exists whose terms require SOC2
> Type II or data-residency.*

The trigger is a signature on paper that names the requirement — not a sales conversation, not a
prospect's wishlist. SOC2 Type II specifically requires an observation window (months of evidence), so
the trigger must fire early enough relative to the contractual deadline; that lead-time calculation is
part of the trigger-time build, not a reason to build speculatively now.

### What the build contains when the trigger fires (sketch)
- **An append-only audit-log pipeline** — every security-relevant and tenant-data-touching action
  (auth, entitlement change, credential set/rotate, **every hard PURGE from Doc 3**, offboarding)
  emits an immutable, timestamped, tenant-attributed log line to a write-once sink. This is the SOC2
  evidence spine and the GDPR deletion-audit record simultaneously.
- **Per-tenant region pinning for data residency** — tenant records (Postgres row, vault refs, APS OSS
  bucket, mushy repo) provisioned into a tenant-selected region; the platform enforces that a tenant's
  data and compute stay in-region. (This composes with the keystone credential-broker/egress-proxy
  named in MATRIX ¶51-57, which is the natural per-tenant egress chokepoint.)
- **Evidence-collection automation (Vanta-style)** — continuous control monitoring, access reviews,
  and evidence export mapped to the SOC2 trust-service criteria.

### Prerequisite pulled forward: the Doc 3 deletion/offboarding design
**`DELETION-OFFBOARDING-DESIGN.md` (Doc 3) is a PREREQUISITE control for this build, not a companion to
it.** Deletion-on-request is an explicit SOC2/GDPR line item, and it is the single control that cannot
be bolted on after the fact: if the tenant storage and the Project/Job schema do not carry the deletion
columns and the purge-cascade from day one, then when the compliance trigger fires there is retained
tenant data across stores with no sanctioned, audited way to remove it. That is exactly why Doc 3 is a
DO-NOW design requirement even though this whole compliance build is deferred. Each hard PURGE that
Doc 3 performs must emit an audit-log line — which is why Doc 3 and this section's audit-log pipeline
converge on the same requirement.

---

## Concurrency notes (stated facts as of 2026-07-17, not TODOs)

**(1) The backbone sibling is splitting `server/app.py` into routers TODAY.** The APS-money endpoints
this plan references — `POST /api/run` and `GET /api/session` — currently live in
`C:/tmp/leaf-web-demo/server/app.py` (pre-split origin: `app.py:197` and `app.py:174`). After the
split they move into `server/routers/tools.py` (`POST /api/run`) and `server/routers/session.py`
(`GET /api/session`). The future billing-metering read (and the Doc 2 quota gate) attach at the
post-split router locations; the pre-split `app.py` is where they live only until the backbone sibling
lands. This plan is written against the post-split names with the pre-split origin recorded.

**(2) The `project-job-schema` sibling is concurrently building `platform/migrations/0001_project_job.sql`.**
Per its brief at `C:/tmp/mushy-platform/plans/project-job-schema.md`, that migration's current v1 DDL
carries `orgs.status`/`orgs.offboarded_at` and a `projects.status` enum value of `'deleted'`, but does
**not** yet carry the `deleted_at` / `purge_requested_at` / `purge_completed_at` columns that Doc 3
requires. Doc 3 states that column set as a binding integration obligation on that sibling's schema.
