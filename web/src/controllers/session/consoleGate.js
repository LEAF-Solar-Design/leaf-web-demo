// The console's session truth table (convergence W2b).
//
// /app used to derive its gate from a hand-rolled `authRequired` boolean that
// three independent observers wrote (the /api/session load, the catalog
// controller, the jobs poll) and one of them ALSO cleared — last writer wins.
// The state machine now lives in createSessionController.js; what is left is
// the console's rendering truth table, and it lives here so it can be proven
// exhaustively without mounting App.jsx (which owns three.js and a dozen
// controllers). Same reason demoState.js is a module: a pure decision helper
// with no React and no import.meta imports headless.
//
// NOTHING in here reads storage, fetches, or holds state. It maps
// (controller status, render mode, build config, token presence) -> what the
// console shows. One expression, one owner, one test.

/**
 * "Live mode with no session: 401s observed -> polls stop, footer says so."
 *
 * The controller's `required` status IS this, and it is the ONLY status that
 * means it: `checking` is the pre-answer state (the console renders its normal
 * loading surfaces), and `active` is a proven /api/session 200.
 */
export function consoleAuthRequired(status) {
  return status === 'required'
}

/**
 * The calm gate condition. A 401 on a SIGNED-IN session of an auth-unconfigured
 * build is deliberately NOT `signedOut`: the token was rejected and there is no
 * way to re-auth, so it must fall through to the pane-fail surface (Retry +
 * Back to the demo) rather than an inert overlay with no way forward.
 *
 * `isSignedIn` is passed as a FUNCTION, not a boolean, on purpose: the storage
 * read stays behind the same `&&` short circuit App has always had, so a
 * healthy render never touches localStorage at all.
 */
export function consoleSignedOut({ mock, authRequired, authConfigured, isSignedIn }) {
  return !mock && authRequired && (authConfigured || !isSignedIn())
}
