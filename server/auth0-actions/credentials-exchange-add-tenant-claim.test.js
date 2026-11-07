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
    leaf_tenant_class: 'non_customer_acceptance',
    leaf_roles: 'org_admin',
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
    leaf_tenant_id: 'production_acceptance_a',
    leaf_tenant_audience: audience,
    leaf_tenant_class: 'non_customer_acceptance',
    leaf_roles: 'owner',
  })),
  {
    tenant_id: 'production_acceptance_a',
    org_id: 'production_acceptance_a',
    tier: 'restricted',
    roles: ['owner'],
    tenant_class: 'non_customer_acceptance',
  }
);

assert.deepEqual(
  action.deriveClaims(event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tenant_class: 'customer',
    leaf_roles: 'org_admin',
  })),
  {
    tenant_id: 'org_leaf_demo',
    org_id: 'org_leaf_demo',
    tier: 'restricted',
    roles: ['org_admin'],
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
// dropped; platform_admin forbidden for default machine clients (like admin).
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

const serviceClientId = 'LeafService_1234';
const adminMetadata = {
  leaf_tenant_id: 'org_leaf_demo',
  leaf_tenant_audience: audience,
  leaf_tier: 'admin',
  leaf_roles: 'owner, platform_admin',
};

function serviceEvent({
  clientId = serviceClientId,
  secret = JSON.stringify([serviceClientId]),
  metadata = adminMetadata,
  requestedAudience = audience,
} = {}) {
  return {
    client: { client_id: clientId, metadata },
    resource_server: { identifier: requestedAudience },
    secrets: { LEAF_SERVICE_ADMIN_CLIENT_IDS: secret },
  };
}

const defaultDeniedClaims = {
  tenant_id: 'org_leaf_demo',
  org_id: 'org_leaf_demo',
  tier: 'restricted',
  roles: ['owner'],
};
const serviceAdminClaims = {
  ...defaultDeniedClaims,
  tier: 'admin',
  roles: ['owner', 'platform_admin'],
  principal_type: 'service_admin',
};

// Admin metadata alone cannot opt in, including lookalike secret/marker keys.
assert.deepEqual(action.deriveClaims(event(adminMetadata)), defaultDeniedClaims);
const metadataOnly = serviceEvent({ metadata: {
  ...adminMetadata,
  LEAF_SERVICE_ADMIN_CLIENT_IDS: JSON.stringify([serviceClientId]),
  leaf_service_admin: 'true',
  principal_type: 'service_admin',
} });
delete metadataOnly.secrets;
assert.deepEqual(action.deriveClaims(metadataOnly), defaultDeniedClaims);

for (const secret of [
  null, false, 42, [serviceClientId], '', 'not json', 'null', '{}', 'true',
  JSON.stringify(serviceClientId), '[]',
  JSON.stringify([serviceClientId, serviceClientId]),
  JSON.stringify([serviceClientId, '*']),
  JSON.stringify([serviceClientId, 'LeafService_*']),
  JSON.stringify([serviceClientId, '']),
  JSON.stringify([serviceClientId, null]),
  JSON.stringify([serviceClientId, 42]),
  JSON.stringify([serviceClientId, 'short']),
  JSON.stringify([serviceClientId, 'x'.repeat(129)]),
  JSON.stringify([serviceClientId, 'nonascii_é']),
  JSON.stringify([serviceClientId, 'bad.id_123']),
  ...[' ', '\t', '\n', '\r'].flatMap(space => [
    JSON.stringify([serviceClientId, space + serviceClientId]),
    JSON.stringify([serviceClientId, serviceClientId + space]),
  ]),
  JSON.stringify([serviceClientId, ...Array.from({ length: 16 }, (_, i) => `Client_${i}`)]),
]) {
  assert.deepEqual(action.deriveClaims(serviceEvent({ secret })), defaultDeniedClaims,
    `invalid allowlist must fail closed: ${JSON.stringify(secret)}`);
}

assert.deepEqual(action.deriveClaims(serviceEvent()), serviceAdminClaims);
assert.deepEqual(action.deriveClaims(serviceEvent({
  metadata: { ...adminMetadata, leaf_roles: '' },
})), { ...serviceAdminClaims, roles: ['platform_admin'] });
for (const clientId of ['Client_1', 'x'.repeat(128)]) {
  assert.deepEqual(action.deriveClaims(serviceEvent({
    clientId, secret: JSON.stringify([clientId]),
  })), serviceAdminClaims);
}
assert.deepEqual(action.deriveClaims(serviceEvent({
  secret: JSON.stringify([serviceClientId,
    ...Array.from({ length: 15 }, (_, i) => `Client_${i}`)]),
})), serviceAdminClaims);

for (const clientId of [
  undefined, null, '', serviceClientId.toLowerCase(), 'LeafServ1ce_1234',
  serviceClientId + 'x', serviceClientId + ' ', '*',
]) {
  const candidate = serviceEvent();
  candidate.client.client_id = clientId;
  assert.deepEqual(action.deriveClaims(candidate), defaultDeniedClaims);
}
for (const leaf_tier of ['hosted_pro', 'restricted', 'Admin', ' admin ', '']) {
  assert.deepEqual(action.deriveClaims(serviceEvent({
    metadata: { ...adminMetadata, leaf_tier },
  })), {
    ...defaultDeniedClaims,
    tier: leaf_tier === 'hosted_pro' ? 'hosted_pro' : 'restricted',
  });
}
assert.equal(action.deriveClaims(serviceEvent({
  requestedAudience: 'https://unrelated.example',
})), null);
for (const metadata of [
  { ...adminMetadata, leaf_tenant_id: '' },
  { ...adminMetadata, leaf_tenant_id: '../org_leaf_demo' },
  { ...adminMetadata, leaf_tenant_id: ' org_leaf_demo ' },
  { ...adminMetadata, leaf_org_id: '../invalid' },
  { ...adminMetadata, leaf_tenant_audience: '' },
]) {
  assert.equal(action.deriveClaims(serviceEvent({ metadata })), null);
}

// Implied platform_admin survives the role cap, without changing default roles.
const manyRoles = Array.from({ length: 20 }, (_, i) => `a_role_${i}`).join(',');
const capped = action.deriveClaims(serviceEvent({
  metadata: { ...adminMetadata, leaf_roles: manyRoles },
}));
assert.equal(capped.tier, 'admin');
assert.equal(capped.roles.length, 16);
assert.ok(capped.roles.includes('platform_admin'));
assert.deepEqual(capped.roles.filter(role => role !== 'platform_admin'),
  action.deriveRoles({ leaf_roles: manyRoles }).slice(0, 15));

const serviceStamped = {};
action.onExecuteCredentialsExchange(serviceEvent(), {
  accessToken: { setCustomClaim: (key, value) => { serviceStamped[key] = value; } },
}).then(() => {
  assert.deepEqual(serviceStamped, {
    'https://leafdesign.ai/tenant_id': 'org_leaf_demo',
    'https://leafdesign.ai/org_id': 'org_leaf_demo',
    'https://leafdesign.ai/tier': 'admin',
    'https://leafdesign.ai/roles': ['owner', 'platform_admin'],
    'https://leafdesign.ai/principal_type': 'service_admin',
  });
});

const stamped = {};
action.onExecuteCredentialsExchange(
  event({
    leaf_tenant_id: 'org_leaf_demo',
    leaf_tenant_audience: audience,
    leaf_tenant_class: 'non_customer_acceptance',
    leaf_roles: 'owner',
  }),
  { accessToken: { setCustomClaim: (key, value) => { stamped[key] = value; } } }
).then(() => {
  assert.deepEqual(stamped, {
    'https://leafdesign.ai/tenant_id': 'org_leaf_demo',
    'https://leafdesign.ai/org_id': 'org_leaf_demo',
    'https://leafdesign.ai/tier': 'restricted',
    'https://leafdesign.ai/roles': ['owner'],
    'https://leafdesign.ai/tenant_class': 'non_customer_acceptance',
  });
  console.log('credentials-exchange tenant claim tests passed');
});
