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
  }
);

assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'production_acceptance_a',
    leaf_tenant_audience: audience,
    leaf_tenant_class: 'non_customer_acceptance',
  })),
  {
    tenant_id: 'production_acceptance_a',
    org_id: 'production_acceptance_a',
    tier: 'restricted',
    tenant_class: 'non_customer_acceptance',
  }
);

assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tenant_class: 'customer',
  })),
  {
    tenant_id: 'org_leaf_demo',
    org_id: 'org_leaf_demo',
    tier: 'restricted',
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
  }
);

const stamped = {};
action.onExecuteCredentialsExchange(
  event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tenant_class: 'non_customer_acceptance',
  }),
  { accessToken: { setCustomClaim: (key, value) => { stamped[key] = value; } } }
).then(() => {
  assert.deepEqual(stamped, {
    'https://leafdesign.ai/tenant_id': 'org_leaf_demo',
    'https://leafdesign.ai/org_id': 'org_leaf_demo',
    'https://leafdesign.ai/tier': 'restricted',
    'https://leafdesign.ai/tenant_class': 'non_customer_acceptance',
  });
  console.log('credentials-exchange tenant claim tests passed');
});
