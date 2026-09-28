import { describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const A = { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: ['graph_seed_required'] }
const I = { schema_version: 1, source_intake_sha256: 'a'.repeat(64), units: { drawing_units: 'ft', wcs_to_ucs: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], elevation_datum: 'unknown', crs: null } }
const P = { expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: I }

describe('Solar settings seed admission', () => {
  async function seedCase({ name = 'solar-settings', availability = A, params = P, context = {}, reason = null } = {}) {
    const run = setup(availability)
    run.controller.setContext({ solarSettingsFormEnabled: true, entitlements: { entitlements: { run_write: true } }, ...context })
    await run.load()
    run.controller.actions.commitDecision({ lane: 'run', tool: name, params, confidence: 1, source: 'ribbon' })
    expect(run.controller.getState().routeError).toBe(reason)
    expect(run.adapters.commitDecision).toHaveBeenCalledTimes(reason === null ? 1 : 0)
    // Opening or seeding a form must not turn readiness into a runnable claim.
    expect(run.controller.getState().runnableTools).toEqual([])
    expect(run.controller.getState().runnableCapabilityCount).toBe(0)
    return run
  }

  it('SF2 row1 a settings seed request passes the graph_seed_required refusal', async () => {
    await seedCase()
  })

  it('SF2 row2 a settings request without initialize keeps graph_seed_required', async () => {
    await seedCase({ params: { expected_rev: 0, changes: { panels_in_sequence: 3 } }, reason: 'graph_seed_required' })
  })

  it('SF2 row3 an inherited initialize key is not a seed request', async () => {
    await seedCase({ params: Object.create({ initialize: I }), reason: 'graph_seed_required' })
  })

  it('SF2 row4 initialize null still reaches the server', async () => {
    await seedCase({ params: { initialize: null } })
  })

  it('SF2 row5 engine_ready false keeps the refusal', async () => {
    await seedCase({ availability: { ...A, engine_ready: false }, reason: 'graph_seed_required' })
  })

  it('SF2 row6 the local entitlement refuses first', async () => {
    await seedCase({ context: { entitlements: { entitlements: { run_write: false } } }, reason: 'entitlement_required' })
  })

  it('SF2 row7 two refusal reasons keep the joined refusal', async () => {
    await seedCase({ availability: { ...A, refusal_reasons: ['graph_seed_required', 'broker_adapter_unavailable'] }, reason: 'graph_seed_required; broker_adapter_unavailable' })
  })

  it('SF2 row8 another solar tool keeps its refusal', async () => {
    await seedCase({ name: 'solar-size-strings', reason: 'graph_seed_required' })
  })

  it('SF2 row9 the carve-out is off outside the settings form scope', async () => {
    await seedCase({ context: { solarSettingsFormEnabled: false }, reason: 'graph_seed_required' })
  })

  it('SF2 row10 persisted_graph_unavailable seed request reaches the server', async () => {
    await seedCase({ availability: { ...A, refusal_reasons: ['persisted_graph_unavailable'] } })
  })

  it('SF2 row11 not_current_head keeps its refusal', async () => {
    await seedCase({ availability: { ...A, refusal_reasons: ['not_current_head'] }, reason: 'not_current_head' })
  })

  it('SF2 row32 the drawing context reaches getCapabilities', async () => {
    const { controller, services } = setup(A)
    controller.start()
    controller.setContext({ drawingId: 'd1', drawingVersion: 3, solarSettingsFormEnabled: true })
    expect(services.getCapabilities).toHaveBeenLastCalledWith(false, {
      drawing_id: 'd1', project_id: undefined, drawing_version: 3,
    })
    controller.destroy()
  })

  it('SF2 row35 a converted solver decision keeps its refusal', async () => {
    const { controller, services, adapters, load } = setup(A)
    controller.setContext({ solarSettingsFormEnabled: true, entitlements: { entitlements: { run_write: true, solve: true } } })
    services.routePrompt.mockResolvedValue({ lane: 'solve', tool: 'solar-solve-proposal', params: P, confidence: 1 })
    await load()
    await controller.actions.dispatch('solve the Solar layout')
    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(controller.getState().routeError).toBe('graph_seed_required')
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
  })
})

// Copied from the 76648b1e declarations and frozen on purpose. Sibling
// capability work must not change this mock's baseline or its expected counts.
const fixture = Object.freeze([
  ['solar-settings', 'server-builtin', 'local-graph-commit', 'run_write', []],
  ['solar-size-strings', 'cloud-service', null, 'run_write', ['string-sizer']],
  ['solar-panel-groups', 'autocad-lane', null, 'run_write', ['panel-group-create']],
  ['solar-solve-proposal', 'cloud-service', 'cloud-proposal', 'solve', ['solve']],
  ['solar-commit-solve', 'server-builtin', null, 'run_write', ['solve']],
  ['solar-correct-string', 'server-builtin', 'local-graph-commit', 'run_write', []],
  ['solar-assign-equipment', 'autocad-lane', null, 'run_write', ['inverter-add']],
  ['solar-homeruns', 'autocad-lane', null, 'run_write', ['homeruns']],
  ['solar-schedule', 'autocad-lane', null, 'run_write', []],
].map(([name, engine, adapter, entitlement, ledger], index) => Object.freeze({
  name,
  capabilities: Object.freeze([entitlement === 'solve' ? 'solve' : 'drawing.write']),
  solar: Object.freeze({
    schema: 'leaf.solar-tool-view.v1', name, family: 'stringing', wave: 1,
    order: (index + 1) * 10, maturity: 'production', engine, adapter, entitlement,
    interaction: Object.freeze({ mode: 'form' }), ledger: Object.freeze(ledger),
  }),
})))

function setup(overrides = {}, extra = []) {
  const rows = [...fixture, ...extra]
  // Readiness comes from each frozen block: engine_ready is adapter !== null.
  const connected = new Set(rows.filter((row) => row.solar.adapter !== null).map((row) => row.name))
  const tools = rows.map(({ solar, ...tool }) => tool)
  const capabilities = rows.map((tool) => ({
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
    expect(state.capabilityCount).toBe(fixture.length)
    expect(state.runnableTools.map((tool) => tool.name))
      .toEqual(fixture.filter((row) => row.solar.adapter !== null).map((row) => row.name))
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

describe('solar block catalog gates', () => {
  it('gates every catalog row that carries a solar block, with no name list', async () => {
    const extra = {
      name: 'solar-extra', capabilities: ['drawing.read'],
      solar: { ...fixture[0].solar, name: 'solar-extra', entitlement: 'run_read' },
    }
    for (const engineReady of [true, false]) {
      const { controller, adapters, capabilities, load } = setup({}, [extra])
      const entry = capabilities.find((row) => row.name === 'solar-extra')
      entry.availability.engine_ready = engineReady
      entry.availability.refusal_reasons = engineReady ? [] : ['broker_adapter_unavailable']
      await load()
      await controller.actions.dispatchSlash('solar-extra')
      const state = controller.getState()
      expect(state.capabilityCount).toBe(10)
      expect(state.runnableCapabilityCount).toBe(engineReady ? 4 : 3)
      expect(state.runnableTools.some((row) => row.name === 'solar-extra')).toBe(engineReady)
      if (engineReady) {
        expect(adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: 'solar-extra' }))
        expect(state.routeError).toBeNull()
      } else {
        expect(adapters.commitDecision).not.toHaveBeenCalled()
        expect(state.routeError).toBe('broker_adapter_unavailable')
      }
    }
  })

  it('takes the required entitlement from the solar block', async () => {
    for (const [name, entitlements] of [
      ['solar-solve-proposal', { solve: false, run_write: true }],
      ['solar-settings', { run_write: false, solve: true }],
    ]) {
      const { controller, adapters, load } = setup()
      controller.setContext({ entitlements: { entitlements } })
      await load()
      await controller.actions.dispatchSlash(name)
      expect(controller.getState().routeError).toBe('entitlement_required')
      expect(adapters.commitDecision).not.toHaveBeenCalled()
    }
    const { controller, adapters, capabilities, load } = setup()
    const entry = capabilities.find((row) => row.name === 'solar-solve-proposal')
    entry.solar = { ...entry.solar, entitlement: 'run_read' }
    controller.setContext({ entitlements: { entitlements: { run_read: false, run_write: true, solve: true } } })
    await load()
    await controller.actions.dispatchSlash(entry.name)
    expect(controller.getState().routeError).toBe('entitlement_required')
    expect(adapters.commitDecision).not.toHaveBeenCalled()
  })

  it('refuses a malformed solar block as capability_availability_unavailable', async () => {
    const { controller, adapters, capabilities, load } = setup()
    capabilities.find((row) => row.name === 'solar-settings').solar = { schema: 'x' }
    await load()
    await controller.actions.dispatchSlash('solar-settings')
    expect(controller.getState().routeError).toBe('capability_availability_unavailable')
    expect(adapters.commitDecision).not.toHaveBeenCalled()

    const notReady = setup({ engine_ready: false, refusal_reasons: [] })
    await notReady.load()
    await notReady.controller.actions.dispatchSlash('solar-settings')
    expect(notReady.controller.getState().routeError).toBe('capability_not_ready')
    expect(notReady.adapters.commitDecision).not.toHaveBeenCalled()
  })

  it('keeps a learned solar name refused after a drawing change', async () => {
    for (const readStateFirst of [false, true]) {
      const { controller, adapters, load } = setup()
      await load()
      if (readStateFirst) controller.getState()
      controller.setContext({ drawingId: 'drawing-2' })
      await controller.actions.dispatchSlash('solar-solve-proposal')
      expect(controller.getState().routeError).toBe('capability_availability_unavailable')
      expect(adapters.commitDecision).not.toHaveBeenCalled()
      expect(controller.getState().catalog.families).toEqual([])
    }
  })

  it('leaves a row without a solar block to the server refusal', async () => {
    for (const solar of [undefined, null]) {
      const { controller, adapters, capabilities, load } = setup()
      const entry = capabilities.find((row) => row.name === 'solar-settings')
      if (solar === undefined) delete entry.solar
      else entry.solar = solar
      await load()
      await controller.actions.dispatchSlash('solar-settings')
      expect(controller.getState().routeError).toBeNull()
      expect(adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: 'solar-settings' }))
    }
  })

  it('does not pre-refuse an unseen solar name before the first catalog', async () => {
    const { controller, services, adapters } = setup()
    await controller.actions.loadTools()
    let resolveCatalog
    services.getCapabilities.mockImplementationOnce(() => new Promise((resolve) => { resolveCatalog = resolve }))
    const pending = controller.actions.loadCatalog()
    await controller.actions.dispatchSlash('solar-size-strings')
    expect(controller.getState().routeError).toBeNull()
    expect(adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: 'solar-size-strings' }))
    resolveCatalog({ families: [] })
    await pending
  })
})
