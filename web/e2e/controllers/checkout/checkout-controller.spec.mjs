import { expect, test } from '@playwright/test'
import {
  createCheckoutController,
  deriveCheckout,
  resolveCheckoutDrawingId,
  storeDrawingIdForSource,
} from '../../../src/controllers/checkout/createCheckoutController.js'
import { secureTakenCheckoutAuthority } from '../../../src/controllers/checkout/useCheckoutController.js'

test('resolves checkout reads against the active drawing identity', () => {
  expect(storeDrawingIdForSource('rooftop_demo')).toBe('demo')
  expect(storeDrawingIdForSource('uploaded-drawing')).toBe('uploaded-drawing')
  expect(resolveCheckoutDrawingId({
    drawingState: { drawing_id: 'versioned-drawing' },
    requestedDrawingId: 'requested-drawing',
  })).toBe('versioned-drawing')
  expect(resolveCheckoutDrawingId({
    drawingState: null,
    requestedDrawingId: 'requested-drawing',
  })).toBe('requested-drawing')
})

test('derives free, owned, other, unknown, and clock-skew-safe checkout states', () => {
  expect(deriveCheckout(null, 'ours')).toMatchObject({ heldByUs: false, writeLocked: false })
  expect(deriveCheckout({ holder: 'ours' }, 'ours')).toMatchObject({
    heldByUs: false,
    writeLocked: true,
    canTake: true,
  })
  expect(deriveCheckout({ holder: 'ours' }, 'ours', Date.now(), false, false, true))
    .toMatchObject({ heldByUs: true, writeLocked: false })
  expect(deriveCheckout({ holder: 'other' }, 'ours')).toMatchObject({ heldByUs: false, writeLocked: true })
  expect(deriveCheckout({ holder: 'other', expires: '2026-01-01T00:00:00Z' }, 'ours', Date.parse('2026-07-24T00:00:00Z')))
    .toMatchObject({ checkout: { holder: 'other' }, writeLocked: true, staleByOther: true, canTake: true })
  expect(deriveCheckout(null, 'ours', Date.now(), true))
    .toMatchObject({ unknown: true, writeLocked: true })
})

test('mock mode performs no checkout calls', async () => {
  let calls = 0
  const controller = createCheckoutController({
    mock: true, drawingId: 'drawing', holder: 'ours',
    services: { loadVersions: async () => { calls += 1 }, take: async () => { calls += 1 }, release: async () => { calls += 1 } },
  })
  await controller.refresh(); await controller.take(); await controller.release()
  expect(calls).toBe(0)
})

test('mutation is single flight and refreshes authoritative checkout', async () => {
  let takeCalls = 0
  let checkout = null
  let releaseTake
  const gate = new Promise((resolve) => { releaseTake = resolve })
  const controller = createCheckoutController({
    drawingId: 'drawing', holder: 'ours',
    services: {
      loadVersions: async () => ({ checkout }),
      take: async () => {
        takeCalls += 1
        await gate
        checkout = { holder: 'ours' }
        return { acquired: true, checkout_capability: 'opaque-proof' }
      },
      release: async () => null,
    },
  })
  const first = controller.take()
  const second = controller.take()
  releaseTake()
  await Promise.all([first, second])
  expect(takeCalls).toBe(1)
  expect(controller.getSnapshot()).toMatchObject({ heldByUs: true, busy: false })
  expect(controller.getCapability()).toBe('opaque-proof')
})

test('a stale drawing refresh cannot replace the current drawing checkout', async () => {
  let releaseOld
  const oldRead = new Promise((resolve) => { releaseOld = resolve })
  const controller = createCheckoutController({
    drawingId: 'old', holder: 'ours',
    services: {
      loadVersions: (id) => id === 'old' ? oldRead : Promise.resolve({ checkout: { holder: 'new-holder' } }),
      take: async () => null, release: async () => null,
    },
  })
  const stale = controller.refresh()
  controller.setScope({ drawingId: 'new', holder: 'ours', mock: false })
  await controller.refresh()
  releaseOld({ checkout: { holder: 'old-holder' } })
  await stale
  expect(controller.getSnapshot()).toMatchObject({ drawingId: 'new', checkout: { holder: 'new-holder' } })
})

test('a failed checkout read pauses writes and exposes a retryable read failure', async () => {
  const controller = createCheckoutController({
    drawingId: 'drawing', holder: 'ours',
    services: {
      loadVersions: async () => { throw new Error('unavailable') },
      take: async () => null,
      release: async () => null,
    },
  })
  await controller.refresh()
  expect(controller.getSnapshot()).toMatchObject({
    checkout: null,
    unknown: true,
    readFailed: true,
    writeLocked: true,
  })
})

test('the opaque capability is used for refresh and release but never enters snapshots', async () => {
  let checkout = null
  const calls = []
  const controller = createCheckoutController({
    drawingId: 'drawing', holder: 'ours',
    services: {
      loadVersions: async () => ({ checkout }),
      take: async (_drawing, _holder, capability) => {
        calls.push(['take', capability])
        checkout = { holder: 'ours' }
        return { acquired: true, checkout_capability: 'opaque-proof' }
      },
      release: async (_drawing, capability) => {
        calls.push(['release', capability])
        checkout = null
        return { released: true }
      },
    },
  })
  await controller.take()
  await controller.take()
  await controller.release()
  expect(calls).toEqual([
    ['take', null],
    ['take', 'opaque-proof'],
    ['release', 'opaque-proof'],
  ])
  expect(controller.getCapability()).toBeNull()
  expect(JSON.stringify(controller.getSnapshot())).not.toContain('opaque-proof')
})

test('an Auth0 return can restore exact authority without exposing it in state', () => {
  const controller = createCheckoutController({
    drawingId: 'drawing', holder: 'ours',
    services: {
      loadVersions: async () => ({ checkout: { holder: 'ours' } }),
      take: async () => null,
      release: async () => null,
    },
  })
  expect(controller.restoreCapability('opaque-auth-return')).toBe(true)
  expect(controller.getCapability()).toBe('opaque-auth-return')
  expect(JSON.stringify(controller.getSnapshot())).not.toContain('opaque-auth-return')
  controller.clearCapability()
  expect(controller.getCapability()).toBeNull()
  controller.setScope({ drawingId: null, holder: 'ours', mock: false })
  expect(controller.restoreCapability('must-not-install')).toBe(false)
})

test('a post-take Web Lock failure clears and releases authority', async () => {
  for (const { active, releaseSucceeds } of [
    { active: false, releaseSucceeds: true },
    { active: true, releaseSucceeds: true },
    { active: true, releaseSucceeds: false },
  ]) {
    let checkout = null
    const released = []
    const services = {
      loadVersions: async () => ({ checkout }),
      take: async () => {
        checkout = { holder: 'ours' }
        return { acquired: true, checkout_capability: `proof-${active}-${releaseSucceeds}` }
      },
      release: async (_drawingId, capability) => {
        released.push(capability)
        if (!releaseSucceeds) throw new Error('release unavailable')
        checkout = null
        return { released: true }
      },
    }
    const controller = createCheckoutController({ drawingId: 'drawing', holder: 'ours', services })
    const snapshots = []
    const unsubscribe = controller.subscribe(() => snapshots.push(controller.getSnapshot()))
    const result = await controller.takeDeferred()
    expect(controller.getCapability()).toBeNull()
    expect(snapshots.some((snapshot) => snapshot.heldByUs && !snapshot.writeLocked)).toBe(false)
    const authority = await secureTakenCheckoutAuthority({
      controller, result, drawingId: 'drawing', holder: 'ours', services,
      holdAuthority: () => ({
        active,
        acquired: Promise.resolve(false),
        stop() {},
      }),
    })
    expect(authority).toBeNull()
    expect(released).toEqual([`proof-${active}-${releaseSucceeds}`])
    expect(controller.getCapability()).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ heldByUs: false, writeLocked: !releaseSucceeds })
    expect(snapshots.some((snapshot) => snapshot.heldByUs && !snapshot.writeLocked)).toBe(false)
    unsubscribe()
  }
})

test('a deferred take publishes authority only from the Web Lock callback', async () => {
  let checkout = null
  const services = {
    loadVersions: async () => ({ checkout }),
    take: async () => {
      checkout = { holder: 'ours' }
      return { acquired: true, checkout_capability: 'deferred-proof' }
    },
    release: async () => null,
  }
  const controller = createCheckoutController({ drawingId: 'drawing', holder: 'ours', services })
  const snapshots = []
  controller.subscribe(() => snapshots.push(controller.getSnapshot()))
  const result = await controller.takeDeferred()
  expect(controller.getCapability()).toBeNull()
  expect(snapshots.some((snapshot) => snapshot.heldByUs && !snapshot.writeLocked)).toBe(false)

  const authority = await secureTakenCheckoutAuthority({
    controller, result, drawingId: 'drawing', holder: 'ours', services,
    holdAuthority: ({ handoff, onAcquired }) => {
      onAcquired(handoff)
      return { active: true, acquired: Promise.resolve(true), stop() {} }
    },
  })
  expect(authority).not.toBeNull()
  expect(controller.getCapability()).toBe('deferred-proof')
  expect(controller.getSnapshot()).toMatchObject({ heldByUs: true, writeLocked: false })
  expect(snapshots.filter((snapshot) => snapshot.heldByUs && !snapshot.writeLocked)).toHaveLength(1)
})
