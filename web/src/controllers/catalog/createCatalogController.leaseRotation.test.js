import { waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createCatalogController } from './createCatalogController.js'
import { createSolarProjectClient } from '../../solar/solarProjectClient.js'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))

const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const authorityKey = 'session-1'
const catalog = { families: [{ family_id: 'current', capabilities: [] }] }
const lease = { holder: 'editor', fence: '1', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z' }
const disposals = []
afterEach(() => { for (const dispose of disposals.splice(0)) dispose(); vi.useRealTimers() })

function setup({ start = true, expires = lease.expires } = {}) {
  const checkout = { ...lease, expires }
  const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({
    organization_id: binding.organization_id, project_id: binding.project_id, drawing_id: binding.drawing_id,
    acquired: true, checkout, checkout_capability: 'lco1.' + 'a'.repeat(64),
  }) }))
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(binding, authorityKey)
  const requests = []
  const services = { projectSolarClient: client, getTools: async () => [], routePrompt: async () => null,
    getCapabilities: vi.fn((mock, context, options) => new Promise((resolve, reject) => {
      requests.push({ resolve, reject, context, ...options })
    })) }
  const adapters = { draftStorage: null, dismissDecision: vi.fn(), onAuthRequired: vi.fn() }
  const controller = createCatalogController({ services, adapters, context: { solarBinding: binding, authorityKey } })
  disposals.push(() => { controller.destroy(); client.dispose() })
  if (start) controller.start()
  return { client, controller, requests, services, adapters }
}

async function accept(run, index = run.requests.length - 1) {
  run.requests[index].resolve(catalog)
  await waitFor(() => expect(run.controller.getState().projectCatalog?.isCurrent()).toBe(true))
  return run.controller.getState().projectCatalog
}

describe('W1C10 lease-only project catalog recovery', () => {
  it('replaces a settled receipt once per rotation, ignoring pending and unchanged checkout notices', async () => {
    const run = setup()
    let receipt = await accept(run)
    for (let i = 0; i < 3; i += 1) {
      const before = run.requests.length
      await run.client.acquireCheckout()
      expect(receipt.isCurrent()).toBe(false)
      expect(run.controller.getState().projectCatalog).toBeNull()
      expect(run.requests).toHaveLength(before + 1)
      expect(run.requests[before - 1].signal.aborted).toBe(true)
      receipt = await accept(run)
      await run.client.readCheckout()
      await run.client.readCheckout()
      run.controller.setContext({ solarBinding: { ...binding }, authorityKey })
      expect(run.requests).toHaveLength(before + 1)
      expect(receipt.isCurrent()).toBe(true)
    }
  })

  it.each(['resolve', 'reject'])('aborts an in-flight preview and ignores its late %s after recovery', async settlement => {
    const run = setup()
    const old = run.requests[0]
    await run.client.acquireCheckout()
    expect(old.signal.aborted).toBe(true)
    expect(run.requests).toHaveLength(2)
    const receipt = await accept(run)
    if (settlement === 'resolve') old.resolve({ families: [{ family_id: 'obsolete', capabilities: [] }] })
    else old.reject(Object.assign(new Error('obsolete session'), { status: 401 }))
    await Promise.resolve()
    expect(run.controller.getState().projectCatalog).toBe(receipt)
    expect(run.controller.getState().catalogError).toBeNull()
    expect(run.adapters.onAuthRequired).not.toHaveBeenCalled()
    expect(run.requests).toHaveLength(2)
  })

  it('retires an intervening recovery when another rotation occurs before it settles', async () => {
    const run = setup()
    const first = await accept(run)
    await run.client.acquireCheckout()
    const intervening = run.requests[1]
    await run.client.acquireCheckout()
    expect(intervening.signal.aborted).toBe(true)
    expect(run.requests).toHaveLength(3)
    const latest = await accept(run)
    intervening.resolve({ families: [{ family_id: 'intervening', capabilities: [] }] })
    await Promise.resolve()
    expect(run.controller.getState().projectCatalog).toBe(latest)
    expect(first.isCurrent()).toBe(false)
  })

  it('does not retry a failed replacement until a new rotation', async () => {
    const run = setup()
    await accept(run)
    await run.client.acquireCheckout()
    run.requests[1].reject(new Error('catalog offline'))
    await waitFor(() => expect(run.controller.getState().catalogError).toBe('Project tools are unavailable.'))
    for (let i = 0; i < 3; i += 1) {
      run.controller.getState()
      await run.client.readCheckout()
    }
    expect(run.requests).toHaveLength(2)
    await run.client.acquireCheckout()
    expect(run.requests).toHaveLength(3)
    await accept(run)
    expect(run.controller.getState().catalogError).toBeNull()
  })

  it('recovers once when the local lease expires', async () => {
    vi.useFakeTimers()
    const run = setup({ expires: new Date(Date.now() + 1000).toISOString() })
    run.requests[0].resolve(catalog)
    await Promise.resolve()
    await run.client.acquireCheckout()
    run.requests[1].resolve(catalog)
    await Promise.resolve()
    const receipt = run.controller.getState().projectCatalog
    expect(receipt.isCurrent()).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(receipt.isCurrent()).toBe(false)
    expect(run.client.getSnapshot().ownsCheckout).toBe(false)
    expect(run.requests).toHaveLength(3)
    run.requests[2].resolve(catalog)
    await Promise.resolve()
    expect(run.controller.getState().projectCatalog.isCurrent()).toBe(true)
    await vi.advanceTimersByTimeAsync(5000)
    expect(run.requests).toHaveLength(3)
  })

  it.each(['binding', 'authority', 'client'])('keeps invalidation for a %s change', async change => {
    const run = setup()
    const receipt = await accept(run)
    if (change === 'binding') run.client.setScope({ ...binding, input_version_id: binding.project_id }, authorityKey)
    if (change === 'authority') run.client.setScope(binding, 'session-2')
    if (change === 'client') {
      run.services.projectSolarClient = { ...run.client }
      await run.client.acquireCheckout()
    }
    expect(receipt.isCurrent()).toBe(false)
    expect(run.controller.getState().projectCatalog).toBeNull()
    expect(run.requests).toHaveLength(1)
  })

  it('does not duplicate a replacement started by an invalidation subscriber', async () => {
    const run = setup()
    await accept(run)
    let reloaded = false
    run.controller.subscribe(() => {
      if (!reloaded && run.controller.getState().projectCatalog === null) {
        reloaded = true
        void run.controller.actions.loadCatalog()
      }
    })
    await run.client.acquireCheckout()
    expect(reloaded).toBe(true)
    expect(run.requests).toHaveLength(2)
    await accept(run)
  })

  it('does not recover after a subscriber destroys the controller during invalidation', async () => {
    const run = setup()
    await accept(run)
    run.controller.subscribe(() => {
      if (run.controller.getState().projectCatalog === null) run.controller.destroy()
    })
    await run.client.acquireCheckout()
    expect(run.requests).toHaveLength(1)
    expect(run.controller.getState().projectCatalog).toBeNull()
  })

  it('cancels lease recovery when an invalidation subscriber changes authority', async () => {
    const run = setup()
    const receipt = await accept(run)
    let switched = false
    run.controller.subscribe(() => {
      if (!switched && run.controller.getState().projectCatalog === null) {
        switched = true
        run.client.setScope(binding, 'session-2')
      }
    })
    await run.client.acquireCheckout()
    expect(switched).toBe(true)
    expect(run.requests).toHaveLength(1)
    expect(receipt.isCurrent()).toBe(false)
    expect(run.controller.getState().projectCatalog).toBeNull()
  })

  it('preserves explicit loading for a controller that has not started', async () => {
    const run = setup({ start: false })
    const pending = run.controller.actions.loadCatalog()
    const receipt = await accept(run)
    await pending
    await run.client.acquireCheckout()
    expect(receipt.isCurrent()).toBe(false)
    expect(run.controller.getState().projectCatalog).toBeNull()
    expect(run.requests).toHaveLength(1)
  })
})
