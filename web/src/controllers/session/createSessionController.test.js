// Regression cover for the 2026-08-17 post-callback token race (D1a defect #6).
//
// The shape of the bug, from the ALB timeline: the SPA's post-callback mount
// fired its whole API burst at 18:49:56.29Z BEFORE leaf.jwt landed in
// localStorage. Eleven 401s -- including its own getSession -- latched the
// session controller into `required`. 700ms later the token was present and
// /api/session returned 200 in 0.4s, but `checking()` refuses to leave
// `required` and nothing re-ran getSession, so the page held a valid token
// behind a signed-out-looking surface, Trust/Jobs tabs disabled, until the user
// manually reloaded.

import { describe, expect, it, vi } from 'vitest'

import { MAX_TOKEN_RECOVERIES, createSessionController } from './createSessionController.js'

function fakeStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
}

// The transport seam, wired the way the app wires it: api.js publishes a token
// arrival, auth.js publishes the store, and nothing polls.
function harness({ storage = fakeStorage() } = {}) {
  const tokenListeners = new Set()
  const unauthorizedListeners = new Set()
  const controller = createSessionController({
    storage,
    subscribeUnauthorized: (listener) => {
      unauthorizedListeners.add(listener)
      return () => unauthorizedListeners.delete(listener)
    },
    subscribeTokenStored: (listener) => {
      tokenListeners.add(listener)
      return () => tokenListeners.delete(listener)
    },
  })
  controller.start()
  return {
    controller,
    storage,
    unauthorized: (source) => { for (const listener of unauthorizedListeners) listener(source) },
    // Exactly what auth.js storeToken() does: write, then notify. Same tab, so
    // no `storage` event ever fires -- this notification is the only edge.
    storeToken: (token) => {
      storage.setItem('leaf.jwt', token)
      for (const listener of tokenListeners) listener(token)
    },
  }
}

describe('post-callback token race', () => {
  it('recovers the latched session when the token lands after the 401 burst', () => {
    const { controller, storeToken, unauthorized } = harness()

    // The mount burst: every call goes out with no bearer and comes back 401.
    for (const source of [
      '/api/session', '/api/tools', '/api/capabilities', '/api/jobs', '/api/usage',
      '/api/entitlements', '/api/health', '/api/claude', '/api/projects', '/api/versions',
      '/api/catalog',
    ]) unauthorized(source)
    expect(controller.getSnapshot().status).toBe('required')

    // 700ms later handleRedirectCallback stores the token it just exchanged.
    storeToken('fresh-token')

    // Pre-fix this stayed 'required' for the life of the page load.
    expect(controller.getSnapshot()).toMatchObject({
      status: 'checking', reason: null, sources: [], recoveries: 1,
    })
  })

  it('closes the loop: the retried getSession activates the session', async () => {
    const { controller, storeToken } = harness()
    // Stand in for ToolCast's session effect, which re-runs on `recoveries`.
    const getSession = vi.fn(async () => {
      if (!controller.getSnapshot().recoveries) {
        const error = new Error('GET /api/session -> 401')
        error.status = 401
        throw error
      }
      return { tenant: 'tenant-a', tier: 'pro' }
    })
    const runSessionEffect = async () => {
      try {
        controller.actions.activate(await getSession())
      } catch (cause) {
        if (cause.status === 401) controller.actions.requireAuth('/api/session')
      }
    }

    await runSessionEffect()
    expect(controller.getSnapshot().status).toBe('required')

    storeToken('fresh-token')
    expect(controller.getSnapshot().status).toBe('checking')
    await runSessionEffect() // the `recoveries` bump re-ran the effect

    expect(controller.getSnapshot()).toMatchObject({ status: 'active', session: { tenant: 'tenant-a' } })
    expect(getSession).toHaveBeenCalledTimes(2)
  })

  it('is bounded: a token that keeps failing stops re-opening the gate', () => {
    const { controller, storeToken, unauthorized } = harness()
    for (let attempt = 1; attempt <= MAX_TOKEN_RECOVERIES + 3; attempt += 1) {
      unauthorized('/api/session')
      storeToken(`token-${attempt}`)
    }
    expect(controller.getSnapshot().recoveries).toBe(MAX_TOKEN_RECOVERIES)
    expect(controller.getSnapshot().status).toBe('required')
  })

  it('is edge-triggered: re-notifying the same token never re-opens the gate', () => {
    const { controller, storeToken, unauthorized } = harness()
    unauthorized('/api/session')
    storeToken('fresh-token')
    expect(controller.getSnapshot().recoveries).toBe(1)

    unauthorized('/api/session')
    storeToken('fresh-token')
    storeToken('fresh-token')
    expect(controller.getSnapshot()).toMatchObject({ status: 'required', recoveries: 1 })
  })

  it('never re-opens an explicit sign-out', async () => {
    const { controller, storeToken } = harness()
    await controller.actions.signOut()
    expect(controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'signed_out' })

    storeToken('a-token-from-another-tab')
    expect(controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'signed_out', recoveries: 0 })
  })

  it('keeps the expiry contract: checking() alone still cannot leave required', () => {
    const { controller, unauthorized } = harness({ storage: fakeStorage({ 'leaf.jwt': 'expired' }) })
    controller.actions.activate({ tenant: 'tenant-a' })
    unauthorized('/api/jobs')
    expect(controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'expired' })

    controller.actions.checking()
    expect(controller.getSnapshot().status).toBe('required')
    // The refusal cleared the bad token, so there is nothing to recover with.
    expect(controller.actions.recoverWithStoredToken()).toBe(false)
    controller.destroy()
  })

  it('stops listening after destroy', () => {
    const { controller, storeToken, unauthorized } = harness()
    unauthorized('/api/session')
    controller.destroy()
    storeToken('fresh-token')
    expect(controller.getSnapshot()).toMatchObject({ status: 'required', recoveries: 0 })
  })
})
