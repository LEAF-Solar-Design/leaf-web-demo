// noteUnauthorized: a 401 wipes the stored platform token ONLY when the failing
// request actually carried the currently-stored token.
//
// The defect this pins (found by the 2026-08-08 production synthetic walk):
// after Auth0 login, handleRedirectCallback stores leaf.jwt and reloads, but
// the guest-phase bootstrap requests fired BEFORE the token existed resolve
// 401 in that same window, and the old unconditional wipe cleared the fresh
// token every time -- every signed-in user landed back in the guest workspace.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { noteUnauthorized, subscribeUnauthorized } from './api.js'

const AUTH_KEY = 'leaf.jwt'
const res401 = { status: 401 }

describe('noteUnauthorized token-wipe guard', () => {
  let notified
  let unsubscribe

  beforeEach(() => {
    localStorage.clear()
    notified = []
    unsubscribe = subscribeUnauthorized((source) => notified.push(source))
  })

  afterEach(() => {
    unsubscribe()
    localStorage.clear()
  })

  it('post-login race: a guest-phase 401 (no bearer sent) must not wipe a freshly stored token', () => {
    // Request went out with NO Authorization (token did not exist yet)...
    // ...then the callback stored the fresh token...
    localStorage.setItem(AUTH_KEY, 'fresh-token')
    // ...and only now does the straggler 401 response arrive.
    const out = noteUnauthorized(res401, '/api/session', undefined)
    expect(out).toBe(res401)
    expect(localStorage.getItem(AUTH_KEY)).toBe('fresh-token')
    expect(notified).toEqual([])
  })

  it('a 401 from a request that carried an OLDER token must not wipe the current one', () => {
    localStorage.setItem(AUTH_KEY, 'new-token')
    noteUnauthorized(res401, '/api/jobs', 'Bearer old-token')
    expect(localStorage.getItem(AUTH_KEY)).toBe('new-token')
    expect(notified).toEqual([])
  })

  it('a 401 from a request that carried the CURRENT token wipes it and notifies', () => {
    localStorage.setItem(AUTH_KEY, 'live-token')
    noteUnauthorized(res401, '/api/entitlements', 'Bearer live-token')
    expect(localStorage.getItem(AUTH_KEY)).toBeNull()
    expect(notified).toEqual(['/api/entitlements'])
  })

  it('a 401 with a bearer but no stored token neither throws nor notifies', () => {
    noteUnauthorized(res401, '/api/run', 'Bearer orphan-token')
    expect(localStorage.getItem(AUTH_KEY)).toBeNull()
    expect(notified).toEqual([])
  })

  it('a non-401 response is passed through untouched', () => {
    localStorage.setItem(AUTH_KEY, 'live-token')
    const ok = { status: 200 }
    expect(noteUnauthorized(ok, '/api/session', 'Bearer live-token')).toBe(ok)
    expect(localStorage.getItem(AUTH_KEY)).toBe('live-token')
    expect(notified).toEqual([])
  })

  it('a malformed sent-auth value (not a Bearer scheme) never wipes', () => {
    localStorage.setItem(AUTH_KEY, 'live-token')
    noteUnauthorized(res401, '/api/session', 'Basic abc123')
    expect(localStorage.getItem(AUTH_KEY)).toBe('live-token')
    expect(notified).toEqual([])
  })
})
