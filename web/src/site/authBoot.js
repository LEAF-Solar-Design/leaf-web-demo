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

export function canonicalDemoUrl(search, pathname) {
  return pathname === '/app' && new URLSearchParams(search).get('demo') === '1'
    ? '/try' + search
    : null
}

export function canonicalizeDemoEntry(win = window) {
  const url = canonicalDemoUrl(win.location.search, win.location.pathname)
  if (url === null) return null
  const target = url + win.location.hash
  win.history.replaceState(win.history.state, '', target)
  return target
}

// True while an Auth0 code exchange is still in flight for THIS page load.
// Nothing that talks to /api may render until it resolves.
export function shouldDeferForAuthCallback(search = window.location.search) {
  return isAuthRedirectCallback(search)
}

// BOOT BACK-COMPAT: any of ?fixture= ?dev= ?drawing= (or ?demo=/?ops= off /try)
// boots straight into the console regardless of path. An auth callback also
// boots the console, but only AFTER shouldDeferForAuthCallback clears -- the
// deferral is what keeps the burst behind the token write.
//
// ONE SHELL: the website and landing sample buttons open /try?demo=1,
// which boots the cockpit demo. Legacy /app?demo=1 entries are canonicalized
// at boot. Only the literal '1' counts on /try; bare /try and /try?demo=0
// stay on the stage as before.
// Never throws on a malformed search: the catch falls through to path routing.
export function bootWantsApp(search, path = window.location.pathname) {
  try {
    const q = new URLSearchParams(search)
    if (APP_BOOT_PARAMS.some((k) => q.has(k))) return true
    if (q.has('demo') && path !== '/try') return true
    if (path === '/try' && q.get('demo') === '1') return true
    if (q.has('ops') && path !== '/try') return true
    if (isAuthRedirectCallback(search) && path !== '/try') return true
  } catch { /* malformed search — fall through to path routing */ }
  return false
}
