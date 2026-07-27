import { expect, test } from '@playwright/test'
import { createCheckoutController, deriveCheckout } from '../../../src/controllers/checkout/createCheckoutController.js'

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
