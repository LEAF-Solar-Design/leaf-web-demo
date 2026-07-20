// Pure demo-mode decision helper — no import.meta, no React, so `node` can
// import it headless for the check script. The deployed link (a VITE_MOCK=0
// build) still lands zero-click on the demo: when the live session load sees a
// 401 AND Auth0 is unconfigured (no sign-in is possible), the app auto-falls
// back to mock instead of parking on the gate.
//
// Returns true ONLY when there is an auth requirement we cannot satisfy and we
// are not already in mock — i.e. authRequired && !authConfigured && !mock. When
// Auth0 IS configured we keep the real SignedOutGate (the user can sign in), and
// when we are already in mock there is nothing to fall back to.
export function shouldAutoDemo({ authRequired, authConfigured, mock } = {}) {
  return !!(authRequired && !authConfigured && !mock)
}
