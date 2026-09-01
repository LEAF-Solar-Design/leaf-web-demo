// The console's half of the session unification (convergence W2b).
//
// Two things are proven here, and neither needs App.jsx to mount:
//   1. the truth table itself (consoleGate.js) — exhaustively, because these
//      four booleans decide whether a signed-out user sees a calm gate, an
//      unrecoverable blank, or a working demo;
//   2. the console's DRIVING SEQUENCE against the REAL controller and the REAL
//      transport guard (api.js noteUnauthorized) — checking() before the load,
//      activate() on a 200, requireAuth() on a 401, and shouldAutoDemo() in the
//      same branch. The defect class this slice removes lived in the seam
//      between the surface's catch and the transport's verdict, so the seam is
//      what gets tested, not either side alone.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { noteUnauthorized, subscribeUnauthorized } from '../../api.js'
import { shouldAutoDemo } from '../../demoState.js'
import { consoleAuthRequired, consoleSignedOut } from './consoleGate.js'
import { MAX_TOKEN_RECOVERIES, createSessionController } from './createSessionController.js'

describe('console session truth table', () => {
  it('maps only `required` to authRequired', () => {
    expect(consoleAuthRequired('required')).toBe(true)
    expect(consoleAuthRequired('checking')).toBe(false)
    expect(consoleAuthRequired('active')).toBe(false)
    // A status this console has never seen is not a refusal.
    expect(consoleAuthRequired(undefined)).toBe(false)
  })

  // Every combination, spelled out: the console's gate is the surface a
  // stranger meets at the front door, and each row here was a shipped decision.
  const rows = [
    // mock always wins — the demo has no platform session to be signed out of.
    { mock: true, authRequired: true, authConfigured: true, signedIn: false, signedOut: false },
    { mock: true, authRequired: true, authConfigured: false, signedIn: false, signedOut: false },
    // no refusal observed -> not signed out, whatever the build looks like.
    { mock: false, authRequired: false, authConfigured: true, signedIn: false, signedOut: false },
    { mock: false, authRequired: false, authConfigured: false, signedIn: false, signedOut: false },
    // configured build: a refusal is always the calm gate, because the user CAN
    // sign in from it.
    { mock: false, authRequired: true, authConfigured: true, signedIn: false, signedOut: true },
    { mock: false, authRequired: true, authConfigured: true, signedIn: true, signedOut: true },
    // unconfigured build, no token: the stranger case the auto-demo rescues.
    { mock: false, authRequired: true, authConfigured: false, signedIn: false, signedOut: true },
    // unconfigured build WITH a token: a rejected real session and no way to
    // re-auth. NOT signedOut on purpose — it must fall through to the pane-fail
    // surface (Retry + Back to the demo), never the inert overlay that round-2
    // review F1 found was an unrecoverable blank.
    { mock: false, authRequired: true, authConfigured: false, signedIn: true, signedOut: false },
  ]
  for (const row of rows) {
    it(`mock=${row.mock} authRequired=${row.authRequired} configured=${row.authConfigured} signedIn=${row.signedIn} -> signedOut=${row.signedOut}`, () => {
      expect(consoleSignedOut({
        mock: row.mock,
        authRequired: row.authRequired,
        authConfigured: row.authConfigured,
        isSignedIn: () => row.signedIn,
      })).toBe(row.signedOut)
    })
  }

  it('never reads the token on a healthy render', () => {
    let reads = 0
    const isSignedIn = () => { reads += 1; return false }
    consoleSignedOut({ mock: false, authRequired: false, authConfigured: true, isSignedIn })
    consoleSignedOut({ mock: true, authRequired: true, authConfigured: false, isSignedIn })
    expect(reads).toBe(0)
  })

  it('short-circuits the token read on a configured build', () => {
    let reads = 0
    const isSignedIn = () => { reads += 1; return false }
    expect(consoleSignedOut({ mock: false, authRequired: true, authConfigured: true, isSignedIn })).toBe(true)
    expect(reads).toBe(0)
  })
})

// The console's session effect, replayed call-for-call against the real
// controller. `run` IS App's `.then`/`.catch` pair; nothing here re-implements
// the truth table (it imports it) or the auto-demo rule (it imports that too).
function consoleHarness({ authConfigured = false } = {}) {
  const controller = createSessionController({
    storage: localStorage,
    subscribeUnauthorized,
    // The controller's default endSession is auth.js logout(), which navigates.
    endSession: async () => { state.endedSessions += 1 },
  })
  controller.start()
  const state = { mock: false, endedSessions: 0 }
  const view = () => {
    const authRequired = consoleAuthRequired(controller.getSnapshot().status)
    return {
      status: controller.getSnapshot().status,
      authRequired,
      signedOut: consoleSignedOut({
        mock: state.mock,
        authRequired,
        authConfigured,
        isSignedIn: () => !!localStorage.getItem('leaf.jwt'),
      }),
    }
  }
  return {
    controller,
    state,
    view,
    // App.jsx's load effect, minus the drawing seating.
    load(result) {
      if (!state.mock) controller.actions.checking()
      if (result.ok) {
        if (!state.mock) controller.actions.activate(result.session || {})
        return
      }
      // The transport sees the 401 first and decides whether the token is
      // indicted; the surface's catch runs after, exactly as api.js orders it.
      noteUnauthorized({ status: 401 }, '/api/session', result.sentAuth)
      if (!state.mock) {
        controller.actions.requireAuth('/api/session')
        if (shouldAutoDemo({
          authRequired: true,
          authConfigured,
          mock: state.mock,
          signedIn: !!localStorage.getItem('leaf.jwt'),
        })) state.mock = true
      }
    },
  }
}

describe('console session adoption', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { localStorage.clear() })

  it('auto-falls back to the demo when a 401 lands on an unconfigured build', () => {
    const console_ = consoleHarness({ authConfigured: false })
    console_.load({ ok: false })

    // The documented escape hatch: the deployed VITE_MOCK=0 link cannot sign
    // in, so it lands zero-click on the demo instead of parking on the gate.
    expect(console_.state.mock).toBe(true)
    expect(console_.view().signedOut).toBe(false)
    console_.controller.destroy()
  })

  it('keeps the calm gate instead of the demo when the user CAN sign in', () => {
    const console_ = consoleHarness({ authConfigured: true })
    console_.load({ ok: false })

    expect(console_.state.mock).toBe(false)
    expect(console_.view()).toMatchObject({ authRequired: true, signedOut: true })
    console_.controller.destroy()
  })

  it('latches authRequired on a 401 that carried the stored token', () => {
    localStorage.setItem('leaf.jwt', 'live-token')
    const console_ = consoleHarness({ authConfigured: true })
    console_.load({ ok: false, sentAuth: 'Bearer live-token' })

    // The transport indicted and wiped the token; the controller latched on it,
    // so the surface's own catch cannot spend a recovery retrying it.
    expect(localStorage.getItem('leaf.jwt')).toBeNull()
    expect(console_.controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'expired', recoveries: 0 })
    expect(console_.view().signedOut).toBe(true)
    console_.controller.destroy()
  })

  it('passes a signed-in session straight through to active with no gate', () => {
    localStorage.setItem('leaf.jwt', 'live-token')
    const console_ = consoleHarness({ authConfigured: true })
    console_.load({ ok: true, session: { tenant: 'cat-litmus-tenant', tier: 'hosted_starter', org: 'org-1' } })

    expect(console_.view()).toMatchObject({ status: 'active', authRequired: false, signedOut: false })
    expect(console_.controller.getSnapshot().session).toMatchObject({ tenant: 'cat-litmus-tenant' })
    console_.controller.destroy()
  })

  it('recovers the post-callback race the hand-rolled latch could not', () => {
    const console_ = consoleHarness({ authConfigured: true })
    // The mount burst goes out before the code exchange stores leaf.jwt, so the
    // 401 carries no bearer: the transport keeps quiet and the surface latches.
    console_.load({ ok: false })
    expect(console_.view().signedOut).toBe(true)

    // 700ms later the token lands. The old boolean had nothing that re-ran the
    // load; the controller re-opens `checking` exactly once for this token.
    localStorage.setItem('leaf.jwt', 'fresh-token')
    expect(console_.controller.actions.recoverWithStoredToken()).toBe(true)
    expect(console_.view().signedOut).toBe(false)

    console_.load({ ok: true, session: { tenant: 'cat-litmus-tenant' } })
    expect(console_.view()).toMatchObject({ status: 'active', signedOut: false })
    console_.controller.destroy()
  })

  it('bounds the recovery so a permanently bad token cannot spin', () => {
    const console_ = consoleHarness({ authConfigured: true })
    console_.load({ ok: false })
    for (let attempt = 1; attempt <= MAX_TOKEN_RECOVERIES + 2; attempt += 1) {
      localStorage.setItem('leaf.jwt', `token-${attempt}`)
      console_.controller.actions.recoverWithStoredToken()
      console_.load({ ok: false })
    }
    expect(console_.controller.getSnapshot().recoveries).toBe(MAX_TOKEN_RECOVERIES)
    expect(console_.view().signedOut).toBe(true)
    console_.controller.destroy()
  })

  it('a jobs 401 latches the gate and a later jobs 200 cannot silently clear it', () => {
    localStorage.setItem('leaf.jwt', 'live-token')
    const console_ = consoleHarness({ authConfigured: true })
    console_.load({ ok: true, session: { tenant: 'cat-litmus-tenant' } })
    expect(console_.view().signedOut).toBe(false)

    // useJobController publishes a two-way boolean; the console now forwards the
    // RISING edge only. Feeding the falling edge back in was the last-writer-
    // wins hazard: a jobs 200 racing a session 401 dismissed a gate over a
    // session that never loaded.
    noteUnauthorized({ status: 401 }, '/api/jobs', 'Bearer live-token')
    console_.controller.actions.requireAuth('jobs')
    expect(console_.view().signedOut).toBe(true)

    // The falling edge is simply not wired; only a re-verified /api/session 200
    // (or the bounded recovery) leaves `required`.
    expect(console_.controller.actions.checking()).toMatchObject({ status: 'required' })
    expect(console_.view().signedOut).toBe(true)
    console_.controller.destroy()
  })

  it('an explicit sign-out is a reason the recovery ladder refuses to re-open', async () => {
    localStorage.setItem('leaf.jwt', 'live-token')
    const console_ = consoleHarness({ authConfigured: true })
    console_.load({ ok: true, session: { tenant: 'cat-litmus-tenant' } })

    await console_.controller.actions.signOut()
    expect(console_.state.endedSessions).toBe(1)
    expect(console_.controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'signed_out' })
    expect(console_.view().signedOut).toBe(true)

    localStorage.setItem('leaf.jwt', 'a-different-token')
    expect(console_.controller.actions.recoverWithStoredToken()).toBe(false)
    expect(console_.view().signedOut).toBe(true)
    console_.controller.destroy()
  })
})
