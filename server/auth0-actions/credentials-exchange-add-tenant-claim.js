/**
 * Add Leaf tenant claims to approved machine-to-machine access tokens.
 *
 * The action is inert unless the Auth0 client has both metadata fields:
 *   leaf_tenant_id
 *   leaf_tenant_audience
 *
 * The requested resource server must exactly match leaf_tenant_audience. This
 * keeps the global credentials-exchange flow safe for unrelated M2M clients.
 */
'use strict';

const CLAIM_NS = 'https://leafdesign.ai/';
const VALID_TIERS = new Set(['restricted', 'self_hosted', 'hosted_starter', 'hosted_pro']);
const DEFAULT_TIER = 'restricted';
const TENANT_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;

// Roles (contract/AUTH.md §11.5): optional client metadata `leaf_roles`, a
// comma-separated list (Auth0 client metadata values are strings). Same shape
// rule + cap as server/roles.py. `platform_admin` is EXCLUDED for machine
// clients — the same posture that keeps `admin` out of VALID_TIERS above: a
// staff identity requires a human login, never a client secret.
const ROLE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const MAX_ROLES = 16;
const M2M_FORBIDDEN_ROLES = new Set(['platform_admin']);

function stringValue(value) {
  return typeof value === 'string' ? value : '';
}

function deriveRoles(metadata) {
  const raw = stringValue(metadata.leaf_roles);
  const seen = new Set();
  for (const part of raw.split(',')) {
    const token = part.trim().toLowerCase();
    if (ROLE_NAME_PATTERN.test(token) && !M2M_FORBIDDEN_ROLES.has(token)) {
      seen.add(token);
    }
  }
  return Array.from(seen).sort().slice(0, MAX_ROLES);
}

function deriveClaims(event) {
  const client = (event && event.client) || {};
  const metadata = client.metadata || {};
  const tenantId = stringValue(metadata.leaf_tenant_id);
  const allowedAudience = stringValue(metadata.leaf_tenant_audience);
  const requestedAudience = stringValue(
    event && event.resource_server && event.resource_server.identifier
  );

  if (!TENANT_ID_PATTERN.test(tenantId)
      || !allowedAudience
      || requestedAudience !== allowedAudience) {
    return null;
  }

  const configuredOrgId = stringValue(metadata.leaf_org_id);
  if (configuredOrgId && !TENANT_ID_PATTERN.test(configuredOrgId)) {
    return null;
  }
  const orgId = configuredOrgId || tenantId;
  const requestedTier = stringValue(metadata.leaf_tier);
  const tier = VALID_TIERS.has(requestedTier) ? requestedTier : DEFAULT_TIER;

  return { tenant_id: tenantId, org_id: orgId, tier, roles: deriveRoles(metadata) };
}

exports.onExecuteCredentialsExchange = async (event, api) => {
  const claims = deriveClaims(event);
  if (!claims) {
    return;
  }

  api.accessToken.setCustomClaim(CLAIM_NS + 'tenant_id', claims.tenant_id);
  api.accessToken.setCustomClaim(CLAIM_NS + 'org_id', claims.org_id);
  api.accessToken.setCustomClaim(CLAIM_NS + 'tier', claims.tier);
  api.accessToken.setCustomClaim(CLAIM_NS + 'roles', claims.roles);
};

exports.deriveClaims = deriveClaims;
exports.deriveRoles = deriveRoles;
exports.CLAIM_NS = CLAIM_NS;
