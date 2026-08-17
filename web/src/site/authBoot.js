// Boot-time routing decisions for an Auth0 return leg, kept pure so they can be
// tested without mounting the console (which owns three.js and every controller).
//
// THE DEFECT THESE CLOSE (2026-08-17 D1a defect #6): the SPA's redirect_uri is
// the bare ORIGIN (auth.js: `redirect_uri: window.location.origin`, asserted
// exactly by web/scripts/deployed_auth0_spa_origin_acceptance.mjs), so the
// callback never lands on /try -- it lands on `/` carrying ?code&state. The old
// gate armed only for `/try`, and `bootWantsApp` sent every other path straight
// into the console, which mounted and fired its whole API burst BEFORE
// handleRedirectCallback finished the code exchange and stored leaf.jwt. Eleven
// 401s later the session controller was latched into `required` holding a valid
// token, and the user sat on a signed-out-looking surface until a manual reload.
//
// The gate is armed by the callback QUERY, not by the path, so the token is
// persisted before the first authed render on every landing.

import { isAuthRedirectCallback } from '../auth.js'

const APP_BOOT_PARAMS = ['fixture', 'dev', 'drawing']

// True while an Auth0 code exchange is still in flight for THIS page load.
// Nothing that talks to /api may render until it resolves.
export function shouldDeferForAuthCallback(search = window.location.search) {
  return isAuthRedirectCallback(search)
}

// BOOT BACK-COMPAT: any of ?fixture= ?dev= ?drawing= (or ?demo=/?ops= off /try)
// boots straight into the console regardless of path. An auth callback also
// boots the console, but only AFTER shouldDeferForAuthCallback clears -- the
// deferral is what keeps the burst behind the token write.
export function bootWantsApp(search, path = window.location.pathname) {
  try {
    const q = new URLSearchParams(search)
    if (APP_BOOT_PARAMS.some((k) => q.has(k))) return true
    if (q.has('demo') && path !== '/try') return true
    if (q.has('ops') && path !== '/try') return true
    if (isAuthRedirectCallback(search) && path !== '/try') return true
  } catch { /* malformed search — fall through to path routing */ }
  return false
}
