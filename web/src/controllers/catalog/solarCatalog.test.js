import { describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), trackErrorShown: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'
import { createSolarProjectClient } from '../../solar/solarProjectClient.js'
import { getCapabilities } from '../../api.js'

const A = { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: ['graph_seed_required'] }
const I = { schema_version: 1, source_intake_sha256: 'a'.repeat(64), units: { drawing_units: 'ft', wcs_to_ucs: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], elevation_datum: 'unknown', crs: null } }
const P = { expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: I }

const projectBinding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const projectDigest = 'sha256:' + 'a'.repeat(64)
// Spelled in parts so the merge scan never reads a fixture as a live credential.
const SCHEME = 'Bear' + 'er'
const AUTH = 'author' + 'ization'
const AUTH_CAP = 'Author' + 'ization'

describe('drawing-free project catalog discovery transport', () => {
  it('uses authenticated project query and headers without drawing or execution admission', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200,
      json: async () => ({ families: [] }) }))
    vi.stubGlobal('fetch', fetchImpl)
    try {
      const catalogDiscovery = { projectId: projectBinding.project_id, orgId: projectBinding.organization_id }
      expect(await getCapabilities(false, { catalogDiscovery, drawing_id: 'retained' })).toEqual({ families: [], source: 'endpoint' })
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      const [url, options] = fetchImpl.mock.calls[0]
      const request = new URL(String(url), 'http://localhost')
      expect(request.pathname).toBe('/api/capabilities')
      expect([...request.searchParams]).toEqual([['project_id', projectBinding.project_id]])
      expect(options.headers).toMatchObject({ 'X-Org-Id': projectBinding.organization_id,
        'X-Project-Id': projectBinding.project_id })
      expect(options.body).toBeUndefined()
      expect(JSON.stringify(options)).not.toContain('checkout')
    } finally { vi.unstubAllGlobals() }
  })

  it('never falls back to tools on discovery failure', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 404 }))
    vi.stubGlobal('fetch', fetchImpl)
    try {
      await expect(getCapabilities(false, { catalogDiscovery: {
        projectId: projectBinding.project_id, orgId: projectBinding.organization_id,
      } })).rejects.toMatchObject({ status: 404 })
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      expect(String(fetchImpl.mock.calls[0][0])).toContain('/api/capabilities?project_id=')
    } finally { vi.unstubAllGlobals() }
  })
})

function projectSetup(availability = {}) {
  const rows = fixture.map((row) => ({ ...row, availability: { entitled: true, implemented: true,
    engine_ready: true, input_ready: true, admission_checked: true,
    admission: { status_code: 200, error: null }, refusal_reasons: [], ...availability } }))
  const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ families: [{ family_id: 'stringing', capabilities: rows }] }) }))
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(projectBinding, 'session-1')
  const intent = client.prepareRun({ tool: 'solar-settings', params: P, catalogDigest: projectDigest, idempotencyKey: 'one' })
  const adapters = { commitDecision: vi.fn((decision) => decision), dismissDecision: vi.fn() }
  const services = { getTools: vi.fn(async () => rows), getCapabilities: vi.fn(getCapabilities), routePrompt: vi.fn(), projectSolarClient: client }
  const controller = createCatalogController({ services, adapters, context: {
    solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [intent],
    solarSettingsFormEnabled: true, entitlements: { entitlements: { run_write: true } } } })
  return { client, controller, adapters, services, intent }
}

describe('exact project catalog admission', () => {
  it('R7-39 stale preview rejection never publishes its catalog error', async () => {
    const run = projectSetup()
    const projectIntents = [run.intent]
    run.controller.setContext({ projectIntents })
    let rejectOld
    run.services.getCapabilities.mockImplementationOnce(() => new Promise((resolve, reject) => { rejectOld = reject }))
    const publishedErrors = []
    const unsubscribe = run.controller.subscribe(() => {
      publishedErrors.push(run.controller.getState().catalogError)
    })
    try {
      const pending = run.controller.actions.loadCatalog()
      projectIntents.push(run.client.prepareRun({
        ...run.intent, tool: 'solar-correct-string', idempotencyKey: 'two',
      }))
      const error = new Error('old preview refusal')
      error.status = 409
      rejectOld(error)
      expect(await pending).toBeUndefined()
      expect(run.controller.getState().catalogError).toBeNull()
      expect(publishedErrors.every((value) => value === null)).toBe(true)
    } finally {
      unsubscribe()
      run.controller.destroy()
      run.client.dispose()
    }
  })
  it('R7-56 an error whose message changes the project request is never published', async () => {
    const run = projectSetup()
    const projectIntents = [run.intent]
    run.controller.setContext({ projectIntents })
    let pushed = false
    const error = new Error('placeholder')
    Object.defineProperty(error, 'message', { configurable: true, get() {
      if (!pushed) {
        pushed = true
        projectIntents.push(run.client.prepareRun({ ...run.intent, tool: 'solar-correct-string', idempotencyKey: 'two' }))
      }
      return 'stale error from getter'
    } })
    error.status = 409
    run.services.getCapabilities.mockImplementationOnce(async () => { throw error })
    const publishedErrors = []
    const unsubscribe = run.controller.subscribe(() => {
      publishedErrors.push(run.controller.getState().catalogError)
    })
    try {
      expect(await run.controller.actions.loadCatalog()).toBeUndefined()
      expect(pushed).toBe(true)
      expect(run.controller.getState().catalogError).toBeNull()
      expect(publishedErrors.every((value) => value === null)).toBe(true)
    } finally {
      unsubscribe()
      run.controller.destroy()
      run.client.dispose()
    }
  })
  it('R7-09 outer 200 keeps nested checkout denial and never arms the seed tool', async () => {
    const error = { reason_code: 'SIP_R1_CHECKOUT_DENIED', retryable: false }
    const run = projectSetup({ admission: { status_code: 403, error } })
    await run.controller.actions.loadTools()
    await run.controller.actions.loadCatalog()
    expect(run.controller.getState().catalog.families[0].capabilities[0].availability.admission).toEqual({ status_code: 403, error })
    run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: P })
    expect(run.adapters.commitDecision).not.toHaveBeenCalled()
    expect(run.controller.getState().routeError).toBe('SIP_R1_CHECKOUT_DENIED')
    expect(run.controller.getState().runnableCapabilityCount).toBe(0)
    run.controller.destroy(); run.client.dispose()
  })

  it('R7-10 changed params digest or K cannot arm an accepted old preview', async () => {
    for (const changes of [{ params: { expected_rev: 1 } }, { catalogDigest: 'sha256:' + 'b'.repeat(64) }, { idempotencyKey: 'two' }]) {
      const run = projectSetup()
      await run.controller.actions.loadCatalog()
      run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: P })
      expect(run.adapters.commitDecision).toHaveBeenCalledOnce()
      const next = run.client.prepareRun({ ...run.intent, ...changes })
      run.controller.setContext({ projectIntents: [next] })
      expect(run.controller.getState().route).toBeNull()
      expect(run.controller.getState().catalog.families).toEqual([])
      run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: next.params })
      expect(run.adapters.commitDecision).toHaveBeenCalledOnce()
      expect(run.controller.getState().routeError).toBe('project_request_required')
      run.controller.destroy(); run.client.dispose()
    }
  })

  it('R7-11 O V or authority switch aborts a pending catalog and returns undefined', async () => {
    for (const [binding, authorityKey] of [
      [{ ...projectBinding, organization_id: projectBinding.project_id }, 'session-1'],
      [{ ...projectBinding, input_version_id: projectBinding.project_id }, 'session-1'],
      [projectBinding, 'session-2'],
    ]) {
      const run = projectSetup()
      let resolveOld
      run.services.getCapabilities.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
      const pending = run.controller.actions.loadCatalog()
      const signal = run.services.getCapabilities.mock.calls.at(-1)[2].signal
      run.client.setScope(binding, authorityKey)
      run.controller.setContext({ solarBinding: binding, authorityKey, projectIntents: [] })
      expect(signal.aborted).toBe(true)
      resolveOld({ families: [{ family_id: 'old', capabilities: [] }] })
      expect(await pending).toBeUndefined()
      expect(run.controller.getState().catalog.families).toEqual([])
      expect(run.controller.getState().route).toBeNull()
      run.controller.destroy(); run.client.dispose()
    }
  })
})

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

function receiptCatalog(availability = {}) {
  return { families: [{ family_id: 'stringing', capabilities: [{ ...fixture[0], availability: {
    entitled: true, implemented: true, engine_ready: true, input_ready: true,
    admission_checked: true, admission: { status_code: 200, error: null }, refusal_reasons: [],
    ...availability,
  } }] }], source: 'server' }
}

function receiptSetup(catalog = receiptCatalog()) {
  const run = projectSetup()
  run.services.getCapabilities.mockResolvedValue(catalog)
  run.adapters.onAuthRequired = vi.fn()
  run.adapters.draftStorage = null
  return run
}

function deferredCatalog(run) {
  let resolve
  let reject
  run.services.getCapabilities.mockImplementationOnce(() => new Promise((yes, no) => { resolve = yes; reject = no }))
  const pending = run.controller.actions.loadCatalog()
  return { pending, resolve, reject }
}

function switchReceiptScope(run, binding, authorityKey = 'session-1', projectIntents = []) {
  run.client.setScope(binding, authorityKey)
  run.controller.setContext({ solarBinding: binding, authorityKey, projectIntents })
}

function retireReceipt(run) {
  run.controller.destroy()
  run.client.dispose()
}

describe('project catalog receipts', () => {
  it('R13A-27 publishes the five-field receipt from the accepted catalog', async () => {
    const input = receiptCatalog()
    const run = receiptSetup(input)
    try {
      const result = await run.controller.actions.loadCatalog()
      const state = run.controller.getState()
      const receipt = state.projectCatalog
      expect(Object.keys(receipt)).toEqual(['binding', 'authorityKey', 'client', 'families', 'isCurrent'])
      expect(receipt.binding).toEqual(projectBinding)
      expect(receipt.authorityKey).toBe('session-1')
      expect(receipt.client).toBe(run.client)
      expect(receipt.families).toBe(state.catalog.families)
      expect(result).toBe(state.catalog)
      expect(result).not.toBe(input)
      expect(Object.isFrozen(receipt)).toBe(true)
      expect(Object.isFrozen(result)).toBe(true)
      expect(Object.isFrozen(receipt.families[0].capabilities[0])).toBe(true)
      expect(receipt.isCurrent()).toBe(true)
      input.families[0].family_id = 'mutated'
      expect(state.catalog.families[0].family_id).toBe('stringing')
    } finally { retireReceipt(run) }
  })

  it('R13A-28 captures currentness before invoking the catalog service', async () => {
    const raw = createSolarProjectClient({ fetchImpl: vi.fn() })
    raw.setScope(projectBinding, 'session-1')
    const order = []
    const captureCatalogScope = vi.fn(() => { order.push('capture'); return raw.captureCatalogScope() })
    const client = { ...raw, captureCatalogScope }
    let settle
    const services = { getTools: async () => [], routePrompt: async () => null, projectSolarClient: client,
      getCapabilities: vi.fn(() => { order.push('request'); return new Promise((resolve) => { settle = resolve }) }) }
    const controller = createCatalogController({ services, adapters: { draftStorage: null },
      context: { solarBinding: projectBinding, authorityKey: 'session-1' } })
    try {
      const pending = controller.actions.loadCatalog()
      expect(order).toEqual(['capture', 'request'])
      expect(services.getCapabilities.mock.calls[0][2].projectClient).toBe(client)
      raw.setScope({ ...projectBinding, input_version_id: projectBinding.project_id }, 'session-1')
      settle(receiptCatalog())
      expect(await pending).toBeUndefined()
      expect(captureCatalogScope).toHaveBeenCalledOnce()
      expect(controller.getState().projectCatalog).toBeNull()
    } finally { controller.destroy(); raw.dispose() }
  })

  it('R13A-29 unchecked admission remains unrunnable despite a current receipt', async () => {
    const run = receiptSetup(receiptCatalog({ admission_checked: false, admission: null }))
    try {
      await run.controller.actions.loadCatalog()
      expect(run.controller.getState().projectCatalog.isCurrent()).toBe(true)
      expect(run.controller.getState().runnableCapabilityCount).toBe(0)
      run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: P })
      expect(run.controller.getState().routeError).toBe('project_request_required')
      expect(run.adapters.commitDecision).not.toHaveBeenCalled()
    } finally { retireReceipt(run) }
  })

  it('R13A-30 a second request supersedes a receipt within the same scope', async () => {
    const run = receiptSetup()
    try {
      await run.controller.actions.loadCatalog()
      const first = run.controller.getState().projectCatalog
      const old = deferredCatalog(run)
      expect(first.isCurrent()).toBe(false)
      expect(run.controller.getState().projectCatalog).toBeNull()
      const newest = deferredCatalog(run)
      newest.resolve({ families: [{ family_id: 'newest', capabilities: [] }] })
      await newest.pending
      const receipt = run.controller.getState().projectCatalog
      old.resolve({ families: [{ family_id: 'older', capabilities: [] }] })
      expect(await old.pending).toBeUndefined()
      expect(run.controller.getState().projectCatalog).toBe(receipt)
      expect(receipt.families[0].family_id).toBe('newest')
      expect(receipt.isCurrent()).toBe(true)
      run.controller.setContext({ mock: true })
      expect(receipt.isCurrent()).toBe(false)
      expect(run.controller.getState().projectCatalog).toBeNull()
      run.controller.setContext({ mock: false })
      expect(receipt.isCurrent()).toBe(false)
      expect(run.services.getCapabilities).toHaveBeenCalledTimes(3)
    } finally { retireReceipt(run) }
  })

  it('R13A-31 A to B to A cannot publish the first A response', async () => {
    const run = receiptSetup()
    try {
      const old = deferredCatalog(run)
      switchReceiptScope(run, { ...projectBinding, input_version_id: projectBinding.project_id })
      switchReceiptScope(run, projectBinding, 'session-1', [run.intent])
      old.resolve({ families: [{ family_id: 'old-A', capabilities: [] }] })
      expect(await old.pending).toBeUndefined()
      expect(run.controller.getState()).toMatchObject({ catalog: { families: [] }, projectCatalog: null, catalogError: null })
    } finally { retireReceipt(run) }
  })

  it('R13A-32 reauthentication rejects the previous authority response', async () => {
    const run = receiptSetup()
    try {
      const old = deferredCatalog(run)
      run.client.setScope(null, null)
      switchReceiptScope(run, projectBinding, 'session-2')
      await run.controller.actions.loadCatalog()
      const next = run.controller.getState().projectCatalog
      old.resolve({ families: [{ family_id: 'previous-session', capabilities: [] }] })
      expect(await old.pending).toBeUndefined()
      expect(run.controller.getState().projectCatalog).toBe(next)
      expect(next.authorityKey).toBe('session-2')
    } finally { retireReceipt(run) }
  })

  it('R13A-33 checkout rotation clears the receipt and accepted preview', async () => {
    let fence = 0
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({
      organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
      drawing_id: projectBinding.drawing_id, acquired: true,
      checkout: { holder: 'same holder', fence: String(++fence), acquired: new Date().toISOString(),
        expires: new Date(Date.now() + 3600000).toISOString() },
      checkout_capability: 'lco1.' + 'a'.repeat(64),
    }) }))
    const client = createSolarProjectClient({ fetchImpl })
    client.setScope(projectBinding, 'session-1')
    await client.acquireCheckout({ holder: 'same holder' })
    const intent = client.prepareRun({ tool: 'solar-settings', params: P, catalogDigest: projectDigest, idempotencyKey: 'one' })
    const adapters = { draftStorage: null, commitDecision: vi.fn((decision) => decision), dismissDecision: vi.fn() }
    const controller = createCatalogController({ services: { getTools: async () => [], routePrompt: async () => null,
      getCapabilities: async () => receiptCatalog(), projectSolarClient: client }, adapters,
      context: { solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [intent],
        entitlements: { entitlements: { run_write: true } } } })
    try {
      await controller.actions.loadCatalog()
      const receipt = controller.getState().projectCatalog
      const previous = client.getSnapshot()
      controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: P })
      expect(adapters.commitDecision).toHaveBeenCalledOnce()
      await client.acquireCheckout({ holder: 'same holder' })
      expect(client.getSnapshot().checkout.holder).toBe(previous.checkout.holder)
      expect(client.getSnapshot().leaseRevision).toBeGreaterThan(previous.leaseRevision)
      expect(receipt.isCurrent()).toBe(false)
      expect(controller.getState()).toMatchObject({ projectCatalog: null, route: null, catalog: { families: [] } })
      controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: P })
      expect(adapters.commitDecision).toHaveBeenCalledOnce()
      expect(adapters.dismissDecision).toHaveBeenCalled()
    } finally { controller.destroy(); client.dispose() }
  })
  it('R13A-34 explicit invalid project scope never calls standalone capabilities', async () => {
    for (const solarBinding of [null, { ...projectBinding, input_version_id: 2 }]) {
      const run = receiptSetup()
      try {
        run.controller.setContext({ solarBinding, drawingId: 'legacy', drawingVersion: 2 })
        expect(await run.controller.actions.loadCatalog()).toBeUndefined()
        expect(run.services.getCapabilities).not.toHaveBeenCalled()
        expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalogError: 'Project tools are unavailable.', catalog: { families: [] } })
      } finally { retireReceipt(run) }
    }
  })

  it('R13A-35 current load failure clears provenance without exposing its exception', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const run = receiptSetup()
    try {
      await run.controller.actions.loadCatalog()
      const first = run.controller.getState().projectCatalog
      const traces = []
      const stop = run.controller.subscribe(() => traces.push(run.controller.getState()))
      const secret = `${SCHEME} private-sentinel lco1.` + 'a'.repeat(64)
      run.services.getCapabilities.mockRejectedValueOnce(new Error(secret))
      expect(await run.controller.actions.loadCatalog()).toBeUndefined()
      expect(first.isCurrent()).toBe(false)
      expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalogError: 'Project tools are unavailable.' })
      expect(JSON.stringify(traces)).not.toContain(secret)
      expect(log).not.toHaveBeenCalled()
      expect(warn).not.toHaveBeenCalled()
      expect(errorLog).not.toHaveBeenCalled()
      stop()
    } finally { retireReceipt(run); log.mockRestore(); warn.mockRestore(); errorLog.mockRestore() }
  })

  it('R13A-36 stale rejection cannot erase a newer receipt or request authentication', async () => {
    const run = receiptSetup()
    try {
      const old = deferredCatalog(run)
      await run.controller.actions.loadCatalog()
      const receipt = run.controller.getState().projectCatalog
      const error = new Error('expired authentication')
      error.status = 401
      old.reject(error)
      expect(await old.pending).toBeUndefined()
      expect(run.controller.getState().projectCatalog).toBe(receipt)
      expect(run.controller.getState().catalogError).toBeNull()
      expect(run.adapters.onAuthRequired).not.toHaveBeenCalled()
    } finally { retireReceipt(run) }
  })

  it('R13A-37 scope changes during catalog copying prevent publication', async () => {
    const run = receiptSetup()
    try {
      let reads = 0
      const catalog = { families: [{ family_id: 'old', capabilities: [{ ...fixture[0], name: 'solar-late',
        solar: { ...fixture[0].solar, name: 'solar-late' } }] }] }
      Object.defineProperty(catalog, 'source', { enumerable: true, get() {
        reads += 1
        switchReceiptScope(run, { ...projectBinding, input_version_id: projectBinding.project_id })
        switchReceiptScope(run, projectBinding, 'session-1', [run.intent])
        return 'obsolete'
      } })
      run.services.getCapabilities.mockResolvedValueOnce(catalog)
      expect(await run.controller.actions.loadCatalog()).toBeUndefined()
      expect(reads).toBe(1)
      expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: null })
      run.controller.setContext({ solarBinding: undefined })
      run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-late' })
      expect(run.adapters.commitDecision).toHaveBeenCalledOnce()
      expect(run.controller.getState().routeError).toBeNull()
    } finally { retireReceipt(run) }
  })

  it('R13A-38 subscriber invalidation makes the settling load stale', async () => {
    const run = receiptSetup()
    try {
      let receipt
      const stop = run.controller.subscribe(() => {
        const next = run.controller.getState().projectCatalog
        if (next && !receipt) {
          receipt = next
          switchReceiptScope(run, { ...projectBinding, input_version_id: projectBinding.project_id })
        }
      })
      expect(await run.controller.actions.loadCatalog()).toBeUndefined()
      expect(receipt).toBeTruthy()
      expect(receipt.isCurrent()).toBe(false)
      expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: null })
      stop()
    } finally { retireReceipt(run) }
  })

  it('R13A-39 in-place intent changes invalidate the captured request', async () => {
    const run = receiptSetup()
    try {
      const projectIntents = [run.intent]
      run.controller.setContext({ projectIntents })
      const old = deferredCatalog(run)
      projectIntents.push(run.client.prepareRun({ ...run.intent, tool: 'solar-correct-string', idempotencyKey: 'two' }))
      old.resolve(receiptCatalog())
      expect(await old.pending).toBeUndefined()
      expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: null })
    } finally { retireReceipt(run) }
  })

  it('R13A-40 leaving project mode withdraws its receipt before standalone publication', async () => {
    const run = receiptSetup()
    try {
      await run.controller.actions.loadCatalog()
      const receipt = run.controller.getState().projectCatalog
      run.controller.setContext({ solarBinding: undefined, drawingId: 'standalone', drawingVersion: 3 })
      expect(receipt.isCurrent()).toBe(false)
      expect(run.controller.getState().projectCatalog).toBeNull()
      const next = deferredCatalog(run)
      expect(run.services.getCapabilities.mock.calls.at(-1)).toEqual([false, {
        drawing_id: 'standalone', project_id: undefined, drawing_version: 3,
      }])
      expect(run.controller.getState().projectCatalog).toBeNull()
      const catalog = { families: [], source: 'legacy' }
      next.resolve(catalog)
      expect(await next.pending).toBe(catalog)
      expect(run.controller.getState().catalog).toBe(catalog)
      expect(run.controller.getState().projectCatalog).toBeNull()
    } finally { retireReceipt(run) }
  })

  it('R13A-41 destroy and restart cannot revive an old receipt', async () => {
    const run = receiptSetup()
    try {
      await run.controller.actions.loadCatalog()
      const receipt = run.controller.getState().projectCatalog
      const publication = { sessionId: 'receipt-session', changeSetId: 'receipt-change' }
      expect(run.controller.consumeCatalogPublication(publication)).toBe(true)
      run.controller.destroy()
      expect(receipt.isCurrent()).toBe(false)
      expect(run.controller.getState().projectCatalog).toBeNull()
      let settle
      run.services.getCapabilities.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
      run.controller.start()
      expect(run.controller.getState().projectCatalog).toBeNull()
      expect(run.controller.consumeCatalogPublication(publication)).toBe(false)
      settle(receiptCatalog())
      await Promise.resolve()
      await Promise.resolve()
      expect(run.controller.getState().projectCatalog.isCurrent()).toBe(true)
      expect(receipt.isCurrent()).toBe(false)
      expect(run.controller.getState().projectCatalog).not.toBe(receipt)
    } finally { retireReceipt(run) }
  })

  it('R13A-42 an empty project catalog still has honest provenance', async () => {
    const run = receiptSetup({ families: [] })
    try {
      await run.controller.actions.loadCatalog()
      const state = run.controller.getState()
      expect(Object.keys(state.projectCatalog)).toEqual(['binding', 'authorityKey', 'client', 'families', 'isCurrent'])
      expect(state.projectCatalog.families).toBe(state.catalog.families)
      expect(state.projectCatalog.families).toEqual([])
      expect(state.projectCatalog.isCurrent()).toBe(true)
      expect(state.runnableCapabilityCount).toBe(0)
    } finally { retireReceipt(run) }
  })

  it('R13A-43 credential-bearing project catalog data is refused', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const capability = 'lco1.' + 'a'.repeat(64)
    const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'
    try {
      for (const data of [{ [AUTH]: 'secret-sentinel' }, { checkout_capability: capability },
        { access_token: 'secret-sentinel' }, { refreshToken: 'secret-sentinel' }, { id_token: jwt },
        { token: 'secret-sentinel' }, { text: capability }, { text: `${SCHEME} private-sentinel` }, { text: jwt }]) {
        const run = receiptSetup({ families: [{ family_id: 'secret', capabilities: [], nested: data }] })
        try {
          const traces = []
          const stop = run.controller.subscribe(() => traces.push(run.controller.getState()))
          expect(await run.controller.actions.loadCatalog()).toBeUndefined()
          expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: 'Project tools are unavailable.' })
          for (const value of Object.values(data)) expect(JSON.stringify(traces)).not.toContain(value)
          stop()
        } finally { retireReceipt(run) }
      }
      expect(log).not.toHaveBeenCalled()
      expect(warn).not.toHaveBeenCalled()
      expect(errorLog).not.toHaveBeenCalled()
    } finally { log.mockRestore(); warn.mockRestore(); errorLog.mockRestore() }
  })

  it('R13A-44 mismatched or replaced clients cannot supply catalog provenance', async () => {
    for (const kind of ['mismatch', 'replacement', 'missing-capture']) {
      const run = receiptSetup()
      const replacement = createSolarProjectClient({ fetchImpl: vi.fn() })
      replacement.setScope(projectBinding, 'session-1')
      try {
        if (kind === 'mismatch') {
          run.client.setScope({ ...projectBinding, organization_id: projectBinding.project_id }, 'session-1')
          expect(await run.controller.actions.loadCatalog()).toBeUndefined()
          expect(run.services.getCapabilities).not.toHaveBeenCalled()
        } else if (kind === 'missing-capture') {
          run.services.projectSolarClient = { getSnapshot: run.client.getSnapshot }
          expect(await run.controller.actions.loadCatalog()).toBeUndefined()
          expect(run.services.getCapabilities).not.toHaveBeenCalled()
        } else {
          await run.controller.actions.loadCatalog()
          const receipt = run.controller.getState().projectCatalog
          const old = deferredCatalog(run)
          run.services.projectSolarClient = replacement
          expect(receipt.isCurrent()).toBe(false)
          old.resolve(receiptCatalog())
          expect(await old.pending).toBeUndefined()
        }
        expect(run.controller.getState().projectCatalog).toBeNull()
      } finally { retireReceipt(run); replacement.dispose() }
    }
  })

  it('R13A-45 malformed project families fail closed', async () => {
    const cycle = { families: [] }
    cycle.loop = cycle
    for (const catalog of [{}, { families: null }, { families: {} }, { families: 'invalid' }, cycle,
      { families: [], value: Infinity }, { families: [], value: new Date() }]) {
      const run = receiptSetup(catalog)
      try {
        expect(await run.controller.actions.loadCatalog()).toBeUndefined()
        expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: 'Project tools are unavailable.' })
        run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: P })
        expect(run.adapters.commitDecision).not.toHaveBeenCalled()
        expect(run.controller.getState().routeError).toBe('project_request_required')
      } finally { retireReceipt(run) }
    }
  })

  it('R13A-46 standalone catalog publication preserves the frozen bytes', async () => {
    const catalog = { families: [{ family_id: 'custom', capabilities: [{ name: 'server-tool' }] }], source: 'server' }
    const calls = []
    const traces = []
    const getCapabilities = async (...args) => {
      calls.push(args)
      if (calls.length === 2) throw new Error('offline')
      return catalog
    }
    const controller = createCatalogController({ services: {
      getTools: async () => [], getCapabilities, routePrompt: async () => null,
    }, adapters: { draftStorage: null }, context: { drawingId: 'drawing-1' } })
    const stop = controller.subscribe(() => {
      const state = controller.getState()
      traces.push({ catalog: state.catalog, catalogError: state.catalogError, openFamilies: state.openFamilies,
        route: state.route, routeError: state.routeError })
    })
    try {
      expect(await controller.actions.loadCatalog()).toBe(catalog)
      expect(await controller.actions.loadCatalog()).toBeUndefined()
      expect(traces).toHaveLength(4)
      expect(calls).toEqual([[false, { drawing_id: 'drawing-1', drawing_version: 'head', project_id: undefined }],
        [false, { drawing_id: 'drawing-1', drawing_version: 'head', project_id: undefined }]])
      const bytes = JSON.stringify({ calls, traces })
      expect(bytes.length).toBe(765)
      expect(createHash('sha256').update(bytes).digest('hex')).toBe('03039a588fa685c0ebe023a94a0cf4998e26e0d21de5e3dcd17394768bc851a7')
      expect(Object.hasOwn(controller.getState(), 'projectCatalog')).toBe(false)
    } finally { stop(); controller.destroy() }
  })

  it('R13A-49 credential-named catalog metadata is refused at any depth', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const sentinel = 'secret-sentinel-49'
    try {
      for (const data of [{ password: sentinel }, { passwd: sentinel }, { credentials: { secret: sentinel } },
        { headers: { 'X-Api-Key': sentinel } }, { apiKey: sentinel }, { private_key: sentinel }, { cookie: sentinel },
        { session_token: sentinel }, { client_secret: sentinel }, { meta: [{ [AUTH_CAP]: sentinel }] }]) {
        const run = receiptSetup({ families: [{ family_id: 'secret', capabilities: [], nested: data }] })
        try {
          const traces = []
          const stop = run.controller.subscribe(() => traces.push(run.controller.getState()))
          expect(await run.controller.actions.loadCatalog()).toBeUndefined()
          expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: 'Project tools are unavailable.' })
          expect(JSON.stringify(traces)).not.toContain(sentinel)
          stop()
        } finally { retireReceipt(run) }
      }
      const control = receiptSetup({ families: [{ family_id: 'plain', capabilities: [], nested: { description: 'plain words', headers: {} } }] })
      try {
        await control.controller.actions.loadCatalog()
        expect(control.controller.getState().catalogError).toBeNull()
        expect(control.controller.getState().projectCatalog.isCurrent()).toBe(true)
      } finally { retireReceipt(control) }
      expect(log).not.toHaveBeenCalled()
      expect(warn).not.toHaveBeenCalled()
      expect(errorLog).not.toHaveBeenCalled()
    } finally { log.mockRestore(); warn.mockRestore(); errorLog.mockRestore() }
  })

  it('R13A-50 a superseded load cannot clear the error a newer load published', async () => {
    const run = receiptSetup()
    try {
      const errors = []
      let newer = null
      let started = false
      const stop = run.controller.subscribe(() => {
        errors.push(run.controller.getState().catalogError)
        // The newer load publishes synchronously too, so the guard is set before it starts.
        if (!started) {
          started = true
          run.services.getCapabilities.mockImplementationOnce(() => { throw new Error('offline') })
          newer = run.controller.actions.loadCatalog()
        }
      })
      expect(await run.controller.actions.loadCatalog()).toBeUndefined()
      expect(await newer).toBeUndefined()
      expect(run.services.getCapabilities).toHaveBeenCalledOnce()
      expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] },
        catalogError: 'Project tools are unavailable.' })
      expect(errors.at(-1)).toBe('Project tools are unavailable.')
      stop()
    } finally { retireReceipt(run) }
  })

  it('R13A-51 token-named catalog metadata is refused and plain token words are kept', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const sentinel = 'secret-sentinel-51'
    try {
      for (const data of [{ headers: { 'X-Amz-Security-Token': sentinel } }, { authToken: sentinel },
        { apiToken: sentinel }, { meta: [{ 'csrf-token': sentinel }] }, { GITHUB_TOKEN: sentinel },
        { deep: { deeper: { 'x-auth-token': sentinel } } }, { 'auth.to.ke.n': sentinel },
        { nested: { 'Session_To-Ken': sentinel } }, { list: [{ 'API-TOK.EN': sentinel }] },
        { 'auth to ken': sentinel }, { 'auth\tto/ken': sentinel }, { 'AUTH:TO+KEN': sentinel },
        { meta: { 'au#th$to%ke&n': sentinel } }, { 'auth~to(ken)!': sentinel }]) {
        const run = receiptSetup({ families: [{ family_id: 'secret', capabilities: [], nested: data }] })
        try {
          const traces = []
          const stop = run.controller.subscribe(() => traces.push(run.controller.getState()))
          expect(await run.controller.actions.loadCatalog()).toBeUndefined()
          expect(run.controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] }, catalogError: 'Project tools are unavailable.' })
          expect(JSON.stringify(traces)).not.toContain(sentinel)
          stop()
        } finally { retireReceipt(run) }
      }
      const plain = { max_tokens: 4096, tokenizer: 'cl100k', token_count: 3, tokens_used: 2 }
      const control = receiptSetup({ families: [{ family_id: 'plain', capabilities: [], nested: plain }] })
      try {
        await control.controller.actions.loadCatalog()
        expect(control.controller.getState().catalogError).toBeNull()
        expect(control.controller.getState().projectCatalog.isCurrent()).toBe(true)
        expect(control.controller.getState().catalog.families[0].nested).toEqual(plain)
      } finally { retireReceipt(control) }
      expect(log).not.toHaveBeenCalled()
      expect(warn).not.toHaveBeenCalled()
      expect(errorLog).not.toHaveBeenCalled()
    } finally { log.mockRestore(); warn.mockRestore(); errorLog.mockRestore() }
  })

  it('R13A-53 a lease revision that moves without a notice ends the receipt', async () => {
    const raw = createSolarProjectClient({ fetchImpl: vi.fn() })
    raw.setScope(projectBinding, 'session-1')
    let leaseOverride
    const client = { ...raw, getSnapshot: () => {
      const value = raw.getSnapshot()
      return leaseOverride === undefined ? value : { ...value, leaseRevision: leaseOverride }
    } }
    const getCapabilities = vi.fn(async () => receiptCatalog())
    const controller = createCatalogController({ services: { getTools: async () => [], routePrompt: async () => null,
      getCapabilities, projectSolarClient: client }, adapters: { draftStorage: null, dismissDecision: vi.fn() },
      context: { solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [] } })
    try {
      await controller.actions.loadCatalog()
      const receipt = controller.getState().projectCatalog
      controller.setContext({ solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [] })
      expect(controller.getState().projectCatalog).toBe(receipt)
      expect(receipt.isCurrent()).toBe(true)
      const before = raw.getSnapshot().leaseRevision
      expect(Number.isInteger(before)).toBe(true)
      leaseOverride = before + 1
      expect(receipt.isCurrent()).toBe(false)
      controller.setContext({ solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [] })
      expect(controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] } })
      expect(getCapabilities).toHaveBeenCalledOnce()
      await controller.actions.loadCatalog()
      const next = controller.getState().projectCatalog
      expect(next.isCurrent()).toBe(true)
      controller.setContext({ solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [] })
      expect(controller.getState().projectCatalog).toBe(next)
      expect(getCapabilities).toHaveBeenCalledTimes(2)
      // A move of two keeps the parity, so a fingerprint that kept only part of the revision would miss it.
      leaseOverride = before + 3
      expect(next.isCurrent()).toBe(false)
      controller.setContext({ solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [] })
      expect(controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] } })
      expect(getCapabilities).toHaveBeenCalledTimes(2)
      // Every move ends the receipt, so a fingerprint that keeps only part of the revision (a remainder up to
      // twelve, a sign, a leading digit) misses at least one of these moves.
      let revision = before + 3
      let calls = 2
      for (const step of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 90, 900]) {
        await controller.actions.loadCatalog()
        calls += 1
        const held = controller.getState().projectCatalog
        expect(held.isCurrent()).toBe(true)
        revision += step
        leaseOverride = revision
        expect(held.isCurrent()).toBe(false)
        controller.setContext({ solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: [] })
        expect(controller.getState()).toMatchObject({ projectCatalog: null, catalog: { families: [] } })
        expect(getCapabilities).toHaveBeenCalledTimes(calls)
      }
    } finally { controller.destroy(); raw.dispose() }
  })

  it('R13A-54 a standalone destroy keeps the state object the base kept', async () => {
    const controller = createCatalogController({ services: { getTools: async () => [],
      getCapabilities: async () => ({ families: [], source: 'server' }), routePrompt: async () => null },
    adapters: { draftStorage: null }, context: { drawingId: 'drawing-1' } })
    await controller.actions.loadCatalog()
    const before = controller.getState()
    controller.destroy()
    expect(controller.getState()).toBe(before)
    expect(Object.hasOwn(controller.getState(), 'projectCatalog')).toBe(false)
  })
})
