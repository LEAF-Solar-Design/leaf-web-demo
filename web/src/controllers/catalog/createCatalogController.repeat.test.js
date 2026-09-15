import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const refusal = { lane: 'run', tool: null, confidence: 0, stub: true, stubKind: 'demo' }
const matched = { lane: 'run', tool: 'count-by-layer', confidence: 0.95 }

function setup() {
  const services = {
    getTools: vi.fn(async () => []),
    getCapabilities: vi.fn(async () => ({ families: [] })),
    routePrompt: vi.fn(async () => refusal),
  }
  const adapters = {
    commitDecision: vi.fn((decision) => decision),
    dismissDecision: vi.fn(),
  }
  const controller = createCatalogController({
    services, adapters, context: { mock: true, agentDisabled: true },
  })
  return { controller, services, adapters }
}

afterEach(() => vi.clearAllMocks())

describe('consecutive refused dispatches', () => {
  it('commits repeat 2 and 3 for the same trimmed request', async () => {
    const { controller, adapters } = setup()
    controller.actions.setPrompt('move all panels 500 units north')
    await controller.actions.dispatch()
    expect(controller.getState().route).not.toHaveProperty('repeat')
    await controller.actions.dispatch('  move all panels 500 units north  ')
    expect(adapters.commitDecision).toHaveBeenLastCalledWith({ ...refusal, repeat: 2 })
    expect(controller.getState().route.repeat).toBe(2)
    await controller.actions.dispatch()
    expect(adapters.commitDecision).toHaveBeenLastCalledWith({ ...refusal, repeat: 3 })
    expect(controller.getState().route.repeat).toBe(3)
  })

  it('resets for a different case-sensitive request', async () => {
    const { controller } = setup()
    await controller.actions.dispatch('unmatched')
    await controller.actions.dispatch('Unmatched')
    expect(controller.getState().route).not.toHaveProperty('repeat')
    await controller.actions.dispatch('unmatched')
    expect(controller.getState().route).not.toHaveProperty('repeat')
  })

  it('never adds repeat to a matched decision and resets refusal history', async () => {
    const { controller, services, adapters } = setup()
    await controller.actions.dispatch('unmatched')
    await controller.actions.dispatch('unmatched')
    services.routePrompt.mockResolvedValueOnce(matched)
    await controller.actions.dispatch('unmatched')
    expect(adapters.commitDecision).toHaveBeenLastCalledWith(matched)
    expect(controller.getState().route).not.toHaveProperty('repeat')
    await controller.actions.dispatch('unmatched')
    expect(controller.getState().route).not.toHaveProperty('repeat')
  })

  it('resets after dismissing the route', async () => {
    const { controller } = setup()
    await controller.actions.dispatch('unmatched')
    controller.actions.dismissRoute()
    await controller.actions.dispatch('unmatched')
    expect(controller.getState().route).not.toHaveProperty('repeat')
  })

  it('resets after another committed decision', async () => {
    const { controller } = setup()
    await controller.actions.dispatch('unmatched')
    controller.actions.commitDecision({ lane: 'build' })
    await controller.actions.dispatch('unmatched')
    expect(controller.getState().route).not.toHaveProperty('repeat')
  })
})
