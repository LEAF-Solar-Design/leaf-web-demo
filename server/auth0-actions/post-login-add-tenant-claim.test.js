'use strict';

const assert = require('node:assert/strict');
const action = require('./post-login-add-tenant-claim');

function event(appMetadata, authzRoles) {
  const e = { user: { user_id: 'auth0|test', app_metadata: appMetadata || {} } };
  if (authzRoles !== undefined) e.authorization = { roles: authzRoles };
  return e;
}

// 16 valid role names that ALL sort before 'platform_admin'.
const sixteenBefore = Array.from({ length: 16 }, (_, i) =>
  'a' + String(i + 1).padStart(2, '0'));

// Baseline: leaf_admin=true implies platform_admin (contract/AUTH.md §11.5).
assert.deepEqual(
  action.deriveRoles(event({ leaf_admin: true })),
  ['platform_admin']
);

// Implied role merges + dedupes with an equivalent explicit assignment.
assert.deepEqual(
  action.deriveRoles(event({ leaf_admin: true }, ['Platform_Admin'])),
  ['platform_admin']
);

// THE round-2 semantic case: 16 valid roles sort before platform_admin and
// leaf_admin=true. The cap must reserve a slot for the implied role — it
// displaces the last kept role instead of being sliced off.
{
  const roles = action.deriveRoles(
    event({ leaf_admin: true, leaf_roles: sixteenBefore }));
  assert.equal(roles.length, 16, 'cap must hold at MAX_ROLES');
  assert.ok(roles.includes('platform_admin'),
    'leaf_admin implies platform_admin even when 16 roles sort before it');
  assert.deepEqual(roles, sixteenBefore.slice(0, 15).concat('platform_admin'),
    'the LAST kept role is displaced; output stays sorted');
  assert.deepEqual(roles, [...roles].sort(), 'output must remain sorted');
}

// Same shape but well past the cap: 20 roles before platform_admin.
{
  const twenty = Array.from({ length: 20 }, (_, i) =>
    'a' + String(i + 1).padStart(2, '0'));
  const roles = action.deriveRoles(event({ leaf_admin: true, leaf_roles: twenty }));
  assert.equal(roles.length, 16);
  assert.ok(roles.includes('platform_admin'));
}

// Scope pin: the reserved slot is for the IMPLIED role only. An explicit
// platform_admin WITHOUT leaf_admin is subject to the plain cap like any
// other role (dropping only ever removes capability — safe direction).
assert.deepEqual(
  action.deriveRoles(
    event({ leaf_roles: sixteenBefore.concat('platform_admin') })),
  sixteenBefore
);

// Under the cap, the implied role changes nothing about ordinary merge order.
assert.deepEqual(
  action.deriveRoles(event({ leaf_admin: true, leaf_roles: ['org_admin'] },
    ['org_member'])),
  ['org_admin', 'org_member', 'platform_admin']
);

// leaf_admin must be STRICT true — truthy values mint no implied role.
assert.deepEqual(action.deriveRoles(event({ leaf_admin: 'yes' })), []);

// Invalid input still contributes nothing.
assert.deepEqual(
  action.deriveRoles(event({ leaf_admin: true, leaf_roles: 'platform_admin' })),
  ['platform_admin']
);
assert.deepEqual(action.deriveRoles(event({})), []);

// Handler stamps the roles claim (always set, possibly []).
{
  const stamped = {};
  action.onExecutePostLogin(
    event({ leaf_admin: true, leaf_roles: sixteenBefore,
            leaf_platform_tenant_id: 'admin-tenant-1' }),
    { accessToken: { setCustomClaim: (key, value) => { stamped[key] = value; } } }
  ).then(() => {
    assert.equal(stamped[action.CLAIM_NS + 'tier'], 'admin');
    const roles = stamped[action.CLAIM_NS + 'roles'];
    assert.equal(roles.length, 16);
    assert.ok(roles.includes('platform_admin'));
    console.log('post-login tenant claim tests passed');
  });
}
