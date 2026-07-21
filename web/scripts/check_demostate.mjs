// Node oracle for M2 — asserts the shouldAutoDemo truth table on the real pure
// module (no import.meta / React, so `node` imports it directly).
import { shouldAutoDemo } from '../src/demoState.js'

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exit(1) }
}

// The ONE true case: a signed-out live build (mock:false) that hit a 401 with
// Auth0 unconfigured must auto-fall back to the demo.
assert(
  shouldAutoDemo({ authRequired: true, authConfigured: false, mock: false, signedIn: false }) === true,
  'authRequired && !authConfigured && !mock && !signedIn must be true',
)
// Omitted signedIn (older call sites / headless checks) still counts as signed out.
assert(
  shouldAutoDemo({ authRequired: true, authConfigured: false, mock: false }) === true,
  'signedIn omitted -> treated as signed out (true)',
)

// Every other combination is false.
assert(shouldAutoDemo({ authRequired: true, authConfigured: false, mock: false, signedIn: true }) === false,
  'a signed-in session (persisted token) must NEVER silently switch to mock (false)')
assert(shouldAutoDemo({ authRequired: true, authConfigured: true, mock: false, signedIn: true }) === false,
  'signed-in + configured -> real error surface, not mock (false)')
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
