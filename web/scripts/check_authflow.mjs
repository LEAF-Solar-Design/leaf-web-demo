import {
  AUTH_CALLBACK_ERROR,
  AUTH_START_ERROR,
  beginLogin,
  finishLoginRedirect,
} from '../src/authFlow.js'

function assert(condition, message) {
  if (!condition) { console.error('FAIL:', message); process.exit(1) }
}

const startFailure = await beginLogin(async () => { throw new Error('network detail') })
assert(startFailure.started === false, 'failed login initiation is not marked started')
assert(startFailure.error === AUTH_START_ERROR, 'login failure uses safe actionable copy')
assert(!startFailure.error.includes('network detail'), 'SDK details are not exposed')

let started = false
await beginLogin(async () => ({ loginWithRedirect: async () => { started = true } }))
assert(started, 'successful initiation invokes the SDK redirect')

let cleaned = 0
const callbackFailure = await finishLoginRedirect({
  configured: true,
  search: '?code=x&state=y',
  getClient: async () => ({ handleRedirectCallback: async () => { throw new Error('PKCE secret') } }),
  audience: 'audience',
  storeToken: () => {},
  cleanUrl: () => { cleaned += 1 },
})
assert(callbackFailure.handled && !callbackFailure.signedIn, 'callback failure is handled but signed out')
assert(callbackFailure.error === AUTH_CALLBACK_ERROR, 'callback failure returns actionable copy')
assert(!callbackFailure.error.includes('PKCE secret'), 'callback internals are not exposed')
assert(cleaned === 1, 'failed callback query is cleaned exactly once')

let stored = null
const callbackSuccess = await finishLoginRedirect({
  configured: true,
  search: '?code=x&state=y',
  getClient: async () => ({
    handleRedirectCallback: async () => {},
    getTokenSilently: async () => 'access-token',
  }),
  audience: 'audience',
  storeToken: (token) => { stored = token },
  cleanUrl: () => { cleaned += 1 },
})
assert(callbackSuccess.signedIn && !callbackSuccess.error, 'successful callback is signed in')
assert(stored === 'access-token', 'successful callback stores the access token')
assert(cleaned === 2, 'successful callback query is cleaned exactly once')

const untouched = await finishLoginRedirect({
  configured: true,
  search: '',
  getClient: async () => { throw new Error('must not run') },
  audience: 'audience',
  storeToken: () => {},
  cleanUrl: () => { cleaned += 1 },
})
assert(!untouched.handled && cleaned === 2, 'non-callback navigation is untouched')

console.log('AUTH_FLOW_OK')
