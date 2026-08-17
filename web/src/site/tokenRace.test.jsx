// Regression: the post-callback token race (2026-08-17 UPDATE 9).
//
// redirect_uri is the bare origin (auth.js), so a fresh login's Auth0
// callback lands on '/' — where the old SiteRoot booted scene 'app' and
// rendered the console IMMEDIATELY, firing its whole API burst BEFORE
// handleRedirectCallback persisted leaf.jwt. Eleven pre-token 401s latched
// the session controller into 'required' with nothing retrying: a
// signed-out-looking /try while actually signed in, until a manual reload
// (the D1a collector's reload workaround existed solely to paper over this).
//
// The contract pinned here: NO authed surface renders on ANY callback
// landing until the token exchange has settled. The mount burst simulation
// is the mocked App/ToolCast — if either mounts while the exchange is still
// in flight, the burst beat the token write and this spec fails (verified to
// fail on the pre-fix SiteRoot).
import { render, screen, act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const seen = vi.hoisted(() => ({
  appMounts: 0,
  toolCastMounts: 0,
  exchange: null, // deferred handleRedirectCallback the test resolves
  tokenStored: false,
}))

vi.mock('../App.jsx', () => ({
  default: function AppMock() {
    seen.appMounts += 1
    return null
  },
}))
vi.mock('./ToolCast.jsx', () => ({
  default: function ToolCastMock() {
    seen.toolCastMounts += 1
    return null
  },
}))
vi.mock('./StageLayer.jsx', async () => {
  const React = await import('react')
  return { default: React.forwardRef(function StageLayerMock(props, ref) { return null }) }
})
vi.mock('./LandingCast.jsx', () => ({ default: () => null }))
vi.mock('./sheets/SheetsPage.jsx', () => ({ default: () => null }))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({
  WorkspaceControllerProvider: ({ children }) => children,
}))
vi.mock('../api.js', () => ({
  getDrawingIntake: vi.fn(async () => ({})),
  getDrawingVersions: vi.fn(async () => ({})),
  redoDrawing: vi.fn(async () => ({})),
  undoDrawing: vi.fn(async () => ({})),
}))
vi.mock('../auth.js', () => ({
  // Same URL shape the real helper reads: state + (code | error).
  isAuthRedirectCallback: (search = window.location.search) => {
    const q = new URLSearchParams(search)
    return q.has('state') && (q.has('code') || q.has('error'))
  },
  isSignedIn: () => seen.tokenStored,
  handleRedirectCallback: () => {
    if (!seen.exchange) {
      let resolve
      const promise = new Promise((r) => { resolve = r })
      seen.exchange = { promise, resolve }
    }
    return seen.exchange.promise
  },
}))

import SiteRoot from './SiteRoot.jsx'

const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

beforeEach(() => {
  seen.appMounts = 0
  seen.toolCastMounts = 0
  seen.exchange = null
  seen.tokenStored = false
})

afterEach(() => {
  cleanup() // globals:false means RTL's auto-cleanup never registers
  window.history.replaceState(null, '', '/')
})

describe('post-callback token race', () => {
  it('holds every authed render on a / callback landing until the token exchange settles', async () => {
    window.history.replaceState(null, '', '/?code=abc&state=xyz')
    render(<SiteRoot />)
    await settle()

    // The exchange is still in flight: the ONLY surface is the sign-in gate.
    // Pre-fix, scene 'app' rendered the console here and its mount burst
    // fired eleven pre-token 401s — appMounts > 0 is the race.
    expect(screen.getByRole('status')).toHaveTextContent('Completing sign in')
    expect(seen.appMounts).toBe(0)
    expect(seen.toolCastMounts).toBe(0)
  })

  it('keeps the /try callback landing gated (existing behavior preserved)', async () => {
    window.history.replaceState(null, '', '/try?code=abc&state=xyz')
    render(<SiteRoot />)
    await settle()

    expect(screen.getByRole('status')).toHaveTextContent('Completing sign in')
    expect(seen.toolCastMounts).toBe(0)
    expect(seen.appMounts).toBe(0)
  })

  it('a failed exchange lowers the gate and proceeds signed-out (no dead-end screen)', async () => {
    window.history.replaceState(null, '', '/?code=abc&state=xyz')
    render(<SiteRoot />)
    await settle()
    expect(screen.getByRole('status')).toHaveTextContent('Completing sign in')

    await act(async () => { seen.exchange.resolve(false) })
    await settle()

    // Gate is down; the boot decision (scene 'app' for a callback landing)
    // proceeds exactly as before the fix — un-authed, calm signed-out path.
    expect(screen.queryByRole('status')).toBeNull()
    expect(seen.appMounts).toBeGreaterThan(0)
  })

  it('a successful exchange keeps the gate up for the reload that commits the authed mount', async () => {
    window.history.replaceState(null, '', '/?code=abc&state=xyz')
    render(<SiteRoot />)
    await settle()

    seen.tokenStored = true // handleRedirectCallback stored leaf.jwt...
    await act(async () => { seen.exchange.resolve(true) }) // ...and reported success
    await settle()

    // The real page reloads here (jsdom cannot). The pinned contract: the
    // authed console still MUST NOT mount in this document — the reload is
    // the only committer, so the burst can never precede the stored token.
    expect(seen.appMounts).toBe(0)
    expect(seen.toolCastMounts).toBe(0)
  })
})
