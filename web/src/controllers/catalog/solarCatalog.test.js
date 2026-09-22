import { describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const names = [
  'solar-settings', 'solar-size-strings', 'solar-panel-groups', 'solar-solve-proposal',
  'solar-commit-solve', 'solar-correct-string', 'solar-assign-equipment',
  'solar-homeruns', 'solar-schedule',
]

// The three capabilities the server's W1 table gives an adapter: the cloud proposal and the two local graph
// commits. The other six keep refusing with broker_adapter_unavailable.
const connected = new Set(['solar-settings', 'solar-solve-proposal', 'solar-correct-string'])

function setup(overrides = {}) {
  const tools = names.map((name) => ({ name, capabilities: [name === 'solar-solve-proposal' ? 'solve' : 'drawing.write'] }))
  const capabilities = tools.map((tool) => ({
    ...tool,
    availability: {
      entitled: true, implemented: true, input_ready: true,
      engine_ready: connected.has(tool.name),
      refusal_reasons: connected.has(tool.name) ? [] : ['broker_adapter_unavailable'],
      ...overrides,
    },
  }))
  const services = {
    getTools: vi.fn(async () => tools),
    getCapabilities: vi.fn(async () => ({ families: [{ family_id: 'stringing', capabilities }] })),
    routePrompt: vi.fn(async () => ({ lane: 'run', tool: 'solar-size-strings', confidence: 0.9 })),
  }
  const adapters = {
    commitDecision: vi.fn((decision) => decision), dismissDecision: vi.fn(),
    startAgentTurn: vi.fn(),
  }
  const controller = createCatalogController({ services, adapters, context: { drawingId: 'drawing-1' } })
  const load = async () => {
    await controller.actions.loadTools()
    await controller.actions.loadCatalog()
  }
  return { controller, services, adapters, load, capabilities }
}

describe('W1 catalog gates', () => {
  it('preserves all four states and reconciles visible counts', async () => {
    const { controller, load } = setup()
    await load()
    const state = controller.getState()
    expect(state.capabilityCount).toBe(9)
    expect(state.runnableCapabilityCount).toBe(3)
    expect(state.unavailableCapabilityCount).toBe(6)
    expect(state.runnableCapabilityCount + state.unavailableCapabilityCount).toBe(state.capabilityCount)
    expect(state.runnableTools.map((tool) => tool.name))
      .toEqual(['solar-settings', 'solar-solve-proposal', 'solar-correct-string'])
    const byName = Object.fromEntries(state.catalog.families[0].capabilities.map((tool) => [tool.name, tool]))
    expect(byName['solar-size-strings'].availability).toMatchObject({
      entitled: true, implemented: true, input_ready: true, engine_ready: false,
    })
    expect(byName['solar-settings'].availability).toMatchObject({
      entitled: true, implemented: true, input_ready: true, engine_ready: true,
    })
  })

  it.each(['entitled', 'implemented', 'input_ready', 'engine_ready'])('refuses %s independently', async (key) => {
    const { controller, adapters, load } = setup({ [key]: false, refusal_reasons: [`${key}_required`] })
    await load()
    await controller.actions.dispatchSlash('solar-solve-proposal')
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(controller.getState().routeError).toBe(`${key}_required`)
    expect(controller.getState().runnableTools).toEqual([])
  })

  it('blocks direct picks, slash picks, alternatives, and prompt routes with the same reason', async () => {
    const { controller, adapters, load } = setup()
    await load()
    controller.actions.commitDecision({ lane: 'run', tool: 'solar-size-strings' })
    await controller.actions.dispatchSlash('solar-size-strings')
    controller.actions.pickAlternative('solar-size-strings')
    await controller.actions.dispatch('size the strings')
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
    expect(controller.getState().routeError).toBe('broker_adapter_unavailable')
  })

  it.each(['solar-settings', 'solar-correct-string'])('allows the local graph commit %s', async (name) => {
    const { controller, adapters, load } = setup()
    await load()
    await controller.actions.dispatchSlash(name)
    expect(adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: name }))
    expect(controller.getState().routeError).toBeFalsy()
  })

  it('allows the reachable proposal and keeps identity when the profile changes', async () => {
    const { controller, adapters, load } = setup()
    await load()
    const catalog = controller.getState().catalog
    controller.setContext({ profile: 'solar' })
    expect(controller.getState().catalog).toBe(catalog)
    await controller.actions.dispatchSlash('solar-solve-proposal')
    expect(adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: 'solar-solve-proposal' }))
  })

  it('passes drawing context to the transport and discards old readiness on drawing changes', async () => {
    const { controller, services, load } = setup()
    await load()
    expect(services.getCapabilities).toHaveBeenLastCalledWith(false, {
      drawing_id: 'drawing-1', project_id: undefined, drawing_version: 'head',
    })
    let resolveOld
    services.getCapabilities.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    const pending = controller.actions.loadCatalog()
    controller.setContext({ drawingId: 'drawing-2' })
    resolveOld({ families: [{ family_id: 'old', capabilities: [] }] })
    await pending
    expect(controller.getState().catalog.families).toEqual([])
    await controller.actions.dispatchSlash('solar-solve-proposal')
    expect(controller.getState().routeError).toBe('capability_availability_unavailable')
  })

  it('does not treat missing or malformed availability as ready', async () => {
    const { controller, capabilities, load } = setup()
    delete capabilities.find((tool) => tool.name === 'solar-solve-proposal').availability
    await load()
    expect(controller.getState().runnableTools.map((tool) => tool.name))
      .toEqual(['solar-settings', 'solar-correct-string'])
    await controller.actions.dispatchSlash('solar-solve-proposal')
    expect(controller.getState().routeError).toBe('capability_availability_unavailable')
  })
})
