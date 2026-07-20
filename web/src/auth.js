// Auth0 platform-session sign-in (Concern 1 — the 401 fix).
//
// The SPA client, the API audience, and the backend RS256 verify path are ALL
// already provisioned and live-proven (contract/AUTH.md, server/auth.py,
// docs/auth0-live-login-receipt.json + docs/auth0-live-server-receipt.json).
// This is the one missing leg: a frontend that obtains a token and stores it as
// localStorage['leaf.jwt'] -- which api.js authHeaders() already sends on every
// request. So enabling live sign-in is purely: bake the three VITE_AUTH0_* vars
// into the build AND allow-list the deploy origin (Auth0 callback + backend CORS).
//
// Inert by design when the vars are absent: authConfigured is false, no button
// renders, the SDK is never imported -- the build/deploy behave exactly as today
// (the calm demo gate). Proven end-to-end against the already-allowed
// http://localhost:8080 callback.

const DOMAIN = import.meta.env.VITE_AUTH0_DOMAIN || ''
const CLIENT_ID = import.meta.env.VITE_AUTH0_CLIENT_ID || ''
const AUDIENCE = import.meta.env.VITE_AUTH0_AUDIENCE || ''
const JWT_KEY = 'leaf.jwt'

// True only when all three public SPA config values are baked into the build.
export const authConfigured = !!(DOMAIN && CLIENT_ID && AUDIENCE)

let _clientP = null
async function client() {
  if (!authConfigured) return null
  if (!_clientP) {
    // Dynamic import so a build without the vars never pulls the SDK.
    _clientP = import('@auth0/auth0-spa-js').then(({ createAuth0Client }) =>
      createAuth0Client({
        domain: DOMAIN,
        clientId: CLIENT_ID,
        authorizationParams: { audience: AUDIENCE, redirect_uri: window.location.origin },
        cacheLocation: 'localstorage',
      }))
  }
  return _clientP
}

// Kick the Authorization-Code + PKCE redirect to Auth0 Universal Login.
export async function login() {
  const c = await client()
  if (c) await c.loginWithRedirect()
}

// Call once on mount. If we returned from Auth0 (?code=&state=), finish the code
// exchange, stash the api-audience access token as leaf.jwt, strip the query,
// and return true so the caller can reload into an authenticated session.
export async function handleRedirectCallback() {
  if (!authConfigured) return false
  const q = window.location.search
  if (!/[?&]code=/.test(q) || !/[?&]state=/.test(q)) return false
  const c = await client()
  if (!c) return false
  const clean = () => window.history.replaceState({}, document.title, window.location.pathname)
  try {
    await c.handleRedirectCallback()
    const token = await c.getTokenSilently({ authorizationParams: { audience: AUDIENCE } })
    if (token) localStorage.setItem(JWT_KEY, token)
    clean()
    return !!token
  } catch {
    clean()
    return false
  }
}

export function isSignedIn() {
  try { return !!localStorage.getItem(JWT_KEY) } catch { return false }
}

// Clear the local token and (best-effort) end the Auth0 session, returning to
// the app origin -- which comes back up in the calm signed-out gate.
export async function logout() {
  try { localStorage.removeItem(JWT_KEY) } catch { /* ignore */ }
  const c = await client()
  if (c) {
    try { await c.logout({ logoutParams: { returnTo: window.location.origin } }); return } catch { /* fall through */ }
  }
  window.location.reload()
}
