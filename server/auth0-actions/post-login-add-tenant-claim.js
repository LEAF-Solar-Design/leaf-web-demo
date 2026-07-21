/**
 * Auth0 POST-LOGIN Action — mint the Leaf namespaced tenant/org/tier claims.
 * ---------------------------------------------------------------------------
 * Concern 1 (Leaf PLATFORM identity). Runs in the Login flow AFTER credentials
 * are verified. It reads the subscription metadata leaf_website already stores
 * at `event.user.app_metadata.leaf.{organization_id, plan}` (see
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
 *   tenant_id = app_metadata.leaf.organization_id  (else the Auth0 sub — a
 *               single-user tenant until they create/join an org)
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
 *   { "leaf": { "organization_id": "org_acme_solar", "plan": "pro" } }
 * EXPECTED ACCESS-TOKEN CLAIMS (dry-run output below):
 *   { "https://leafdesign.ai/tenant_id": "org_acme_solar",
 *     "https://leafdesign.ai/org_id":    "org_acme_solar",
 *     "https://leafdesign.ai/tier":      "hosted_pro" }
 *
 * Run the dry-run locally:  node server/auth0-actions/post-login-add-tenant-claim.js
 */

'use strict';

// Namespaced claim prefix — MUST match the server's LEAF_TENANT_CLAIM_NS.
const CLAIM_NS = 'https://leafdesign.ai/';

// plan (leaf_website app_metadata.leaf.plan) -> DeploymentTier
// (cadwalk-studio types.ts: "self_hosted" | "hosted_starter" | "hosted_pro").
// Surface-only mapping; refine as billing plans evolve.
const PLAN_TIER = {
  free: 'hosted_starter',
  starter: 'hosted_starter',
  basic: 'hosted_starter',
  trial: 'hosted_starter',
  pro: 'hosted_pro',
  monthly: 'hosted_pro',
  yearly: 'hosted_pro',
  team: 'hosted_pro',
  business: 'hosted_pro',
  enterprise: 'self_hosted',
  self_hosted: 'self_hosted',
};
const DEFAULT_TIER = 'hosted_starter';

// subscription_status values that force tier='restricted' regardless of plan.
const LAPSED_STATUSES = new Set(['canceled', 'unpaid', 'incomplete_expired']);

/**
 * Pure claim derivation — unit-testable, shared by the Action and the dry-run.
 * @param {object} event Auth0 post-login event.
 * @returns {{tenant_id: string, org_id: (string|null), tier: string}}
 */
function deriveClaims(event) {
  const user = (event && event.user) || {};
  const leaf = (user.app_metadata && user.app_metadata.leaf) || {};
  const orgId = leaf.organization_id || null;
  const sub = user.user_id || user.sub || null;
  const tenantId = orgId || sub;
  const plan = (leaf.plan || '').toString().toLowerCase();
  let tier = PLAN_TIER[plan] || DEFAULT_TIER;

  // Subscription-lapse override — only fires when the field is explicitly
  // present; absent fields (legacy users) leave the plan-derived tier intact.
  const subscriptionActive = leaf.subscription_active;
  const subscriptionStatus = (leaf.subscription_status || '').toString().toLowerCase();
  if (subscriptionActive === false || LAPSED_STATUSES.has(subscriptionStatus)) {
    tier = 'restricted';
  }

  return { tenant_id: tenantId, org_id: orgId, tier: tier };
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
  // (ID-token mirror is optional; the server verifies the ACCESS token.)
};

exports.deriveClaims = deriveClaims;
exports.CLAIM_NS = CLAIM_NS;
exports.PLAN_TIER = PLAN_TIER;

// --------------------------------------------------------------------------- //
// Local dry-run (acceptance): `node post-login-add-tenant-claim.js` prints the
// namespaced claims a sample event would mint. Not executed inside Auth0.
// --------------------------------------------------------------------------- //
if (require.main === module) {
  const samples = [
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
  ];
  for (const s of samples) {
    const c = deriveClaims(s.event);
    const out = {};
    out[CLAIM_NS + 'tenant_id'] = c.tenant_id;
    out[CLAIM_NS + 'org_id'] = c.org_id;
    out[CLAIM_NS + 'tier'] = c.tier;
    console.log('CASE:', s.name);
    console.log(JSON.stringify(out, null, 2));
  }
}
