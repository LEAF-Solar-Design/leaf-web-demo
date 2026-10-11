import { afterEach, describe, expect, it, vi } from 'vitest'
import { getCapabilities, runToolAsync } from './api.js'
import { solarFlowRunOutcome } from './solar/solarFlowModel.js'
import { SOLAR_SIZING_RUN_REASONS, solarSizingRunSentence } from './solar/solarSizingRunReasons.js'
import { createSolarProjectClient } from './solar/solarProjectClient.js'

const I = { schema_version: 1, source_intake_sha256: 'a'.repeat(64), units: { drawing_units: 'ft', wcs_to_ucs: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], elevation_datum: 'unknown', crs: null } }
const P = { expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: I }
const sizingAliases = [
  ["CLOUD_AUTH_MISSING","grant_missing","FORBIDDEN"],
  ["CLOUD_TENANT_UNAUTHORIZED","grant_scope","FORBIDDEN"],
  ["CLOUD_REQUEST_INVALID","request_invalid","BAD_PARAMS"],
  ["INVALID_SIZING_REQUEST","request_invalid","BAD_PARAMS"],
  ["INVALID_SIZING_MODE","request_invalid","BAD_PARAMS"],
  ["invalid_seed_request","request_invalid","BAD_PARAMS"],
  ["INVALID_COMMIT_REQUEST","request_invalid","BAD_PARAMS"],
  ["INVALID_NUMERIC_PARAM","request_invalid","BAD_PARAMS"],
  ["INVALID_PARENT_VERSION","request_invalid","BAD_PARAMS"],
  ["DRAWING_ID_CONFLICT","request_invalid","BAD_PARAMS"],
  ["INVALID_SEED_REQUEST","request_invalid","BAD_PARAMS"],
  ["JOB_IDENTITY_MISSING","request_invalid","BAD_PARAMS"],
  ["local_graph_commit_invalid","request_invalid","BAD_PARAMS"],
  ["invalid_solve_binding","request_invalid","BAD_PARAMS"],
  ["file_only_request_invalid","request_invalid","BAD_PARAMS"],
  ["GRAPH_LIMIT_EXCEEDED","request_invalid","BAD_PARAMS"],
  ["INVALID_JSON_OBJECT","request_invalid","BAD_PARAMS"],
  ["INVALID_JSON_STRING","request_invalid","BAD_PARAMS"],
  ["NONFINITE_NUMBER","request_invalid","BAD_PARAMS"],
  ["NUMBER_LIMIT_EXCEEDED","request_invalid","BAD_PARAMS"],
  ["INVALID_JSON_VALUE","request_invalid","BAD_PARAMS"],
  ["INVALID_SIZING_COVERAGE","coverage_invalid","BAD_PARAMS"],
  ["INVALID_ZONE_COVERAGE","coverage_invalid","BAD_PARAMS"],
  ["SIZING_MODEL_MISMATCH","models_mismatch","BAD_PARAMS"],
  ["SIZING_PROJECT_MISMATCH","project_mismatch","BAD_PARAMS"],
  ["CLOUD_UPSTREAM_FAILURE","cloud_unavailable","WORKITEM_FAILED"],
  ["CLOUD_RESPONSE_INVALID","cloud_response_invalid","WORKITEM_FAILED"],
  ["COLD_VOLTAGE_FAILED","cold_voltage","BAD_PARAMS"],
  ["STALE_GRAPH_REVISION","stale","BAD_PARAMS"],
  ["not_current_head","stale","BAD_PARAMS"],
  ["valid_settings_required","settings","BAD_PARAMS"],
  ["UNRESOLVED_UNITS","units","BAD_PARAMS"],
  ["MISSING_PANEL","panels","BAD_PARAMS"],
  ["SIZING_CONFIRMATION_REQUIRED","confirmation","BAD_PARAMS"],
  ["MODULE_POWER_REQUIRED","confirmation","BAD_PARAMS"],
  ["drawing_context_required","graph_unavailable","BAD_PARAMS"],
  ["invalid_drawing_context","graph_unavailable","BAD_PARAMS"],
  ["persisted_graph_unavailable","graph_unavailable","BAD_PARAMS"],
  ["licensed_graph_commit_required","graph_unavailable","BAD_PARAMS"],
  ["GRAPH_CONTEXT_UNAVAILABLE","graph_unavailable","BAD_PARAMS"],
  ["GRAPH_NOT_EMBEDDED","graph_unavailable","BAD_PARAMS"],
  ["LICENSED_GRAPH_COMMIT_REQUIRED","graph_unavailable","BAD_PARAMS"],
  ["GRAPH_DIGEST_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["PROJECT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["INVALID_GROUND_SLOTS","graph_invalid","BAD_PARAMS"],
  ["INVALID_GRAPH","graph_invalid","BAD_PARAMS"],
  ["UNSUPPORTED_SCHEMA_VERSION","graph_invalid","BAD_PARAMS"],
  ["UNKNOWN_UNITS","graph_invalid","BAD_PARAMS"],
  ["INVALID_GRAPH_SCHEMA","graph_invalid","BAD_PARAMS"],
  ["INVALID_REVISION_CHAIN","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_APPLICATION_ID","graph_invalid","BAD_PARAMS"],
  ["ID_KIND_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FUTURE_ENTITY_REVISION","graph_invalid","BAD_PARAMS"],
  ["FUTURE_SCHEDULE_REVISION","graph_invalid","BAD_PARAMS"],
  ["INVALID_SCHEDULE_COLUMNS","graph_invalid","BAD_PARAMS"],
  ["INSTALLATION_DESIGN_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["TRACKER_SLOT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DEGENERATE_TRACKER_AXIS","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_TRACKER_SOURCE","graph_invalid","BAD_PARAMS"],
  ["STRING_COUNT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_PANEL_MEMBERSHIP","graph_invalid","BAD_PARAMS"],
  ["PANEL_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FRAME_MEMBERSHIP_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["MISSING_ELECTRICAL_ZONE","graph_invalid","BAD_PARAMS"],
  ["INVALID_MATRIX_DIMENSIONS","graph_invalid","BAD_PARAMS"],
  ["INVALID_MATRIX_PANEL","graph_invalid","BAD_PARAMS"],
  ["MATRIX_CELL_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["MATRIX_SEQUENCE_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FRAME_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FRAME_SEQUENCE_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["INVERTER_CAPACITY_EXCEEDED","graph_invalid","BAD_PARAMS"],
  ["INVERTER_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_INVERTER_INPUT","graph_invalid","BAD_PARAMS"],
  ["EQUIPMENT_TYPE_REQUIRED","graph_invalid","BAD_PARAMS"],
  ["L2_MODE_REQUIRED","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_EQUIPMENT_NUMBER","graph_invalid","BAD_PARAMS"],
  ["L2_MIXED_INPUTS","graph_invalid","BAD_PARAMS"],
  ["L2_CAPACITY_EXCEEDED","graph_invalid","BAD_PARAMS"],
  ["L2_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_L2_INPUT","graph_invalid","BAD_PARAMS"],
  ["MATRIX_INPUT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["ROUTE_ENDPOINT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_FEEDER","graph_invalid","BAD_PARAMS"],
  ["ROUTE_PATHWAY_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["CHECKOUT_REQUIRED","checkout","FORBIDDEN"],
  ["CHECKOUT_DENIED","checkout","FORBIDDEN"],
  ["GRAPH_STORE_UNAVAILABLE","storage","INTERNAL"],
  ["GRAPH_COMMIT_READBACK_FAILED","commit_unconfirmed","INTERNAL"],
  ["INVALID_JOB_BINDING","commit_refused","BAD_PARAMS"],
  ["JOB_BINDING_REUSED","commit_refused","BAD_PARAMS"],
  ["STALE_GRAPH_COMPANION","commit_refused","BAD_PARAMS"],
  ["DRAWING_MUTATION_REFUSED","commit_refused","BAD_PARAMS"],
  ["GRAPH_COMMIT_REFUSED","commit_refused","BAD_PARAMS"],
  ["UNKNOWN_LOCAL_GRAPH_TOOL","commit_refused","BAD_PARAMS"],
  ["drawing_mutations_env_disabled","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_fence_closed","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_fence_unreadable","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_fence_lock_unavailable","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_refused_unattributed","commit_refused","APS_UNAVAILABLE"],
  ["GRAPH_COMMIT_CANCELLED","cancelled","BAD_PARAMS"],
  ["BROKER_UNREACHABLE","broker_unavailable","BROKER_UNREACHABLE"],
  ["TIMEOUT","timeout","TIMEOUT"],
  ["FORBIDDEN","access","FORBIDDEN"],
  ["UNAUTHENTICATED","access","UNAUTHENTICATED"],
  ["TENANT_DISABLED","access","TENANT_DISABLED"],
  ["ENTITLEMENT_REQUIRED","access","ENTITLEMENT_REQUIRED"],
  ["entitlement_required","access","FORBIDDEN"],
  ["quota_exceeded","quota","quota_exceeded"],
  ["INTERNAL","unknown","INTERNAL"],
  ["BAD_PARAMS","unknown","BAD_PARAMS"],
  ["UNKNOWN_TOOL","unknown","UNKNOWN_TOOL"],
  ["WORKITEM_FAILED","unknown","WORKITEM_FAILED"],
  ["APS_UNAVAILABLE","unknown","APS_UNAVAILABLE"],
]
const error = { error_code: 'bad_params', message: 'invalid_seed_parent', retryable: false }

const projectBinding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }

describe('project capabilities adapter boundary', () => {
  it('R7-38 adapter refuses a catalog success that settles after its scope changes', async () => {
    const fetch = stubResponse({ families: [] })
    const client = createSolarProjectClient({ fetchImpl: fetch })
    client.setScope(projectBinding, 'principal')
    const catalog = vi.fn(async () => {
      client.setScope(null, null)
      return { ok: true, status: 200, data: { families: [{ family_id: 'old', capabilities: [] }] } }
    })
    const projectClient = { ...client, catalog }
    try {
      await expect(getCapabilities(false, {
        solarBinding: projectBinding, authorityKey: 'principal', projectIntents: [],
      }, { projectClient })).rejects.toMatchObject({ reason: 'SIP_R7_SCOPE_STALE', status: null, body: null })
      expect(catalog).toHaveBeenCalledOnce()
      expect(fetch).not.toHaveBeenCalled()
    } finally { client.dispose() }
  })
  it('R7-54 a family read through the real client that signs out, or carries a custom prototype, never returns', async () => {
    const context = { solarBinding: projectBinding, authorityKey: 'principal', projectIntents: [] }
    let reads = 0
    for (const [prototype, reason] of [[Object.prototype, 'SIP_R7_SCOPE_STALE'], [{}, 'SIP_R7_RESPONSE_INVALID']]) {
      let client
      const family = Object.assign(Object.create(prototype), { capabilities: [] })
      Object.defineProperty(family, 'family_id', { enumerable: true, get() {
        reads += 1
        client.setScope(null, null)
        return 'old'
      } })
      const fetch = stubResponse({ families: [family] })
      client = createSolarProjectClient({ fetchImpl: fetch })
      client.setScope(projectBinding, 'principal')
      try {
        await expect(getCapabilities(false, context, { projectClient: client }))
          .rejects.toMatchObject({ reason, status: null, body: null })
        expect(fetch).toHaveBeenCalledOnce()
      } finally { client.dispose() }
    }
    // The plain family's getter runs once while the response is read; the custom prototype is refused unread.
    expect(reads).toBe(1)
  })
  it('R7-55 a scope change while the adapter normalizes families is refused by its second check', async () => {
    const fetch = stubResponse({ families: [] })
    const client = createSolarProjectClient({ fetchImpl: fetch })
    client.setScope(projectBinding, 'principal')
    const family = { capabilities: [] }
    Object.defineProperty(family, 'family_id', { enumerable: true, get() {
      client.setScope(null, null)
      return 'old'
    } })
    const catalog = vi.fn(async () => ({ ok: true, status: 200, data: { families: [family] } }))
    const projectClient = { ...client, catalog }
    try {
      await expect(getCapabilities(false, {
        solarBinding: projectBinding, authorityKey: 'principal', projectIntents: [],
      }, { projectClient })).rejects.toMatchObject({ reason: 'SIP_R7_SCOPE_STALE', status: null, body: null })
      expect(catalog).toHaveBeenCalledOnce()
      expect(client.getSnapshot().binding).toBeNull()
      expect(fetch).not.toHaveBeenCalled()
    } finally { client.dispose() }
  })
  it('R7-02 canonical catalog sends the full binding and never drawing_version', async () => {
    const fetch = stubResponse({ families: [] })
    const client = createSolarProjectClient({ fetchImpl: fetch })
    client.setScope(projectBinding, 'principal')
    const context = { solarBinding: projectBinding, authorityKey: 'principal', projectIntents: [] }
    await getCapabilities(false, context, { projectClient: client })
    expect(pathAndQuery(fetch.mock.calls[0][0])).toBe(`/api/capabilities?drawing_id=${projectBinding.drawing_id}&project_id=${projectBinding.project_id}&input_version_id=${projectBinding.input_version_id}`)
    expect(fetch.mock.calls[0][0]).not.toContain('drawing_version')
    const before = fetch.mock.calls.length
    await expect(getCapabilities(false, { ...context, solarBinding: null }, { projectClient: client })).rejects.toMatchObject({ reason: 'SIP_R7_BINDING_INVALID', status: null, body: null })
    await expect(getCapabilities(false, context)).rejects.toMatchObject({ reason: 'SIP_R7_BINDING_INVALID' })
    await expect(getCapabilities(false, { ...context, authorityKey: 'other' }, { projectClient: client })).rejects.toMatchObject({ reason: 'SIP_R7_BINDING_INVALID' })
    expect(fetch.mock.calls.length).toBe(before)
    client.dispose()
  })

  it('R7-34 standalone catalog and run carry no canonical project fields', async () => {
    const fetch = stubResponse({ families: [] })
    await getCapabilities(false, { drawing_id: 'standalone', drawing_version: 2 })
    stubResponse({ error: { ...error, error_code: 'BAD_PARAMS' } }, 409)
    const runFetch = globalThis.fetch
    await runToolAsync({ name: 'solar-settings' }, P, 'standalone', { dwgVersion: 2 })
    for (const [url, init] of [...fetch.mock.calls, ...runFetch.mock.calls]) {
      expect(url).not.toContain('project_id')
      expect(url).not.toContain('input_version_id')
      expect(url).not.toContain('project_runs')
      expect(init.headers).not.toHaveProperty('X-Org-Id')
      expect(init.headers).not.toHaveProperty('X-Project-Id')
      expect(init.body || '').not.toContain('project_id')
      expect(init.body || '').not.toContain('input_version_id')
    }
  })

  it('R7-36 legacy project_id without solarBinding preserves the existing request', async () => {
    const fetch = stubResponse({ families: [] })
    await getCapabilities(false, { drawing_id: 'd1', project_id: projectBinding.project_id, drawing_version: 3 })
    expect(fetch).toHaveBeenCalledOnce()
    expect(pathAndQuery(fetch.mock.calls[0][0])).toBe('/api/capabilities?drawing_id=d1&drawing_version=3')
    expect(fetch.mock.calls[0][1].headers).not.toHaveProperty('X-Project-Id')
  })
})

function stubResponse(body, status = 200) {
  const fetch = vi.fn().mockResolvedValue({ ok: status >= 200 && status < 300, status, json: async () => body })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

const pathAndQuery = (url) => {
  const parsed = new URL(url, 'https://example.test')
  return parsed.pathname + parsed.search
}

afterEach(() => vi.unstubAllGlobals())

describe('Solar settings API transport', () => {
  it.each(sizingAliases)('G1A-9 %s survives the sizing wrapper without ok as %s', async (reason_code, klass, error_code) => {
    for (const reason of new Set([reason_code, reason_code.toUpperCase()])) {
      stubResponse({ error: { ...error, error_code }, reason_code: reason }, 409)
      const envelope = await runToolAsync({ name: 'solar-size-strings' }, P, 'd1', {})
      expect(solarFlowRunOutcome(envelope)).toEqual({ ok: false, code: reason })
      expect(SOLAR_SIZING_RUN_REASONS).toHaveProperty(klass)
      expect(solarSizingRunSentence(solarFlowRunOutcome(envelope).code)).toBe(SOLAR_SIZING_RUN_REASONS[klass])
    }
  })

  it('G1A-9 keeps sizing refusal reasons without ok and preserves tool exclusions and malformed fallback', async () => {
    stubResponse({ error: { ...error, error_code: 'BAD_PARAMS' }, reason_code: 'valid_settings_required' }, 409)
    const sizing = await runToolAsync({ name: 'solar-size-strings' }, P, 'd1', {})
    expect(sizing.reason_code).toBe('valid_settings_required')
    expect(solarSizingRunSentence(solarFlowRunOutcome(sizing).code))
      .toBe('Complete valid Solar settings before sizing strings. Your inputs are kept.')
    stubResponse({ error, reason_code: 'valid_settings_required' }, 409)
    expect(await runToolAsync({ name: 'solar-panel-groups' }, P, 'd1', {})).not.toHaveProperty('reason_code')
    for (const tool of ['solar-settings', 'solar-size-strings']) {
      for (const reason_code of [null, 7, '', 'A'.repeat(65), 'bad code!', '__proto__']) {
        stubResponse({ error, reason_code }, 409)
        const envelope = await runToolAsync({ name: tool }, P, 'd1', {})
        expect(envelope).not.toHaveProperty('reason_code')
        expect(envelope.error).toEqual(error)
      }
    }
  })

  it('SF2 row20a the settings 409 keeps its reason_code', async () => {
    const fetch = stubResponse({ error, reason_code: 'invalid_seed_parent', degraded_mode: false }, 409)
    const envelope = await runToolAsync({ name: 'solar-settings' }, P, 'd1', {})
    expect(envelope).toMatchObject({ ok: false, tool: 'solar-settings', reason_code: 'invalid_seed_parent', error })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('SF2 row23 getCapabilities sends the drawing context', async () => {
    const fetch = stubResponse({ families: [] })
    await getCapabilities(false, { drawing_id: 'd1', drawing_version: 3 })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(pathAndQuery(fetch.mock.calls[0][0])).toBe('/api/capabilities?drawing_id=d1&drawing_version=3')
  })

  it('SF2 row24 getCapabilities without a drawing sends the bare path', async () => {
    const fetch = stubResponse({ families: [] })
    await getCapabilities(false)
    await getCapabilities(false, { drawing_id: '' })
    expect(fetch).toHaveBeenCalledTimes(2)
    for (const [url] of fetch.mock.calls) expect(pathAndQuery(url)).toBe('/api/capabilities')
  })

  it('SF2 row27 another tool 409 keeps the old envelope', async () => {
    stubResponse({ error, reason_code: 'invalid_seed_parent', degraded_mode: false }, 409)
    const envelope = await runToolAsync({ name: 'solar-panel-groups' }, P, 'd1', {})
    expect(envelope).toEqual({ ok: false, tool: 'solar-panel-groups', version: null, result: null, overlay: null,
      timing_ms: 0, cost: null, error, degraded_mode: false })
    expect(Object.hasOwn(envelope, 'reason_code')).toBe(false)
  })

  it('SF2 row28 a malformed reason_code is dropped', async () => {
    for (const [tool, reason_code] of ['solar-settings', 'solar-size-strings'].flatMap((tool) =>
      ['x'.repeat(65), 'bad code!', 7].map((reason) => [tool, reason]))) {
      stubResponse({ error, reason_code, degraded_mode: false }, 409)
      const envelope = await runToolAsync({ name: tool }, P, 'd1', {})
      expect(Object.hasOwn(envelope, 'reason_code')).toBe(false)
      expect(envelope.error).toEqual(error)
    }
  })
})
