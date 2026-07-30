# BILLING.md — subscription → tier design (census item 5)

Companion to `contract/AUTH.md` (identity; §11 owns the FROZEN tier vocabulary
this document feeds) and `contract/CONTRACT.md` (frozen core, unchanged).
Status: **design + dark skeleton**. The mapping and the sync endpoint are
implemented and gate-tested; nothing here bills anyone until the operator
activation ritual in §5 is performed. Lane: census item 5, 2026-07-23.

Two invariants frame everything (`contract/AUTH.md` §0, §11):

1. **Billing state never carries or references a Claude/Anthropic
   credential.** This document maps subscription facts to a tier string,
   nothing else — LLM supply stays swappable.
2. **The tier vocabulary is frozen.** Every value below is a member of AUTH.md
   §11.2; the claim-mintable subset {`restricted`, `self_hosted`,
   `hosted_starter`, `hosted_pro`} is the only vocabulary a verified identity
   or an org row can carry. Gate: `server/tests/test_auth_vocab_freeze.py`.

## 1. One mapping, two legs

Subscription state reaches the product on two independent legs that MUST
resolve tiers identically:

| Leg | When | Mechanism | Tier consumer |
|---|---|---|---|
| **Login leg** | every Auth0 login | Post-Login Action (`server/auth0-actions/post-login-add-tenant-claim.js`, hand-pasted into the Auth0 dashboard) reads `app_metadata.leaf` and mints the `https://leafdesign.ai/tier` claim | server lane JWT tier resolution (`server/auth.py` → `server/entitlements.py`) |
| **Stored leg** | on subscription change | leaf_website's Stripe webhook (§3 caller) → `POST /api/orgs/{org_id}/billing/tier-sync` updates the org row's stored tier | platform jobs lane (`platform/entitlements.py` branches on the org row) |

The canonical mapping lives in **`server/billing_tiers.py`**
(`PLAN_TIER` / `DEFAULT_TIER` / `LAPSED_STATUSES` / `derive_tier()`), stdlib-only so
both lanes and the static tests can load it. The Action is a hand-pasted JS
copy that can drift silently, so `load_action_billing_constants()` parses the
JS and `server/tests/test_billing_tiers.py` fails the build the moment the
copies disagree. **Change the mapping in `billing_tiers.py` first; the failing
parity test then names the exact JS constant the operator must re-paste.**

## 2. Plan vocabulary → tier

Keys are leaf_website `app_metadata.leaf.plan` values. Today's Stripe glue
(`leaf_website/lib/stripe.ts` `getPlanFromPriceId`) emits `monthly` / `yearly`.
Existing production metadata can also carry `single_*` and `team_*` names.
The remaining names are the agreed forward vocabulary for named products, so a
product rename ships here (and in the re-pasted Action) BEFORE marketing uses
it:

| plan (case-insensitive) | tier |
|---|---|
| `free`, `starter`, `basic`, `trial` | `hosted_starter` |
| `pro`, `monthly`, `yearly`, `single_monthly`, `single_yearly`, `team`, `team_monthly`, `team_yearly`, `business` | `hosted_pro` |
| `enterprise`, `self_hosted` | `self_hosted` |
| *unknown / absent* | `hosted_starter` (`DEFAULT_TIER`) |

Unknown plans deliberately resolve to the paid-entry default, never
`restricted`: a paying-intent identity must not be bounced because a new plan
name shipped before this table learned it. The lapse override (§4) is the only
path to `restricted`.

`restricted` is **lapse-reachable only**: `platform/models.py TIERS` (the
org-CREATION vocabulary) is `("self_hosted", "hosted_starter", "hosted_pro")`
— an org is never born restricted, it can only lapse into it, and only the
billing feed (or an operator) does that.

## 3. The stored-tier feed: `POST /api/orgs/{org_id}/billing/tier-sync`

Flag-gated skeleton, **dark by default**. Server half: `platform/billing.py` +
`platform/api.py` + `platform/store.py set_org_tier()`. Proven end-to-end by
`platform/tests/test_billing_sync.py` (12 cases, Postgres).

| Property | Contract |
|---|---|
| Activation | `LEAF_BILLING_SYNC_LIVE=1` AND `LEAF_BILLING_SYNC_SECRET` set; otherwise **503** and no write. Fail-closed configuration: a deployed-but-unconfigured endpoint can never be driven. |
| Hop auth | `X-Billing-Sync-Secret` header, constant-time compare (F16); missing/wrong → **403**. Same trusted-internal-caller model as the broker hop. |
| Body | Subscription FACTS only: `{plan?, subscription_active?, subscription_status?, stripe_subscription_id?, stripe_event_id?}` — the same fields the Action reads from `app_metadata.leaf`, so both legs derive from identical inputs. The body **never carries a literal tier** (single derivation path: the table decides) and never a credential. |
| Derivation | `billing_tiers.derive_tier(plan, subscription_active, subscription_status)`; result validated against the frozen claim-mintable subset before any write. |
| Org safety | Unknown org → **404**. Non-active org → **409**, untouched (billing must never resurrect an offboarding/deleted org into a billable tier). The write itself is an active-guarded `UPDATE … WHERE status='active'` (TOCTOU-safe); a state flip between read and write → **409**, no write. |
| Idempotency | The write sets an absolute tier; replaying an event is a no-op (`applied:false` in the response). No event-id dedup table yet (§6). |
| Response | `{org_id, previous_tier, tier, applied, stripe_subscription_id, stripe_event_id}` — the Stripe ids are **audit echoes for the caller's log line only**, deliberately not persisted (§6). |

### 3.1 Org discovery: `POST /api/billing/org-resolve`

The tier-sync path param is a **platform org UUID**, but leaf_website's org
ids are Prisma CUIDs. This endpoint is the durable bridge (the §6 follow-up,
built 2026-07-30): it resolves a verified external identity — the org OWNER's
Auth0 `sub`, which the Stripe webhook already has in hand — to the platform
org UUID through the `identity_bindings` row that org bootstrap created.

| Property | Contract |
|---|---|
| Gates | Identical to tier-sync: `LEAF_BILLING_SYNC_LIVE` + secret, else **503**; wrong/missing `X-Billing-Sync-Secret` → **403** (constant-time). |
| Body | `{external_authority: "auth0", external_subject: "<sub>"}` — POSTed in the body, never the URL, so the subject stays out of access logs. |
| Semantics | Read-only lookup of `identity_bindings` (active row) → `{org_id}`. Unknown identity → **404** (honest missing linkage: an account predating platform bootstrap). |
| Caller cadence | leaf_website calls this ONCE per org — the first subscription event with no stored linkage — then persists the UUID in its own DB (`Organization.platformOrgId`) and never asks again. `LEAF_BILLING_SYNC_ORG_MAP` remains an explicit operator override with top precedence. |

Dev posture: `POST /api/orgs` accepts an optional `external_subject` when auth
is off, bootstrapping org + binding in one call so leaf_website's e2e harness
can prove bootstrap → resolve → tier-sync without Auth0. With `LEAF_AUTH_LIVE=1`
that field is refused (422): a client-supplied identity is never trusted.

## 4. Lapse and grace

* Hard lapse: `subscription_status` ∈ `{canceled, unpaid, incomplete_expired}`
  → `restricted`, regardless of plan.
* Explicit deactivation: `subscription_active: false` → `restricted`.
* **`past_due` is deliberately NOT a hard lapse.** Payment failure enters the
  grace window on the leaf_website side (dunning); leaf_website expresses the
  end of grace by sending `subscription_active: false`. Until then the
  plan-derived tier stands.
* Absent fields (legacy metadata) leave the plan-derived tier intact —
  byte-for-byte the Action's backward-compat rule.

## 5. Activation ritual (operator; census item 5 hand-off)

Nothing below is chippable; each step is an operator action:

1. **Auth0 paste**: paste `server/auth0-actions/post-login-add-tenant-claim.js`
   into the Auth0 Post-Login flow (AUTH.md §10 rollback discipline applies:
   capture current bindings first). The parity gate proves the pasted copy
   matches `billing_tiers.py` — run `cd server && python -m pytest
   tests/test_billing_tiers.py -q` before pasting.
2. **Stripe products**: create/confirm the price → `plan` mapping leaf_website
   emits (`lib/stripe.ts getPlanFromPriceId`) against §2's table.
3. **Webhook leg** (leaf_website repo): on `customer.subscription.*` events,
   after updating `app_metadata.leaf`, POST the same facts to
   `/api/orgs/{org_id}/billing/tier-sync` with the shared secret. (Scaffold
   target: the existing Stripe webhook handler; this repo's endpoint is ready
   for it.)
4. **Turn the feed on**: set `LEAF_BILLING_SYNC_LIVE=1` +
   `LEAF_BILLING_SYNC_SECRET=<secret>` on the platform service, and give
   leaf_website the same secret.

## 6. Deferred decisions (recorded, not built)

* **Stripe id audit table** — the endpoint echoes `stripe_subscription_id` /
  `stripe_event_id` but persists neither; a billing-audit table (and event-id
  idempotency dedup) is a follow-up schema decision.
* **Metering** — usage-based billing (`/api/usage` `agent` aggregates) is out
  of scope for the tier rail entirely.
* **Org discovery for the webhook** — BUILT (2026-07-30), see §3.1. The
  durable linkage is leaf_website's DB column `Organization.platformOrgId`,
  first populated by `POST /api/billing/org-resolve` keyed on the org owner's
  Auth0 `sub`. The location this section originally named —
  `app_metadata.leaf.org_id` — was REJECTED: Auth0's PATCH merges only
  root-level `app_metadata` keys, and leaf_website's webhook replaces the
  whole `leaf` object on every subscription event, so a UUID stored there is
  erased by the very webhook that needs it; and writing it would require the
  platform to hold Auth0 Management API write credentials it deliberately does
  not have. (`app_metadata.leaf.organization_id` remains the WEBSITE org CUID,
  unrelated to the platform UUID.) If an account predates org bootstrap,
  resolve answers 404 and the webhook skips — acceptable: the login leg still
  corrects the claim, and the stored leg catches up on the next event after
  bootstrap.
