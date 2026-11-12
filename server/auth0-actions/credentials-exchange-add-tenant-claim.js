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
const VALID_TIERS = new Set(['restricted', 'self_hosted', 'hosted_free', 'hosted_starter', 'hosted_pro']);
const DEFAULT_TIER = 'restricted';
const ACCEPTANCE_TENANT_CLASS = 'non_customer_acceptance';
const TENANT_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;

// Roles (contract/AUTH.md §11.5): optional client metadata `leaf_roles`, a
// comma-separated list (Auth0 client metadata values are strings). Same shape
// rule + cap as server/roles.py. Default machine clients cannot mint
// `platform_admin` or the admin tier. The explicit service-admin exception
// below additionally requires the operator-owned Action secret allowlist.
const ROLE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const MAX_ROLES = 16;
const M2M_FORBIDDEN_ROLES = new Set(['platform_admin']);
const SERVICE_ADMIN_CLIENT_ID_PATTERN = /^[A-Za-z0-9_-]{8,128}$/;
const MAX_SERVICE_ADMIN_CLIENT_IDS = 16;

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

function isServiceAdminClient(event) {
  const raw = event && event.secrets && event.secrets.LEAF_SERVICE_ADMIN_CLIENT_IDS;
  if (typeof raw !== 'string') {
    return false;
  }
  let ids;
  try {
    ids = JSON.parse(raw);
  } catch {
    return false;
  }
  if (!Array.isArray(ids) || ids.length > MAX_SERVICE_ADMIN_CLIENT_IDS
      || ids.some(id => typeof id !== 'string' || id.trim() !== id
        || !SERVICE_ADMIN_CLIENT_ID_PATTERN.test(id))
      || new Set(ids).size !== ids.length) {
    return false;
  }
  return ids.includes(event && event.client && event.client.client_id);
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
  const serviceAdmin = requestedTier === 'admin' && isServiceAdminClient(event);
  const tier = serviceAdmin ? 'admin'
    : (VALID_TIERS.has(requestedTier) ? requestedTier : DEFAULT_TIER);
  const roles = deriveRoles(metadata);
  if (serviceAdmin) {
    // Reserve a slot so the admin tier and implied role always stay in step.
    if (roles.length === MAX_ROLES) {
      roles.pop();
    }
    roles.push('platform_admin');
    roles.sort();
  }
  const tenantClass = stringValue(metadata.leaf_tenant_class) === ACCEPTANCE_TENANT_CLASS
    ? ACCEPTANCE_TENANT_CLASS
    : null;

  return {
    tenant_id: tenantId,
    org_id: orgId,
    tier,
    roles,
    ...(serviceAdmin ? { principal_type: 'service_admin' } : {}),
    ...(tenantClass ? { tenant_class: tenantClass } : {}),
  };
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
  if (claims.principal_type) {
    api.accessToken.setCustomClaim(CLAIM_NS + 'principal_type', claims.principal_type);
  }
  if (claims.tenant_class) {
    api.accessToken.setCustomClaim(CLAIM_NS + 'tenant_class', claims.tenant_class);
  }
};

exports.deriveClaims = deriveClaims;
exports.deriveRoles = deriveRoles;
exports.CLAIM_NS = CLAIM_NS;
