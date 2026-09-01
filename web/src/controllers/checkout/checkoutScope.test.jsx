/**
 * The W2c additions to the SHARED single-writer checkout controller, driven
 * through the shipped hook rather than asserted against source:
 *
 *   * the scope-reset contract (docs/convergence/ACCEPTANCE.md, binding) for a
 *     TENANT switch — a lock held under tenant A must never gate or authorize
 *     writes under tenant B;
 *   * the duplicate-tab claim protocol the console hand-rolled until now: the
 *     deferral while a reload handoff is unredeemed, the incumbent claim after
 *     it is redeemed, and the remint after it is refused.
 *
 * Both shells consume exactly this code, so a regression here is a regression
 * on /app and /try at once — which is the whole point of retiring the twin.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  HOLDER_STORAGE_KEY,
  stageCheckoutReloadHandoff,
} from '../../checkoutIdentity.js'
import { checkoutScopeDrawingId } from './createCheckoutController.js'
import useCheckoutController from './useCheckoutController.js'

// --- seams -----------------------------------------------------------------

function installReloadNavigation() {
  const original = performance.getEntriesByType
  performance.getEntriesByType = (type) => (type === 'navigation'
    ? [{ type: 'reload' }]
    : original.call(performance, type))
  return () => { performance.getEntriesByType = original }
}

// A Web Locks stand-in. jsdom has no Web Locks, and `holdCheckoutReloadAuthority`
// FAILS CLOSED without them (`active: false`) — which is also the shape of a
// refused redemption, so `installLocks` is simply not called for that case.
function installLocks({ grantAfterMs = 0 } = {}) {
  const held = new Map()
  const locks = {
    async request(name, _options, callback) {
      if (held.has(name)) {
        // Never resolves: a queued duplicate waits for the incumbent. The
        // controller must not treat a queued request as authority.
        await new Promise(() => {})
        return undefined
      }
      held.set(name, true)
      // A real Web Lock is granted asynchronously. The delay is what makes the
      // claim DEFERRAL observable: without it the redemption resolves before
      // an undeferred mount claim could reach the channel, and the pin below
      // would pass for the wrong reason.
      if (grantAfterMs) await new Promise((resolve) => { setTimeout(resolve, grantAfterMs) })
      return callback({ name })
    },
  }
  Object.defineProperty(navigator, 'locks', { value: locks, configurable: true })
  return () => { delete navigator.locks }
}

function makeServices({ checkout = null, capability = 'opaque-proof' } = {}) {
  const state = { checkout }
  return {
    state,
    loadVersions: vi.fn(async () => ({ checkout: state.checkout })),
    take: vi.fn(async (_drawingId, holder) => {
      state.checkout = { holder }
      return { acquired: true, checkout_capability: capability }
    }),
    release: vi.fn(async () => { state.checkout = null; return { released: true } }),
  }
}

let restoreLocks = () => {}
let restoreNavigation = () => {}

beforeEach(() => {
  sessionStorage.clear()
})

afterEach(() => {
  restoreLocks()
  restoreNavigation()
  restoreLocks = () => {}
  restoreNavigation = () => {}
})

// --- the scope gate, as data ----------------------------------------------

describe('checkoutScopeDrawingId', () => {
  it('keeps each shell on the exact drawing it addressed before', () => {
    // The console: the version chain wins, the booted identity is the fallback.
    expect(checkoutScopeDrawingId({
      identityDrawingId: 'demo',
      drawingState: { drawing_id: 'demo' },
      requestedDrawingId: 'demo',
    })).toBe('demo')
    expect(checkoutScopeDrawingId({
      identityDrawingId: 'demo',
      drawingState: null,
      requestedDrawingId: 'demo',
    })).toBe('demo')
    // The stage: the seated drawing only, no fallback — its own shape.
    expect(checkoutScopeDrawingId({
      identityDrawingId: 'cat-panels',
      drawingState: { drawing_id: 'cat-panels' },
    })).toBe('cat-panels')
    expect(checkoutScopeDrawingId({
      identityDrawingId: 'cat-panels',
      drawingState: null,
    })).toBeNull()
  })

  it('voids the scope when the tenant switch voided the drawing identity', () => {
    // The version chain does NOT reset with the identity, so this is the case
    // that used to keep addressing the previous tenant's drawing.
    expect(checkoutScopeDrawingId({
      identityDrawingId: null,
      drawingState: { drawing_id: 'tenant-a-drawing' },
      requestedDrawingId: 'tenant-a-drawing',
    })).toBeNull()
  })
})

// --- the tenant switch, through the shipped hook ---------------------------

describe('a checkout held under tenant A', () => {
  it('is abandoned, not released, and gates nothing under tenant B', async () => {
    restoreLocks = installLocks()
    const services = makeServices()
    const { result, rerender } = renderHook(
      ({ drawingId }) => useCheckoutController({
        drawingId, holder: 'sess-tenant-a', services,
      }),
      { initialProps: { drawingId: 'tenant-a-drawing' } },
    )

    await act(async () => { await result.current.actions.take() })
    await waitFor(() => expect(result.current.heldByUs).toBe(true))
    expect(result.current.writeLocked).toBe(false)
    expect(result.current.actions.getCapability()).toBe('opaque-proof')
    const releasesBefore = services.release.mock.calls.length

    // The tenant switch: DrawingIdentityProvider.resetAll voids every mode's
    // identity, so the scope goes null while the version chain still names
    // tenant A's drawing.
    await act(async () => { rerender({ drawingId: null }) })

    // AUTHORIZE: the bearer proof is gone, so no write can carry it.
    expect(result.current.actions.getCapability()).toBeNull()
    // GATE: tenant A's lock record no longer decides anything here.
    expect(result.current.checkout).toBeNull()
    expect(result.current.lockedByOther).toBeNull()
    expect(result.current.heldByUs).toBe(false)
    // ABANDON, not release: the capability belongs to the previous principal,
    // so this client never spends the new principal's credentials on a DELETE.
    // The server's lease cap is what frees it.
    expect(services.release.mock.calls.length).toBe(releasesBefore)
    // And the lease is still there for whoever holds tenant A's session.
    expect(services.state.checkout).toEqual({ holder: 'sess-tenant-a' })
  })
})

// --- the duplicate-tab claim protocol --------------------------------------

describe('the holder claim', () => {
  it('is deferred while a reload handoff is unredeemed, then claims as the incumbent', async () => {
    restoreNavigation = installReloadNavigation()
    restoreLocks = installLocks({ grantAfterMs: 40 })
    const holder = 'sess-reload-incumbent'
    sessionStorage.setItem(HOLDER_STORAGE_KEY, holder)
    stageCheckoutReloadHandoff({
      capability: 'staged-proof', holder, drawingId: 'reload-drawing',
    })

    // A live peer already sharing this id — a duplicated tab that raced the
    // reload. It answers `held` with a claim OLDER than any wall-clock claim,
    // so a runtime that claimed normally would step aside and lose the very
    // holder id its staged capability is keyed to. The incumbent must not.
    const claimsSeen = []
    const peer = new BroadcastChannel('leaf.checkout_holder_claim')
    peer.onmessage = (event) => {
      if (event.data?.type !== 'claim' || event.data.id !== holder) return
      claimsSeen.push(event.data)
      peer.postMessage({ type: 'held', id: holder, claimedAt: 1, nonce: 'aaa' })
    }

    const reminted = []
    const services = makeServices()
    const { result } = renderHook(() => useCheckoutController({
      drawingId: 'reload-drawing',
      bootDrawingId: 'reload-drawing',
      holder,
      services,
      onHolderRemint: (next) => reminted.push(next),
    }))

    // The handoff is redeemed inside the exclusive lock, and only then does the
    // capability appear.
    await waitFor(() => expect(result.current.actions.getCapability()).toBe('staged-proof'))
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 80)) })
    peer.close()

    // Not vacuous: the incumbent DID announce itself, and the peer DID answer.
    expect(claimsSeen).toHaveLength(1)
    expect(claimsSeen[0].claimedAt).toBe(0)
    expect(reminted).toEqual([])
    expect(sessionStorage.getItem(HOLDER_STORAGE_KEY)).toBe(holder)
  })

  it('remints and re-announces when the redemption is refused', async () => {
    restoreNavigation = installReloadNavigation()
    // No Web Locks: the authority fails closed, which is exactly the state a
    // runtime that cannot prove exclusive ownership must land in.
    const holder = 'sess-refused-redemption'
    sessionStorage.setItem(HOLDER_STORAGE_KEY, holder)
    stageCheckoutReloadHandoff({
      capability: 'refused-proof', holder, drawingId: 'refused-drawing',
    })

    const reminted = []
    const services = makeServices()
    const { result } = renderHook(() => useCheckoutController({
      drawingId: 'refused-drawing',
      bootDrawingId: 'refused-drawing',
      holder,
      services,
      onHolderRemint: (next) => reminted.push(next),
    }))

    // A refused redemption means the stored id may be a CLONE's: a reload and a
    // duplication present identically, so the only safe reading is that we are
    // the duplicate. Mint a new id and hand back the lease we could not prove.
    await waitFor(() => expect(reminted).toHaveLength(1))
    expect(reminted[0]).not.toBe(holder)
    expect(sessionStorage.getItem(HOLDER_STORAGE_KEY)).toBe(reminted[0])
    expect(result.current.actions.getCapability()).toBeNull()
    await waitFor(() => {
      expect(services.release).toHaveBeenCalledWith('refused-drawing', 'refused-proof')
    })
  })
})
