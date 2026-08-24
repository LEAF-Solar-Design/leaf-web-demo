/**
 * Auth0 POST-LOGIN Action — mint the Leaf namespaced tenant/org/tier claims.
 * ---------------------------------------------------------------------------
 * Concern 1 (Leaf PLATFORM identity). Runs in the Login flow AFTER credentials
 * are verified. It reads the subscription metadata leaf_website already stores
 * at `event.user.app_metadata.leaf.{organization_id, plan}` plus the
 * server-provisioned root sibling `app_metadata.leaf_platform_tenant_id` (see
 * leaf_website/lib/auth0.ts `SubscriptionMetadata`) and stamps three NAMESPACED
 * custom claims onto the ACCESS token so the server (server/auth.py) can resolve
 * a tenant WITHOUT a Management-API round-trip:
 *
 *     https://leafdesign.ai/tenant_id
 *     https://leafdesign.ai/org_id
 *     https://leafdesign.ai/tier
 *
 * INVARIANT (Concern 1 vs Concern 2): these claims NEVER carry a Claude /
 * Anthropic credential. The user's "sign in with Claude" Agent-SDK OAuth is a
 * SEPARATE concern owned by sibling hosted-oauth-spike. This action only
 * describes WHO the tenant is.
 *
 * DERIVATION:
 *   tenant_id = app_metadata.leaf_platform_tenant_id (else the legacy
 *               organization_id, else the Auth0 sub)
 *   org_id    = app_metadata.leaf.organization_id  (null for a solo user)
 *   tier      = PLAN_TIER[plan]  (DeploymentTier enum; default hosted_starter).
 *               Surface-only — the server does NOT enforce entitlement here.
 *
 * SUBSCRIPTION-LAPSE OVERRIDE (2026-07-20):
 *   app_metadata.leaf.subscription_active and .subscription_status are
 *   SIBLINGS of .plan (confirmed: leaf_website/lib/auth0.ts SubscriptionMetadata,
 *   lines 52-61 — subscription_active: boolean, subscription_status: string).
 *   If subscription_active === false, OR subscription_status is one of
 *   'canceled' | 'unpaid' | 'incomplete_expired', the tier is forced to
 *   'restricted' REGARDLESS of what .plan says — a stale/cached 'pro' plan
 *   value must not outrank a lapsed subscription. This is still surface-only:
 *   the server is expected to treat 'restricted' as no-entitlement.
 *   BACKWARD COMPAT: when both fields are absent (legacy users minted before
 *   this override existed, or any event missing app_metadata.leaf entirely),
 *   the override does not fire and PLAN_TIER[plan] applies exactly as before.
 *
 * SETUP (operator, Auth0 Dashboard):
 *   Actions -> Library -> Build Custom -> Login / Post Login -> paste this code
 *   -> Deploy -> add to the Login flow. No secrets required.
 *   REDEPLOY IS MANUAL: editing this file on disk does NOT change the live
 *   Action — an operator must re-paste/re-deploy in the Auth0 Dashboard (or via
 *   the Auth0 Deploy CLI) for any code change here, including this override,
 *   to take effect on real logins.
 *
 * SAMPLE INPUT  (event.user.app_metadata):
 *   { "leaf_platform_tenant_id": "bccb0d64-04c9-4108-bcc1-f27b8bb3924d",
 *     "leaf": { "organization_id": "website_org_cuid", "plan": "pro" } }
 * EXPECTED ACCESS-TOKEN CLAIMS (dry-run output below):
 *   { "https://leafdesign.ai/tenant_id": "bccb0d64-04c9-4108-bcc1-f27b8bb3924d",
 *     "https://leafdesign.ai/org_id":    "website_org_cuid",
 *     "https://leafdesign.ai/tier":      "hosted_pro" }
 *
 * Run the dry-run locally:  node server/auth0-actions/post-login-add-tenant-claim.js
 */

'use strict';

// Namespaced claim prefix — MUST match the server's LEAF_TENANT_CLAIM_NS.
const CLAIM_NS = 'https://leafdesign.ai/';

// plan (leaf_website app_metadata.leaf.plan) -> DeploymentTier
// (orchestration platform tier union: "self_hosted" | "hosted_starter" | "hosted_pro").
// Surface-only mapping; refine as billing plans evolve.
const PLAN_TIER = {
  free: 'hosted_starter',
  starter: 'hosted_starter',
  basic: 'hosted_starter',
  trial: 'hosted_starter',
  pro: 'hosted_pro',
  monthly: 'hosted_pro',
  yearly: 'hosted_pro',
  single_monthly: 'hosted_pro',
  single_yearly: 'hosted_pro',
  team_monthly: 'hosted_pro',
  team_yearly: 'hosted_pro',
  team: 'hosted_pro',
  business: 'hosted_pro',
  enterprise: 'self_hosted',
  self_hosted: 'self_hosted',
};
const DEFAULT_TIER = 'hosted_starter';

// subscription_status values that force tier='restricted' regardless of plan.
const LAPSED_STATUSES = new Set(['canceled', 'unpaid', 'incomplete_expired']);

// Role names (contract/AUTH.md §11.5) — same canonical shape rule as tenant
// ids; MUST match server/roles.py ROLE_NAME_RE / MAX_ROLES.
const ROLE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const MAX_ROLES = 16;
const PLATFORM_ADMIN_ROLE = 'platform_admin';

/**
 * Pure claim derivation — unit-testable, shared by the Action and the dry-run.
 * @param {object} event Auth0 post-login event.
 * @returns {{tenant_id: string, org_id: (string|null), tier: string}}
 */
function deriveClaims(event) {
  const user = (event && event.user) || {};
  const appMetadata = user.app_metadata || {};
  const leaf = appMetadata.leaf || {};
  const orgId = leaf.organization_id || null;
  // Kept at app_metadata root so leaf_website subscription PATCHes can replace
  // app_metadata.leaf without erasing the platform identity binding.
  const platformTenantId = appMetadata.leaf_platform_tenant_id || null;
  const sub = user.user_id || user.sub || null;
  const tenantId = platformTenantId || orgId || sub;
  const plan = (leaf.plan || '').toString().toLowerCase();
  let tier = PLAN_TIER[plan] || DEFAULT_TIER;

  // Subscription-lapse override — only fires when the field is explicitly
  // present; absent fields (legacy users) leave the plan-derived tier intact.
  const subscriptionActive = leaf.subscription_active;
  const subscriptionStatus = (leaf.subscription_status || '').toString().toLowerCase();
  if (subscriptionActive === false || LAPSED_STATUSES.has(subscriptionStatus)) {
    tier = 'restricted';
  }

  // Admin override (W14 admin self-edit lane, contract/AUTH.md §11.2) — fires
  // LAST, outranking the lapse override: `admin` is an OPERATOR grant, not a
  // billing state, and revoking it means removing the flag, not lapsing a
  // subscription. The flag lives at the app_metadata ROOT (like
  // leaf_platform_tenant_id) so leaf_website subscription PATCHes replacing
  // app_metadata.leaf can never mint or erase it; strict === true means a
  // truthy-but-wrong value ("yes", 1) mints nothing.
  if (appMetadata.leaf_admin === true) {
    tier = 'admin';
  }

  return { tenant_id: tenantId, org_id: orgId, tier: tier, roles: deriveRoles(event) };
}

/**
 * Role claim derivation (contract/AUTH.md §11.5). Union of:
 *   1. Auth0-native role assignments (event.authorization.roles — the
 *      dashboard's User Management -> Roles UI, the preferred operator path);
 *   2. the root-level app_metadata.leaf_roles array (API-writable sibling of
 *      leaf_platform_tenant_id, so leaf_website subscription PATCHes that
 *      replace app_metadata.leaf can never mint or erase a role);
 *   3. leaf_admin === true implies platform_admin (one operator flag, one
 *      staff identity — the tier override above and this role stay in step).
 * Normalized (trim/lowercase), validated against ROLE_NAME_PATTERN, deduped,
 * sorted, capped at MAX_ROLES. Unreadable input contributes nothing — roles
 * only ever ADD capability server-side, so dropping is always the safe answer.
 * The one exception to "dropping is safe": the leaf_admin-implied
 * platform_admin role is CONTRACT (leaf_admin implies platform_admin,
 * AUTH.md §11.5), so the cap reserves a slot for it — it can never be
 * displaced by other roles sorting ahead of it.
 * @param {object} event Auth0 post-login event.
 * @returns {string[]} sorted role names (possibly empty).
 */
function deriveRoles(event) {
  const user = (event && event.user) || {};
  const appMetadata = user.app_metadata || {};
  const authz = (event && event.authorization) || {};
  const adminImplied = appMetadata.leaf_admin === true;
  const candidates = []
    .concat(Array.isArray(authz.roles) ? authz.roles : [])
    .concat(Array.isArray(appMetadata.leaf_roles) ? appMetadata.leaf_roles : []);
  if (adminImplied) {
    candidates.push(PLATFORM_ADMIN_ROLE);
  }
  const seen = new Set();
  for (const item of candidates) {
    if (typeof item !== 'string') continue;
    const token = item.trim().toLowerCase();
    if (ROLE_NAME_PATTERN.test(token)) seen.add(token);
  }
  const sorted = Array.from(seen).sort();
  const capped = sorted.slice(0, MAX_ROLES);
  // Reserved slot for the implied role: when leaf_admin is set and enough
  // other roles sort ahead of platform_admin to push it past the cap, drop
  // the LAST kept role instead. platform_admin sorted after every kept role
  // here, so appending preserves sort order.
  if (adminImplied && capped.indexOf(PLATFORM_ADMIN_ROLE) === -1) {
    capped.length = MAX_ROLES - 1;
    capped.push(PLATFORM_ADMIN_ROLE);
  }
  return capped;
}

/**
 * Auth0 Post-Login handler.
 */
exports.onExecutePostLogin = async (event, api) => {
  const claims = deriveClaims(event);
  if (!claims.tenant_id) {
    // No org and no sub — nothing to assert. Leave the token unstamped; the
    // server treats a missing tenant claim as 403 (not provisioned).
    return;
  }
  api.accessToken.setCustomClaim(CLAIM_NS + 'tenant_id', claims.tenant_id);
  api.accessToken.setCustomClaim(CLAIM_NS + 'org_id', claims.org_id);
  api.accessToken.setCustomClaim(CLAIM_NS + 'tier', claims.tier);
  // Always set (possibly []) so a token's role state is explicit, never
  // ambiguous-absent. The server treats absent and [] identically (no roles).
  api.accessToken.setCustomClaim(CLAIM_NS + 'roles', claims.roles);
  // (ID-token mirror is optional; the server verifies the ACCESS token.)
};

exports.deriveClaims = deriveClaims;
exports.deriveRoles = deriveRoles;
exports.CLAIM_NS = CLAIM_NS;
exports.PLAN_TIER = PLAN_TIER;

// --------------------------------------------------------------------------- //
// Local dry-run (acceptance): `node post-login-add-tenant-claim.js` prints the
// namespaced claims a sample event would mint. Not executed inside Auth0.
// --------------------------------------------------------------------------- //
if (require.main === module) {
  const samples = [
    { name: 'canonical platform tenant + website org',
      event: { user: { app_metadata: {
        leaf_platform_tenant_id: 'bccb0d64-04c9-4108-bcc1-f27b8bb3924d',
        leaf: { organization_id: 'website_org_cuid', plan: 'pro' },
      } } } },
    { name: 'org + pro plan',
      event: { user: { app_metadata: { leaf: { organization_id: 'org_acme_solar', plan: 'pro' } } } } },
    { name: 'solo user, no org, free',
      event: { user: { user_id: 'auth0|abc123', app_metadata: { leaf: { plan: 'free' } } } } },
    { name: 'org + enterprise (self_hosted)',
      event: { user: { app_metadata: { leaf: { organization_id: 'org_big', plan: 'enterprise' } } } } },
    { name: 'no leaf metadata (sub only)',
      event: { user: { user_id: 'auth0|nometadata' } } },
    { name: 'pro plan but subscription_active=false -> restricted',
      event: { user: { app_metadata: { leaf: { organization_id: 'org_lapsed', plan: 'pro', subscription_active: false } } } } },
    { name: 'pro plan but subscription_status=canceled -> restricted',
      event: { user: { app_metadata: { leaf: { organization_id: 'org_canceled', plan: 'pro', subscription_active: true, subscription_status: 'canceled' } } } } },
    { name: 'pro plan, subscription_active=true, subscription_status=active -> unaffected',
      event: { user: { app_metadata: { leaf: { organization_id: 'org_good', plan: 'pro', subscription_active: true, subscription_status: 'active' } } } } },
    { name: 'operator-set leaf_admin=true -> admin (outranks plan and lapse)',
      event: { user: { app_metadata: { leaf_admin: true, leaf_platform_tenant_id: 'admin-tenant-1', leaf: { plan: 'pro', subscription_active: false } } } } },
    { name: 'leaf_admin="yes" (not strict true) -> mints nothing special',
      event: { user: { app_metadata: { leaf_admin: 'yes', leaf: { organization_id: 'org_x', plan: 'pro' } } } } },
    { name: 'Auth0-native role + app_metadata role -> merged, normalized, sorted',
      event: { authorization: { roles: ['Platform_Admin', 'ignored bad name!'] },
               user: { app_metadata: { leaf_roles: ['org_admin', 'platform_admin'],
                                       leaf: { organization_id: 'org_r', plan: 'pro' } } } } },
    { name: 'leaf_admin=true implies platform_admin role (and admin tier)',
      event: { user: { app_metadata: { leaf_admin: true, leaf: { plan: 'free' } } ,
                       user_id: 'auth0|staff1' } } },
    { name: 'leaf_roles not an array -> no roles minted',
      event: { user: { user_id: 'auth0|solo9', app_metadata: { leaf_roles: 'platform_admin', leaf: { plan: 'pro' } } } } },
  ];
  for (const s of samples) {
    const c = deriveClaims(s.event);
    const out = {};
    out[CLAIM_NS + 'tenant_id'] = c.tenant_id;
    out[CLAIM_NS + 'org_id'] = c.org_id;
    out[CLAIM_NS + 'tier'] = c.tier;
    out[CLAIM_NS + 'roles'] = c.roles;
    console.log('CASE:', s.name);
    console.log(JSON.stringify(out, null, 2));
  }
}
