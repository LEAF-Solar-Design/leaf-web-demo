'use strict';

const assert = require('node:assert/strict');
const action = require('./credentials-exchange-add-tenant-claim');

const audience = 'https://api.leafdesign.ai';

function event(metadata, requestedAudience = audience) {
  return {
    client: { metadata },
    resource_server: { identifier: requestedAudience },
  };
}

assert.equal(action.deriveClaims(event({})), null);
assert.equal(action.deriveClaims(event({
  leaf_tenant_id: '../org_leaf_demo',
  leaf_tenant_audience: audience,
})), null);
assert.equal(action.deriveClaims(event({
  leaf_tenant_id: ' org_leaf_demo ',
  leaf_tenant_audience: audience,
})), null);
assert.equal(action.deriveClaims(event({
  leaf_tenant_id: 'org_leaf_demo',
  leaf_tenant_audience: ` ${audience} `,
})), null);
assert.equal(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
  }, 'https://unrelated.example')),
  null
);

assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tier: 'restricted',
  })),
  {
    tenant_id: 'org_leaf_demo',
    org_id: 'org_leaf_demo',
    tier: 'restricted',
    roles: [],
  }
);

assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_org_id: 'org_leaf_demo',
    leaf_tier: 'hosted_pro',
  })),
  {
    tenant_id: 'org_leaf_demo',
    org_id: 'org_leaf_demo',
    tier: 'hosted_pro',
    roles: [],
  }
);

assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tier: 'unexpected-tier',
  })),
  {
    tenant_id: 'org_leaf_demo',
    org_id: 'org_leaf_demo',
    tier: 'restricted',
    roles: [],
  }
);

// Roles (§11.5): comma-separated leaf_roles, normalized + sorted; bad names
// dropped; platform_admin FORBIDDEN for machine clients (like the admin tier).
assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tier: 'hosted_pro',
    leaf_roles: ' Org_Admin , platform_admin, bad name!, org_member ',
  })),
  {
    tenant_id: 'org_leaf_demo',
    org_id: 'org_leaf_demo',
    tier: 'hosted_pro',
    roles: ['org_admin', 'org_member'],
  }
);
assert.deepEqual(action.deriveRoles({}), []);
assert.deepEqual(action.deriveRoles({ leaf_roles: 'platform_admin' }), []);
assert.deepEqual(action.deriveRoles({ leaf_roles: 42 }), []);

const stamped = {};
action.onExecuteCredentialsExchange(
  event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_roles: 'org_admin',
  }),
  { accessToken: { setCustomClaim: (key, value) => { stamped[key] = value; } } }
).then(() => {
  assert.deepEqual(stamped, {
    'https://leafdesign.ai/tenant_id': 'org_leaf_demo',
    'https://leafdesign.ai/org_id': 'org_leaf_demo',
    'https://leafdesign.ai/tier': 'restricted',
    'https://leafdesign.ai/roles': ['org_admin'],
  });
  console.log('credentials-exchange tenant claim tests passed');
});
