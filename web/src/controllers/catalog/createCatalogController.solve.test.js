import { describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const bound = {
  lane: 'solve',
  tool: 'string-autofill-opt',
  params: { panelsPerString: 12 },
  confidence: 0.43,
  rationale: "Matched the 'string-autofill-opt' solver capability; confirm before it runs.",
  alternatives: [{ tool: 'solar-solve-proposal', confidence: 0.39 }],
}
const unbound = {
  lane: 'solve',
  tool: null,
  params: { description: 'optimize the panel layout to maximize energy production' },
  confidence: 0.8,
  rationale: 'No solver matches this request; please pick one of the available solvers.',
  alternatives: [{ tool: 'string-autofill-opt', confidence: 0.15 }],
}

function setup(decision, { context, catalog = { families: [] } } = {}) {
  const solver = { name: 'string-autofill-opt', capabilities: ['solve'] }
  const reader = { name: 'count-by-layer', capabilities: ['drawing.read'] }
  const services = {
    getTools: vi.fn(async () => [solver, reader]),
    getCapabilities: vi.fn(async () => catalog),
    routePrompt: vi.fn(async () => decision),
  }
  const adapters = {
    commitDecision: vi.fn((d) => d),
    dismissDecision: vi.fn(),
    startAgentTurn: vi.fn(async () => undefined),
  }
  const controller = createCatalogController({ services, adapters, ...(context ? { context } : {}) })
  return { controller, services, adapters }
}

describe('solve lane dispatch', () => {
  it('arms a bound solve decision as a confirmable run of that tool', async () => {
    const { controller, adapters } = setup(bound)
    const decision = await controller.actions.dispatch('solve the string sizing for this array with 12 panels')
    const expected = { ...bound, lane: 'run', routedLane: 'solve' }

    expect(adapters.commitDecision).toHaveBeenCalledOnce()
    expect(adapters.commitDecision).toHaveBeenCalledWith(expected)
    expect(controller.getState().route).toEqual(expected)
    expect(controller.getState().routeError).toBeNull()
    expect(decision.lane).toBe('run')
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
    expect(bound.lane).toBe('solve')
  })

  it('races the agent alongside the solve chip when the agent tier is on', async () => {
    const { controller, adapters } = setup(bound, { context: { agentDisabled: false } })
    await controller.actions.dispatch('solve the string sizing for this array with 12 panels')

    expect(adapters.commitDecision).toHaveBeenCalledOnce()
    expect(adapters.commitDecision).toHaveBeenCalledWith({ ...bound, lane: 'run', routedLane: 'solve' })
    expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
    expect(adapters.startAgentTurn).toHaveBeenCalledWith(
      'solve the string sizing for this array with 12 panels',
      { lane: 'run', tool: 'string-autofill-opt', confidence: 0.43, rationale: bound.rationale },
      { allowSecretOnce: false },
    )
    expect(controller.getState().agentMode).toBe('race')
  })

  it('leaves an unbound solve decision on the solve lane', async () => {
    const { controller, adapters } = setup(unbound)
    await controller.actions.dispatch('optimize the panel layout to maximize energy production')

    expect(adapters.commitDecision).toHaveBeenCalledOnce()
    expect(adapters.commitDecision).toHaveBeenCalledWith(unbound)
    expect(controller.getState().route).toBe(unbound)
    expect('routedLane' in controller.getState().route).toBe(false)
  })

  it('refuses a bound solar solve proposal with the same reason as a run', async () => {
    const solarRow = {
      name: 'solar-solve-proposal',
      capabilities: ['solve'],
      solar: {
        schema: 'leaf.solar-tool-view.v1',
        name: 'solar-solve-proposal',
        family: 'stringing',
        wave: 1,
        order: 40,
        entitlement: 'solve',
        interaction: { mode: 'form' },
      },
    }
    const catalog = { families: [{ family_id: 'stringing', capabilities: [solarRow] }] }
    const { controller, adapters } = setup({
      lane: 'solve', tool: 'solar-solve-proposal', params: {},
      confidence: 0.45, rationale: 'r', alternatives: [],
    }, { catalog })
    await controller.actions.loadCatalog()

    const decision = await controller.actions.dispatch('solve a proposal')

    expect(decision).toBeUndefined()
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(controller.getState().routeError).toBe('capability_availability_unavailable')
    expect(controller.getState().route).toBeNull()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
  })

  it('arms a solver picked from an unbound solve decision as a run', async () => {
    const { controller, adapters } = setup(unbound)
    await controller.actions.dispatch('optimize the panel layout to maximize energy production')

    const decision = controller.actions.pickAlternative('string-autofill-opt')
    const expected = { lane: 'run', tool: 'string-autofill-opt', confidence: 0.99 }

    expect(decision).toMatchObject(expected)
    expect(adapters.commitDecision).toHaveBeenLastCalledWith(expect.objectContaining(expected))
    expect(controller.getState().route.tool).toBe('string-autofill-opt')
  })
})
