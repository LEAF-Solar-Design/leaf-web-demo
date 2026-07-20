// Pure demo-mode decision helper — no import.meta, no React, so `node` can
// import it headless for the check script. The deployed link (a VITE_MOCK=0
// build) still lands zero-click on the demo: when the live session load sees a
// 401 AND Auth0 is unconfigured (no sign-in is possible), the app auto-falls
// back to mock instead of parking on the gate.
//
// Returns true ONLY when there is an auth requirement we cannot satisfy and we
// are not already in mock — i.e. authRequired && !authConfigured && !mock &&
// !signedIn. When Auth0 IS configured we keep the real SignedOutGate (the user
// can sign in), when we are already in mock there is nothing to fall back to,
// and a session with a persisted token (signedIn) is a REAL live session even
// on a build whose Auth0 env is missing — a 401 there (expired/rejected token)
// must surface as an error, never silently switch the user onto mock data.
export function shouldAutoDemo({ authRequired, authConfigured, mock, signedIn } = {}) {
  return !!(authRequired && !authConfigured && !mock && !signedIn)
}
