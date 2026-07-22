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

function stringValue(value) {
  return typeof value === 'string' ? value : '';
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

  return { tenant_id: tenantId, org_id: orgId, tier };
}

exports.onExecuteCredentialsExchange = async (event, api) => {
  const claims = deriveClaims(event);
  if (!claims) {
    return;
  }

  api.accessToken.setCustomClaim(CLAIM_NS + 'tenant_id', claims.tenant_id);
  api.accessToken.setCustomClaim(CLAIM_NS + 'org_id', claims.org_id);
  api.accessToken.setCustomClaim(CLAIM_NS + 'tier', claims.tier);
};

exports.deriveClaims = deriveClaims;
exports.CLAIM_NS = CLAIM_NS;
