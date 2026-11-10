import { describe, expect, it, vi } from 'vitest'
import { createCatalogController } from './createCatalogController.js'
import { createSolarProjectClient } from '../../solar/solarProjectClient.js'
import { getCapabilities } from '../../api.js'

const projectBinding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const otherBinding = { ...projectBinding, input_version_id: '55555555-5555-5555-5555-555555555555' }

// The server's project shape for non-Solar rows (solar_project_admission.project_drawing_tool_availability).
const ready = { entitled: true, engine_ready: true, implemented: true, input_ready: true,
  refusal_reasons: [], runnable: true, admission_checked: false, admission: null, project_read: true }
const noExecutor = { entitled: true, engine_ready: false, implemented: true, input_ready: false,
  refusal_reasons: ['project_adapter_unavailable'], runnable: false, admission_checked: false,
  admission: null, project_read: false }

function setup(countAvailability = ready) {
  const rows = [
    { name: 'count-by-layer', label: 'Count by layer', capabilities: ['drawing.read'], availability: countAvailability },
    { name: 'delete-marked-panel', label: 'Delete marked panel', capabilities: ['drawing.write'], availability: noExecutor },
  ]
  const fetchImpl = vi.fn(async () => ({ ok: true, status: 200,
    json: async () => ({ families: [{ family_id: 'drawing', capabilities: rows }] }) }))
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(projectBinding, 'session-1')
  const adapters = { commitDecision: vi.fn((decision) => decision), dismissDecision: vi.fn(), draftStorage: null }
  const services = { getTools: vi.fn(async () => rows), getCapabilities: vi.fn(getCapabilities),
    routePrompt: vi.fn(), projectSolarClient: client }
  // App passes this exact empty, frozen intent list in project mode.
  const controller = createCatalogController({ services, adapters, context: {
    solarBinding: projectBinding, authorityKey: 'session-1', projectIntents: Object.freeze([]),
    entitlements: { entitlements: { run_read: true, run_write: true } } } })
  const retire = () => { controller.destroy(); client.dispose() }
  return { controller, adapters, client, fetchImpl, retire }
}

describe('project drawing catalog reads', () => {
  it('a server-marked project read reaches review with no Solar intent', async () => {
    const run = setup()
    try {
      await run.controller.actions.loadCatalog()
      expect(run.controller.getState().projectCatalog).toBeTruthy()
      run.controller.actions.commitDecision({ lane: 'run', tool: 'count-by-layer', params: {} })
      expect(run.controller.getState().routeError).toBeNull()
      expect(run.adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: 'count-by-layer' }))
    } finally { run.retire() }
  })

  it('a drawing.write tool keeps its typed no-executor refusal', async () => {
    const run = setup()
    try {
      await run.controller.actions.loadCatalog()
      run.controller.actions.commitDecision({ lane: 'run', tool: 'delete-marked-panel', params: {} })
      expect(run.controller.getState().routeError).toBe('project_adapter_unavailable')
      expect(run.adapters.commitDecision).not.toHaveBeenCalled()
    } finally { run.retire() }
  })

  it('a Solar tool without its project intent stays refused', async () => {
    const run = setup()
    try {
      await run.controller.actions.loadCatalog()
      run.controller.actions.commitDecision({ lane: 'run', tool: 'solar-settings', params: {} })
      expect(run.adapters.commitDecision).not.toHaveBeenCalled()
      expect(run.controller.getState().routeError).toBe('project_request_required')
    } finally { run.retire() }
  })

  it('a stale project receipt refuses the read until the catalog reloads', async () => {
    const run = setup()
    try {
      await run.controller.actions.loadCatalog()
      run.client.setScope(otherBinding, 'session-1')
      run.controller.setContext({ solarBinding: otherBinding, authorityKey: 'session-1' })
      run.controller.actions.commitDecision({ lane: 'run', tool: 'count-by-layer', params: {} })
      expect(run.adapters.commitDecision).not.toHaveBeenCalled()
      expect(run.controller.getState().routeError).toBe('project_request_required')
    } finally { run.retire() }
  })

  it('an unentitled project read surfaces the server reason', async () => {
    const run = setup({ ...ready, entitled: false, runnable: false, refusal_reasons: ['entitlement_required'] })
    try {
      await run.controller.actions.loadCatalog()
      run.controller.actions.commitDecision({ lane: 'run', tool: 'count-by-layer', params: {} })
      expect(run.adapters.commitDecision).not.toHaveBeenCalled()
      expect(run.controller.getState().routeError).toBe('entitlement_required')
    } finally { run.retire() }
  })
})
