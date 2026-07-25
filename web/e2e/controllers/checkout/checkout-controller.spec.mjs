import { expect, test } from '@playwright/test'
import { createCheckoutController, deriveCheckout } from '../../../src/controllers/checkout/createCheckoutController.js'

test('derives free, owned, other, and expired checkout states', () => {
  expect(deriveCheckout(null, 'ours')).toMatchObject({ heldByUs: false, writeLocked: false })
  expect(deriveCheckout({ holder: 'ours' }, 'ours')).toMatchObject({ heldByUs: true, writeLocked: false })
  expect(deriveCheckout({ holder: 'other' }, 'ours')).toMatchObject({ heldByUs: false, writeLocked: true })
  expect(deriveCheckout({ holder: 'other', expires: '2026-01-01T00:00:00Z' }, 'ours', Date.parse('2026-07-24T00:00:00Z')))
    .toMatchObject({ checkout: null, writeLocked: false })
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
      take: async () => { takeCalls += 1; await gate; checkout = { holder: 'ours' }; return { acquired: true } },
      release: async () => null,
    },
  })
  const first = controller.take()
  const second = controller.take()
  releaseTake()
  await Promise.all([first, second])
  expect(takeCalls).toBe(1)
  expect(controller.getSnapshot()).toMatchObject({ heldByUs: true, busy: false })
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
