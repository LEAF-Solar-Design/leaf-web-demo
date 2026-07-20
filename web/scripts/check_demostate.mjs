// Node oracle for M2 — asserts the shouldAutoDemo truth table on the real pure
// module (no import.meta / React, so `node` imports it directly).
import { shouldAutoDemo } from '../src/demoState.js'

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exit(1) }
}

// The ONE true case: a live build (mock:false) that hit a 401 with Auth0
// unconfigured must auto-fall back to the demo.
assert(
  shouldAutoDemo({ authRequired: true, authConfigured: false, mock: false }) === true,
  'authRequired && !authConfigured && !mock must be true',
)

// Every other combination is false.
assert(shouldAutoDemo({ authRequired: true, authConfigured: true, mock: false }) === false,
  'authConfigured true -> keep the real sign-in gate (false)')
assert(shouldAutoDemo({ authRequired: true, authConfigured: false, mock: true }) === false,
  'already in mock -> nothing to fall back to (false)')
assert(shouldAutoDemo({ authRequired: true, authConfigured: true, mock: true }) === false,
  'authConfigured && mock -> false')
assert(shouldAutoDemo({ authRequired: false, authConfigured: false, mock: false }) === false,
  'no auth requirement observed -> false')
assert(shouldAutoDemo({ authRequired: false, authConfigured: true, mock: false }) === false,
  'no auth requirement -> false')
assert(shouldAutoDemo({}) === false, 'empty args -> false')

console.log('DEMO_STATE_OK')
