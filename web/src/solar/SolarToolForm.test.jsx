import React, { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarToolForm from './SolarToolForm.jsx'
import SolarPresetForm from './SolarPresetForm.jsx'
import declaration from '../../../server/solar_tools/solar_design_presets.json'
import listDeclaration from '../../../server/solar_tools/solar_design_presets_list.json'
import conversionDeclaration from '../../../server/solar_tools/solar_trackers_to_panel_groups.json'
import snapshot from '../../../docs/parity/evidence/ground/generate/profile-settings.json'
import writeTools from '../../../server/write_tools.json'
import SolarStepEditor from './SolarStepEditor.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'
import { SEED_SETTINGS } from './solarSettingsModel.js'
import { PROJECT_EQUIPMENT_TOOL, deriveProjectEquipmentState, buildProjectEquipmentRequest,
  exactProjectEquipmentPreview, projectSolarFlowReason, solarFlowStepId } from './solarFlowModel.js'

afterEach(cleanup)

const equipmentClients = []
afterEach(() => { cleanup(); for (const client of equipmentClients.splice(0)) client.dispose() })
const projectEquipmentBinding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const projectEquipmentNext = { ...projectEquipmentBinding, input_version_id: '66666666-6666-6666-6666-666666666666' }
const projectEquipmentDigest = 'sha256:' + 'd'.repeat(64)
const projectEquipmentJob = '55555555-5555-5555-5555-555555555555'
const projectEquipmentTool = { ...writeTools.tools.find((entry) => entry.name === PROJECT_EQUIPMENT_TOOL), label: 'Assign equipment',
  solar: { schema: 'leaf.solar-tool-view.v1', name: PROJECT_EQUIPMENT_TOOL, family: 'stringing',
    wave: 1, order: 70, entitlement: 'run_write', interaction: { mode: 'form' } } }
function projectEquipmentIntake(rev = 7) {
  return { solar_design_graph: { rev, settings: { ...SEED_SETTINGS },
    project: { name: 'Roof', zip_code: '44224', latitude: null, longitude: null } }, solar_design_graph_sha256: 'b'.repeat(64) }
}
function projectEquipmentContext(binding = projectEquipmentBinding, intake = projectEquipmentIntake(), changes = {}) {
  return { context: { organization_id: binding.organization_id, project_id: binding.project_id, drawing_id: binding.drawing_id,
    input_version_id: binding.input_version_id, head_version_id: binding.input_version_id, is_head: true,
    intake_sha256: 'a'.repeat(64), ...changes }, intake }
}
function projectEquipmentPreviewRow(changes = {}, availability = {}) {
  return { name: PROJECT_EQUIPMENT_TOOL, catalog_digest: projectEquipmentDigest,
    availability: { admission_checked: true, admission: { status_code: 200, error: null },
      entitled: true, engine_ready: true, implemented: true, input_ready: true, runnable: true,
      refusal_reasons: [], ...availability }, ...changes }
}
const projectEquipmentPreview = (rows = [projectEquipmentPreviewRow()]) => ({ families: [{ capabilities: rows }] })
const equipmentResponse = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data })
function equipmentDeferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function equipmentHarness(overrides = {}) {
  const h = { contextResponses: [], previewResponses: [], submitResponses: [], intake: projectEquipmentIntake(),
    contextChanges: {}, preview: projectEquipmentPreview(), run: { job_id: projectEquipmentJob, status: 'submitted' }, ...overrides }
  h.fetchImpl = vi.fn(async (url, options) => {
    const binding = h.real.getSnapshot().binding
    if (url.includes('/context?')) return h.contextResponses.shift() ?? equipmentResponse(projectEquipmentContext(binding, h.intake, h.contextChanges))
    if (url.includes('/api/capabilities?')) return h.previewResponses.shift() ?? equipmentResponse(h.preview)
    if (url.endsWith('/api/run')) return h.submitResponses.shift() ?? equipmentResponse(h.run, 202)
    if (url.endsWith('/checkout')) {
      const identity = { organization_id: binding.organization_id, project_id: binding.project_id, drawing_id: binding.drawing_id }
      if (options.method === 'DELETE') return equipmentResponse({ ...identity, released: true, checkout: null })
      return equipmentResponse({ ...identity, acquired: true, checkout: { holder: 'synthetic editor',
        acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z', fence: '1' },
        checkout_capability: 'lco1.' + 'c'.repeat(64) })
    }
    throw new Error('Unexpected equipment request')
  })
  h.real = createSolarProjectClient({ fetchImpl: h.fetchImpl, authHeaders: () => ({ Authorization: 'Bearer synthetic' }) })
  h.real.setScope(projectEquipmentBinding, 'epoch-1')
  equipmentClients.push(h.real)
  h.client = { ...h.real }
  for (const name of ['readContext', 'prepareRun', 'catalog', 'submitRun', 'captureCatalogScope', 'setScope', 'dispose', 'acquireCheckout', 'releaseCheckout']) {
    h.client[name] = vi.fn((...args) => h.real[name](...args))
  }
  h.unsubscribes = []
  h.client.subscribe = vi.fn((listener) => {
    const unsubscribe = vi.fn(h.real.subscribe(listener))
    h.unsubscribes.push(unsubscribe)
    return unsubscribe
  })
  let sequence = 0
  h.props = { tool: projectEquipmentTool, solarBinding: projectEquipmentBinding, authorityKey: 'epoch-1', projectClient: h.client,
    catalogDigest: projectEquipmentDigest, newProjectIntentKey: vi.fn(() => 'equipment-intent-' + ++sequence),
    onProjectSubmit: vi.fn((intent, options) => h.real.submitRun(intent, { signal: options.signal })),
    readIntake: vi.fn(), onSubmit: vi.fn(), onClose: vi.fn() }
  return h
}
const equipmentPreviewCalls = (h) => h.fetchImpl.mock.calls.filter(([url]) => url.includes('/api/capabilities?'))
const equipmentRunCalls = (h) => h.fetchImpl.mock.calls.filter(([url]) => url.endsWith('/api/run'))
const equipmentReview = () => screen.getByRole('button', { name: 'Review & run' })
const equipmentRefresh = () => fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }))
const equipmentChange = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const equipmentReason = () => screen.getByTestId('project-solar-equipment-reason').textContent
async function equipmentReady() { await waitFor(() => expect(equipmentReview().disabled).toBe(false)) }
async function equipmentSettle() { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
async function equipmentMount(h = equipmentHarness()) {
  const view = render(<SolarToolForm {...h.props} />)
  await equipmentReady()
  return { h, view }
}
function equipmentRebind(view, h, changes) {
  h.props = { ...h.props, ...changes }
  h.real.setScope(h.props.solarBinding, h.props.authorityKey)
  view.rerender(<SolarToolForm {...h.props} />)
}

it('R11-05 invalid or absent project bindings refuse before any legacy or client read', async () => {
  const accessor = { ...projectEquipmentBinding }
  const get = vi.fn(() => projectEquipmentBinding.input_version_id)
  Object.defineProperty(accessor, 'input_version_id', { enumerable: true, get })
  const missing = { ...projectEquipmentBinding }
  delete missing.drawing_id
  for (const solarBinding of [undefined, null, missing, accessor, { ...projectEquipmentBinding, extra: true },
    { ...projectEquipmentBinding, input_version_id: 3 }]) {
    const h = equipmentHarness()
    render(<SolarToolForm {...h.props} solarBinding={solarBinding} />)
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason('binding_invalid')))
    expect(h.fetchImpl).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(h.client.prepareRun).not.toHaveBeenCalled()
    cleanup()
  }
  const h = equipmentHarness()
  const props = { ...h.props, projectId: projectEquipmentBinding.project_id }
  delete props.solarBinding
  render(<SolarToolForm {...props} />)
  expect(equipmentReason()).toBe(projectSolarFlowReason('binding_invalid'))
  expect(h.fetchImpl).not.toHaveBeenCalled()
  expect(get).not.toHaveBeenCalled()
})

it('R11-06 missing project dependencies refuse without legacy handoff', async () => {
  for (const key of ['projectClient', 'newProjectIntentKey', 'onProjectSubmit']) {
    const h = equipmentHarness()
    render(<SolarToolForm {...h.props} {...{ [key]: undefined }} />)
    expect(equipmentReason()).toBe(projectSolarFlowReason('submit_unavailable'))
    expect(h.fetchImpl).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    cleanup()
  }
  const h = equipmentHarness()
  h.real.setScope(projectEquipmentNext, 'epoch-1')
  render(<SolarToolForm {...h.props} />)
  await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason('binding_invalid')))
  expect(h.fetchImpl).not.toHaveBeenCalled()
})

it('R11-07 foreign or inconsistent canonical context cannot create a candidate', async () => {
  for (const contextChanges of [{ project_id: projectEquipmentNext.input_version_id },
    { input_version_id: projectEquipmentNext.input_version_id }, { organization_id: projectEquipmentNext.input_version_id },
    { drawing_id: projectEquipmentNext.input_version_id }, { head_version_id: projectEquipmentNext.input_version_id, is_head: true },
    { is_head: 'true' }]) {
    const h = equipmentHarness({ contextChanges })
    render(<SolarToolForm {...h.props} />)
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason('context_unreadable')))
    expect(h.client.prepareRun).not.toHaveBeenCalled()
    expect(equipmentPreviewCalls(h)).toHaveLength(0)
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    cleanup()
  }
})

it('R11-08 historical project context requires the head before preview', async () => {
  const h = equipmentHarness({ contextChanges: { head_version_id: projectEquipmentNext.input_version_id, is_head: false } })
  render(<SolarToolForm {...h.props} />)
  await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason('not_current_head')))
  expect(equipmentPreviewCalls(h)).toHaveLength(0)
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
})

it('R11-09 graphless broken companions and unsafe revisions cannot seed equipment', async () => {
  const broken = projectEquipmentIntake()
  delete broken.solar_design_graph_sha256
  for (const [intake, reason] of [[{}, 'graphless'], [broken, 'graph_unreadable'],
    [projectEquipmentIntake(Number.MAX_SAFE_INTEGER + 1), 'graph_unreadable'], [projectEquipmentIntake(-1), 'graph_unreadable']]) {
    const h = equipmentHarness({ intake })
    render(<SolarToolForm {...h.props} />)
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason(reason)))
    expect(equipmentPreviewCalls(h)).toHaveLength(0)
    expect(h.client.prepareRun).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    cleanup()
  }
})

it('R11-10 digest and UTF-8 intent key validation precede equipment preview', async () => {
  for (const [change, reason] of [[{ catalogDigest: undefined }, 'catalog_digest_required'],
    [{ catalogDigest: 'sha256:bad' }, 'catalog_digest_required'],
    [{ newProjectIntentKey: vi.fn(() => ' ') }, 'idempotency_key_required'],
    [{ newProjectIntentKey: vi.fn(() => 'é'.repeat(129)) }, 'idempotency_key_required']]) {
    const h = equipmentHarness()
    render(<SolarToolForm {...h.props} {...change} />)
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason(reason)))
    expect(equipmentPreviewCalls(h)).toHaveLength(0)
    expect(h.client.prepareRun).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    cleanup()
  }
})

it('R11-11 both project hosts preview and hand off one frozen R7 intent on the canonical wire', async () => {
  for (const surface of ['tool', 'step']) {
    const h = equipmentHarness()
    await h.real.acquireCheckout()
    const view = render(surface === 'tool' ? <SolarToolForm {...h.props} />
      : <SolarStepEditor {...h.props} row={projectEquipmentTool} retained={{ expected_rev: 999 }} status="failed" failureCode="LEGACY" />)
    await equipmentReady()
    expect(screen.getByText('Graph revision 7')).toBeTruthy()
    expect(screen.queryByLabelText('Expected rev')).toBeNull()
    expect(screen.queryByLabelText('Drawing id')).toBeNull()
    expect(screen.getByLabelText('Equipment').value).toBe('')
    for (const label of ['Equipment', 'Assignments', 'Cancel', 'Preview']) expect(screen.getByLabelText(label)).toBeTruthy()
    equipmentChange('Equipment', '[]'); equipmentChange('Assignments', '[]')
    await equipmentReady()
    const intent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
    expect(Object.keys(intent)).toEqual(['binding', 'authorityKey', 'tool', 'params', 'catalogDigest', 'idempotencyKey'])
    expect(Object.isFrozen(intent)).toBe(true)
    expect(Object.isFrozen(intent.params)).toBe(true)
    expect(intent.params).toEqual({ drawing_id: projectEquipmentBinding.drawing_id, expected_rev: 7, equipment: [], assignments: [] })
    const [url, options] = equipmentPreviewCalls(h).at(-1)
    const b = projectEquipmentBinding
    const query = `drawing_id=${b.drawing_id}&project_id=${b.project_id}&input_version_id=${b.input_version_id}`
    const packed = JSON.stringify({ [PROJECT_EQUIPMENT_TOOL]: { params: intent.params,
      catalog_digest: projectEquipmentDigest, idempotency_key: intent.idempotencyKey } })
    expect(url).toBe('/api/capabilities?' + query + '&project_runs=' + encodeURIComponent(packed))
    expect(options.headers).toMatchObject({ Authorization: 'Bearer synthetic', 'X-Org-Id': b.organization_id,
      'X-Tenant-Id': b.organization_id, 'X-Project-Id': b.project_id, 'X-Checkout-Capability': 'lco1.' + 'c'.repeat(64) })
    expect(h.fetchImpl.mock.calls.find(([path]) => path.includes('/context?'))[0])
      .toBe(`/api/projects/${b.project_id}/drawing-versions/${b.input_version_id}/context?drawing_id=${b.drawing_id}`)
    fireEvent.click(equipmentReview())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    const handoff = h.props.onProjectSubmit.mock.calls[0][1]
    expect(handoff.binding).toEqual(b)
    expect(handoff.authorityKey).toBe('epoch-1')
    expect(handoff.signal.aborted).toBe(false)
    expect(handoff.isCurrent()).toBe(true)
    await waitFor(() => expect(screen.getByTestId('project-solar-equipment-feedback').textContent).toBe('Equipment request submitted.'))
    const [, runOptions] = equipmentRunCalls(h)[0]
    expect(JSON.parse(runOptions.body)).toEqual({ tool: PROJECT_EQUIPMENT_TOOL, params: intent.params, dwg: b.input_version_id, catalog_digest: projectEquipmentDigest })
    expect(runOptions.headers['Idempotency-Key']).toBe(intent.idempotencyKey)
    expect(runOptions.headers['X-Checkout-Capability']).toBe('lco1.' + 'c'.repeat(64))
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(h.props.onClose).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    view.unmount()
  }
})

it('R11-12 successful HTTP catalogs must satisfy every exact equipment admission condition', async () => {
  const omitted = { status_code: 200 }
  const badRows = [[], [projectEquipmentPreviewRow({ catalog_digest: 'sha256:' + 'e'.repeat(64) })],
    [projectEquipmentPreviewRow({}, { admission_checked: false })], [projectEquipmentPreviewRow({}, { admission: omitted })],
    [projectEquipmentPreviewRow({}, { refusal_reasons: ['project_request_required'] })],
    [projectEquipmentPreviewRow(), projectEquipmentPreviewRow({}, { admission: { status_code: 409, error: { reason_code: 'INVALID_EQUIPMENT_CAPACITY' } } })],
    ...['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].map((key) => [projectEquipmentPreviewRow({}, { [key]: false })])]
  for (const rows of badRows) {
    const h = equipmentHarness({ preview: projectEquipmentPreview(rows) })
    render(<SolarToolForm {...h.props} />)
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason('preview_refused')))
    expect(equipmentReview().disabled).toBe(true)
    fireEvent.click(equipmentReview())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    const outcome = await h.client.catalog.mock.results[0].value
    expect(outcome.ok).toBe(true)
    expect(outcome.status).toBe(200)
    expect(exactProjectEquipmentPreview(outcome, projectEquipmentDigest).accepted).toBe(false)
    cleanup()
  }
})

const presetTool = {
  name: 'solar-design-presets', label: 'Design presets', capabilities: ['drawing.write'],
  solar: { schema: 'leaf.solar-tool-view.v1', name: 'solar-design-presets', family: 'settings',
    wave: 2, order: 12, entitlement: 'run_write', interaction: { mode: 'form' } },
  params: declaration.record.params,
}
const SNAP = Object.fromEntries(Object.keys(declaration.record.params.properties.current_settings.properties).map((key) => [key, snapshot[key]]))
const row = (n, name, value) => ({ id: { entity_id: n }, type: 'report', quantity: 1, unit: 'each', name, value })
const LIST_TWO = { schema: 'leaf.solar-design-presets.v1', active_prefix: 'B', current_settings: { ...SNAP }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'B Beta'), row('report-profiles', 'profiles', 2)] }


const tool = {
  name: 'solar-extra', label: 'Solar inputs', capabilities: ['drawing.write'],
  solar: {
    schema: 'leaf.solar-tool-view.v1', name: 'solar-extra', family: 'stringing',
    wave: 1, order: 10, entitlement: 'run_write', interaction: { mode: 'form' },
  },
  params: { type: 'object', properties: {
    drawing_id: { type: 'string', default: 'must-not-submit' },
    expected_rev: { type: 'integer', default: 1 },
    cancel: { type: 'boolean', default: false },
    changes: { type: 'object', default: {} },
    initialize: { type: ['object', 'null'], default: null },
  } },
}

it('R11-16 edits digests and ABA scope changes synchronously invalidate old preview success and failure', async () => {
  for (const settlement of ['success', 'failure']) {
    const late = equipmentDeferred()
    const h = equipmentHarness({ previewResponses: [late.promise] })
    const view = render(<SolarToolForm {...h.props} />)
    await waitFor(() => expect(equipmentPreviewCalls(h)).toHaveLength(1))
    const signal = h.client.catalog.mock.calls[0][0].signal
    equipmentChange('Equipment', '[{"id":""}]')
    expect(signal.aborted).toBe(true)
    await equipmentReady()
    const draftIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
    const validCalls = equipmentPreviewCalls(h).length
    equipmentChange('Equipment', '[')
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason('invalid_value')))
    expect(equipmentReview().disabled).toBe(true)
    expect(equipmentPreviewCalls(h)).toHaveLength(validCalls)
    fireEvent.click(equipmentReview())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    equipmentChange('Equipment', '[{"id":""}]')
    await equipmentReady()
    const factory = vi.fn(() => 'replacement-key')
    h.props = { ...h.props, newProjectIntentKey: factory }
    const calls = equipmentPreviewCalls(h).length
    view.rerender(<SolarToolForm {...h.props} />)
    await equipmentSettle()
    expect(factory).not.toHaveBeenCalled()
    expect(equipmentPreviewCalls(h)).toHaveLength(calls)
    const digestLate = equipmentDeferred()
    h.previewResponses.push(digestLate.promise)
    h.props = { ...h.props, catalogDigest: 'sha256:' + 'e'.repeat(64) }
    view.rerender(<SolarToolForm {...h.props} />)
    expect(equipmentReview().disabled).toBe(true)
    await waitFor(() => expect(equipmentPreviewCalls(h)).toHaveLength(calls + 1))
    const digestSignal = h.client.catalog.mock.calls.at(-1)[0].signal
    equipmentRebind(view, h, { solarBinding: projectEquipmentNext })
    equipmentRebind(view, h, { solarBinding: projectEquipmentBinding, catalogDigest: projectEquipmentDigest })
    await equipmentReady()
    expect(digestSignal.aborted).toBe(true)
    expect(screen.getByLabelText('Equipment').value).toBe('')
    expect(h.client.catalog.mock.calls.at(-1)[0].intents[0]).not.toBe(draftIntent)
    await act(async () => {
      if (settlement === 'success') late.resolve(equipmentResponse(projectEquipmentPreview()))
      else late.reject(new Error('late old request'))
      digestLate.resolve(equipmentResponse(projectEquipmentPreview()))
    })
    expect(screen.queryByTestId('project-solar-equipment-feedback')).toBeNull()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    const authorityLate = equipmentDeferred()
    h.previewResponses.push(authorityLate.promise)
    equipmentRefresh()
    await waitFor(() => expect(h.client.catalog.mock.calls.at(-1)[0].signal.aborted).toBe(false))
    const oldAuthority = h.client.catalog.mock.calls.at(-1)[0].signal
    equipmentRebind(view, h, { authorityKey: 'epoch-2' })
    await equipmentReady()
    expect(oldAuthority.aborted).toBe(true)
    const replacement = equipmentHarness()
    view.rerender(<SolarToolForm {...replacement.props} />)
    await equipmentReady()
    await act(async () => authorityLate.reject(new Error('old authority')))
    expect(screen.queryByTestId('project-solar-equipment-feedback')).toBeNull()
    expect(h.unsubscribes.every((unsubscribe) => unsubscribe.mock.calls.length === 1)).toBe(true)
    view.unmount()
  }
})

it('R11-17 checkout rotation aborts and restarts an unsettled initial context read', async () => {
  const late = equipmentDeferred()
  const h = equipmentHarness({ contextResponses: [late.promise] })
  render(<SolarToolForm {...h.props} />)
  await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1))
  const signal = h.client.readContext.mock.calls[0][0].signal
  await act(async () => { await h.real.acquireCheckout() })
  await equipmentReady()
  expect(signal.aborted).toBe(true)
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  await act(async () => late.resolve(equipmentResponse(projectEquipmentContext(projectEquipmentBinding, projectEquipmentIntake(99)))))
  expect(screen.getByText('Graph revision 7')).toBeTruthy()
  expect(screen.queryByText('Graph revision 99')).toBeNull()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
})

it('R11-30 a client scope that leaves and returns during the initial read restarts it and settles', async () => {
  const late = equipmentDeferred()
  const h = equipmentHarness({ contextResponses: [late.promise] })
  render(<SolarToolForm {...h.props} />)
  await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1))
  const signal = h.client.readContext.mock.calls[0][0].signal
  act(() => {
    h.real.setScope(projectEquipmentNext, 'epoch-1')
    h.real.setScope(projectEquipmentBinding, 'epoch-1')
  })
  expect(signal.aborted).toBe(true)
  await equipmentReady()
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  await act(async () => late.resolve(equipmentResponse(projectEquipmentContext(projectEquipmentBinding, projectEquipmentIntake(99)))))
  expect(screen.getByText('Graph revision 7')).toBeTruthy()
  expect(screen.queryByText('Graph revision 99')).toBeNull()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
  // Once the context has settled a further leave and return restarts nothing.
  act(() => {
    h.real.setScope(projectEquipmentNext, 'epoch-1')
    h.real.setScope(projectEquipmentBinding, 'epoch-1')
  })
  await equipmentSettle()
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
})

it('R11-18 checkout rotation disarms and previews the same frozen candidate and intent key', async () => {
  const { h } = await equipmentMount()
  const intent = h.client.catalog.mock.calls[0][0].intents[0]
  const late = equipmentDeferred()
  h.previewResponses.push(late.promise)
  await act(async () => { await h.real.acquireCheckout() })
  await waitFor(() => expect(equipmentPreviewCalls(h)).toHaveLength(2))
  expect(equipmentReview().disabled).toBe(true)
  expect(h.client.catalog.mock.calls[1][0].intents[0]).toBe(intent)
  expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
  fireEvent.click(equipmentReview())
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
  await act(async () => late.resolve(equipmentResponse(projectEquipmentPreview())))
  await equipmentReady()
  expect(equipmentPreviewCalls(h)[1][1].headers['X-Checkout-Capability']).toBe('lco1.' + 'c'.repeat(64))
})

it('R11-19 final context reread refuses a moved head digest or revision without retargeting', async () => {
  for (const reason of ['head', 'digest', 'rev']) {
    const { h, view } = await equipmentMount()
    const candidate = h.client.catalog.mock.calls[0][0].intents[0]
    if (reason === 'head') h.contextChanges = { head_version_id: projectEquipmentNext.input_version_id, is_head: false }
    if (reason === 'digest') h.contextChanges = { intake_sha256: 'f'.repeat(64) }
    if (reason === 'rev') h.intake = projectEquipmentIntake(8)
    fireEvent.click(equipmentReview())
    await waitFor(() => expect(equipmentReason()).toBe(projectSolarFlowReason(reason === 'head' ? 'not_current_head' : 'scope_stale')))
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(equipmentRunCalls(h)).toHaveLength(0)
    expect(h.client.prepareRun).toHaveBeenCalledTimes(1)
    expect(candidate.params.expected_rev).toBe(7)
    view.unmount()
  }
})

it('R11-20 same tick submission locks survive unrelated renders until callback settlement', async () => {
  const pending = equipmentDeferred()
  const h = equipmentHarness()
  h.props.onProjectSubmit = vi.fn(() => pending.promise)
  const { view } = await equipmentMount(h)
  const run = equipmentReview()
  act(() => { fireEvent.click(run); fireEvent.click(run) })
  await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
  const intent = h.props.onProjectSubmit.mock.calls[0][0]
  view.rerender(<SolarToolForm {...h.props} onClose={vi.fn()} />)
  expect(equipmentReview().disabled).toBe(true)
  fireEvent.click(equipmentReview())
  expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  await act(async () => pending.resolve({ ok: true, status: 202, data: { status: 'submitted', job_id: projectEquipmentJob } }))
  await equipmentReady()
  expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
  expect(h.props.onClose).not.toHaveBeenCalled()
})

it('R11-21 uncertain submission waits for explicit retry and keeps candidate identity across refresh', async () => {
  const { h } = await equipmentMount()
  h.client.submitRun.mockImplementationOnce(async () => ({ ok: false, status: null, reason: 'SIP_R7_TIMEOUT', envelope: null }))
  h.props.onProjectSubmit.mockImplementation((intent, options) => h.client.submitRun(intent, { signal: options.signal }))
  fireEvent.click(equipmentReview())
  await waitFor(() => expect(screen.getByTestId('project-solar-equipment-feedback').textContent).toContain('SIP_R7_TIMEOUT'))
  await equipmentSettle()
  expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
  const intent = h.props.onProjectSubmit.mock.calls[0][0]
  expect(screen.getByRole('button', { name: 'Retry' }).disabled).toBe(false)
  equipmentRefresh()
  await equipmentReady()
  expect(h.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(intent)
  expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Retry' }).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2))
  expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(intent)
  await waitFor(() => expect(screen.getByTestId('project-solar-equipment-feedback').textContent).toBe('Equipment request submitted.'))
  expect(h.props.onClose).not.toHaveBeenCalled()
})

it('R11-22 a current 202 reports submission without claiming applied finished or ready', async () => {
  const { h } = await equipmentMount()
  fireEvent.click(equipmentReview())
  await waitFor(() => expect(screen.getByTestId('project-solar-equipment-feedback').textContent).toBe('Equipment request submitted.'))
  expect(screen.getByTestId('project-solar-equipment-form').getAttribute('data-mode')).toBe('edit')
  expect(h.props.onClose).not.toHaveBeenCalled()
  expect(h.props.onSubmit).not.toHaveBeenCalled()
  expect(h.client.submitRun).not.toHaveBeenCalled()
  expect(equipmentRunCalls(h)).toHaveLength(1)
  for (const text of ['Equipment applied', 'Equipment finished', 'Equipment ready']) expect(screen.queryByText(text)).toBeNull()
})

it('R11-23 feedback keeps exact outcomes and safe nested outer and fallback codes without server prose', async () => {
  const nested = { status_code: 409, error: { reason_code: 'INVALID_EQUIPMENT_CAPACITY', message: 'private nested prose' } }
  const h = equipmentHarness({ preview: projectEquipmentPreview([projectEquipmentPreviewRow({}, { admission: nested, runnable: false })]) })
  render(<SolarToolForm {...h.props} />)
  await waitFor(() => expect(screen.getByTestId('project-solar-equipment-feedback').textContent)
    .toBe(projectSolarFlowReason('preview_refused') + ' (INVALID_EQUIPMENT_CAPACITY)'))
  const preview = await h.client.catalog.mock.results[0].value
  expect(exactProjectEquipmentPreview(preview, projectEquipmentDigest).admission).toEqual(nested)
  expect(preview.status).toBe(200)
  expect(screen.getByTestId('project-solar-equipment-form').textContent).not.toContain('private nested prose')
  cleanup()
  for (const [envelope, status, code] of [[{ error: { reason_code: 'CHECKOUT_EXPIRED', message: 'private transport prose' } }, 409, 'CHECKOUT_EXPIRED'],
    [{ error: { message: 'private missing reason' } }, 409, 'HTTP_ERROR'],
    [{ error: { reason_code: 'bad code <secret>', message: 'private unsafe reason' } }, 403, 'HTTP_ERROR']]) {
    const next = equipmentHarness({ previewResponses: [equipmentResponse(envelope, status)] })
    render(<SolarToolForm {...next.props} />)
    await waitFor(() => expect(screen.getByTestId('project-solar-equipment-feedback').textContent)
      .toBe(projectSolarFlowReason('preview_refused') + ` (${code})`))
    const outcome = await next.client.catalog.mock.results[0].value
    expect(outcome.status).toBe(status)
    expect(outcome.reason).toBe(code)
    expect(outcome.envelope).toEqual(envelope)
    expect(screen.getByTestId('project-solar-equipment-form').textContent).not.toContain('private')
    expect(screen.getByTestId('project-solar-equipment-form').textContent).not.toContain('<secret>')
    expect(next.props.onProjectSubmit).not.toHaveBeenCalled()
    cleanup()
  }
})

it('R11-24 empty equipment IDs and array order survive preparation preview and identical explicit retry', async () => {
  const { h } = await equipmentMount()
  const equipment = [{ id: '', number: 2, model: 'second' }, { id: '', number: 1, model: 'first' }]
  const assignments = [{ string_ref: 's2', inverter_ref: '', mppt_letter: 'B', input_number: 0 },
    { string_ref: 's1', inverter_ref: '', mppt_letter: 'A', input_number: 1 }]
  equipmentChange('Equipment', JSON.stringify(equipment))
  equipmentChange('Assignments', JSON.stringify(assignments))
  await equipmentReady()
  const intent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
  expect(intent.params.equipment).toEqual(equipment)
  expect(intent.params.assignments).toEqual(assignments)
  const state = deriveProjectEquipmentState({ binding: projectEquipmentBinding, contextView: projectEquipmentContext() })
  const build = (values) => buildProjectEquipmentRequest({ binding: projectEquipmentBinding, state, values,
    catalogDigest: projectEquipmentDigest, idempotencyKey: 'key' })
  expect(build({ equipment, assignments, cancel: false, preview: false }).request.params)
    .toEqual({ drawing_id: projectEquipmentBinding.drawing_id, expected_rev: 7, equipment, assignments, cancel: false, preview: false })
  const accessor = {}
  const get = vi.fn(() => equipment)
  Object.defineProperty(accessor, 'equipment', { enumerable: true, get })
  const circular = []; circular.push(circular)
  for (const values of [{ expected_rev: 99 }, { drawing_id: 'foreign' }, { project_id: 'foreign' },
    { equipment: [undefined] }, { equipment: circular }, { equipment: [NaN] }, accessor]) {
    expect(build(values)).toEqual({ ok: false, reason: 'request_invalid' })
  }
  expect(get).not.toHaveBeenCalled()
  h.props.onProjectSubmit.mockResolvedValue({ ok: false, status: null, reason: 'SIP_R7_TIMEOUT', envelope: null })
  fireEvent.click(equipmentReview())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' }).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2))
  expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
  expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(intent)
  const packed = JSON.parse(new URL('https://synthetic.invalid' + equipmentPreviewCalls(h).at(-1)[0]).searchParams.get('project_runs'))
  expect(packed[PROJECT_EQUIPMENT_TOOL].params.equipment).toEqual(equipment)
  expect(Object.keys(packed[PROJECT_EQUIPMENT_TOOL].params).sort()).toEqual(['assignments', 'drawing_id', 'equipment', 'expected_rev'])
  // This is transport preservation; no server creation or execution is inferred from the fixture.
})

it('R11-26 StrictMode unmount and late settlement abort and unsubscribe without managing the shared client', async () => {
  for (const stage of ['context', 'preview', 'submit']) {
    const late = equipmentDeferred()
    const h = equipmentHarness(stage === 'context' ? { contextResponses: [late.promise] }
      : stage === 'preview' ? { previewResponses: [late.promise] } : {})
    if (stage === 'submit') h.props.onProjectSubmit.mockImplementation(() => late.promise)
    const view = render(<StrictMode><SolarToolForm {...h.props} /></StrictMode>)
    if (stage === 'submit') { await equipmentReady(); fireEvent.click(equipmentReview()); await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)) }
    else await waitFor(() => expect(stage === 'context' ? h.client.readContext.mock.calls.length : h.client.catalog.mock.calls.length).toBeGreaterThan(0))
    const calls = stage === 'context' ? h.client.readContext.mock.calls : stage === 'preview' ? h.client.catalog.mock.calls : h.props.onProjectSubmit.mock.calls.map((call) => [call[1]])
    view.unmount()
    expect(calls.every(([options]) => options.signal.aborted)).toBe(true)
    await act(async () => late.resolve(stage === 'submit' ? { ok: true, status: 202, data: { status: 'submitted', job_id: projectEquipmentJob } }
      : equipmentResponse(stage === 'context' ? projectEquipmentContext() : projectEquipmentPreview())))
    expect(h.unsubscribes.every((unsubscribe) => unsubscribe.mock.calls.length === 1)).toBe(true)
    for (const name of ['setScope', 'dispose', 'acquireCheckout', 'releaseCheckout']) expect(h.client[name]).not.toHaveBeenCalled()
    expect(h.props.onClose).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
  }
  for (const surface of ['tool', 'step']) {
    const h = equipmentHarness()
    const view = render(<>
      <button type="button" data-tool={PROJECT_EQUIPMENT_TOOL} id={solarFlowStepId(PROJECT_EQUIPMENT_TOOL)}>Equipment opener</button>
      {surface === 'tool' ? <SolarToolForm {...h.props} /> : <SolarStepEditor {...h.props} row={projectEquipmentTool} />}
    </>)
    await equipmentReady()
    expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(h.props.onClose).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Equipment opener' }))
    screen.getByLabelText('Equipment').focus()
    fireEvent.keyDown(screen.getByLabelText('Equipment'), { key: 'Escape' })
    expect(h.props.onClose).toHaveBeenCalledTimes(2)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Equipment opener' }))
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    view.unmount()
  }
})

it('R11-28 project presets and other generic tools refuse without legacy reads or submissions', async () => {
  for (const host of ['tool', 'step']) {
    for (const name of ['solar-design-presets', 'solar-design-presets-list', 'solar-homeruns']) {
      const h = equipmentHarness()
      const unsupported = { ...projectEquipmentTool, name }
      render(host === 'tool' ? <SolarToolForm {...h.props} tool={unsupported} />
        : <SolarStepEditor {...h.props} row={unsupported} projectId={projectEquipmentBinding.project_id} />)
      expect(equipmentReason()).toBe(projectSolarFlowReason('tool_unsupported'))
      expect(h.fetchImpl).not.toHaveBeenCalled()
      expect(h.props.readIntake).not.toHaveBeenCalled()
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
      expect(h.props.onSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  }
    for (const name of ['solar-string-add', 'solar-string-multi-add']) {
    const h = equipmentHarness()
    const specialised = { ...projectEquipmentTool, name }
    render(<SolarStepEditor {...h.props} row={specialised} drawingId="d1" drawingVersion={3} />)
    await equipmentSettle()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    // R10: the two string tools read the project context through the project client, a canonical
    // read and never a legacy one; no specialised host submits a run or takes a checkout on mount.
    const urls = h.fetchImpl.mock.calls.map(([url]) => url)
    if (name === 'solar-string-add' || name === 'solar-string-multi-add') {
      expect(urls.length).toBeGreaterThan(0)
      expect(urls.every((url) => url.includes('/context?'))).toBe(true)
    } else {
      expect(urls).toEqual([])
    }
    cleanup()
  }
})

it('SolarToolForm renders only the visible keys', () => {
  const { container } = render(<SolarToolForm tool={tool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByRole('region', { name: 'Solar inputs parameters' }).id).toBe('solar-tool-form')
  expect(screen.getByRole('heading', { name: 'Solar inputs' })).toBeTruthy()
  for (const label of ['Expected rev', 'Cancel', 'Changes', 'Initialize']) {
    expect(screen.getByLabelText(label)).toBeTruthy()
  }
  expect(container.querySelectorAll('input')).toHaveLength(3)
  expect(container.querySelectorAll('select')).toHaveLength(1)
  expect(screen.queryByLabelText('Drawing id')).toBeNull()
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
})

it('SolarToolForm submits defaults merged with edits once and closes', () => {
  const onSubmit = vi.fn(() => ({ runIntent: { intentId: 'armed' } }))
  const onClose = vi.fn()
  render(<SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} />)
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 3, cancel: false, changes: {}, initialize: null })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit.mock.invocationCallOrder[0]).toBeLessThan(onClose.mock.invocationCallOrder[0])
})

it('SolarToolForm Escape and Cancel close without a run', () => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  const onParentKeyDown = vi.fn()
  render(<div onKeyDown={onParentKeyDown}><SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} /></div>)
  fireEvent.keyDown(screen.getByLabelText('Expected rev'), { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onParentKeyDown).not.toHaveBeenCalled()
  onClose.mockClear()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

const focusCases = [tool, presetTool].flatMap((formTool) =>
  ['Cancel', 'Escape'].map((action) => [formTool.name, action, formTool]))

it.each(focusCases)('%s %s returns focus to its visible ribbon button without submitting', (name, action, formTool) => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<>
    <button data-tool={name}>Open tool</button>
    <SolarToolForm tool={formTool} onSubmit={onSubmit} onClose={onClose} />
  </>)
  const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
  cancel.focus()
  if (action === 'Cancel') fireEvent.click(cancel)
  else fireEvent.keyDown(cancel, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open tool' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it.each(focusCases)('%s %s returns focus to More panels when its ribbon group is hidden', (name, action, formTool) => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<>
    <div id="drafting-ribbon-panels"><div hidden><button data-tool={name}>Open tool</button></div></div>
    <button aria-controls="drafting-ribbon-panels" aria-expanded="false">More panels</button>
    <SolarToolForm tool={formTool} onSubmit={onSubmit} onClose={onClose} />
  </>)
  const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
  cancel.focus()
  if (action === 'Cancel') fireEvent.click(cancel)
  else fireEvent.keyDown(cancel, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'More panels' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it.each(focusCases)('%s %s leaves focus alone when no return control exists', (name, action, formTool) => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<SolarToolForm tool={formTool} onSubmit={onSubmit} onClose={onClose} />)
  const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
  cancel.focus()
  expect(() => {
    if (action === 'Cancel') fireEvent.click(cancel)
    else fireEvent.keyDown(cancel, { key: 'Escape' })
  }).not.toThrow()
  expect(document.activeElement).toBe(cancel)
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it('ST4 routes design presets to the typed form', () => {
  const { container } = render(<SolarToolForm tool={presetTool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByTestId('solar-preset-form')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Design presets parameters' }).id).toBe('solar-tool-form')
  expect([...container.querySelectorAll('input')].some((input) => input.value === '{}')).toBe(false)
})

it('ST5 forwards the optional listing and revision', () => {
  render(<SolarToolForm tool={presetTool} presetListing={LIST_TWO} presetRevision={2} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByText('B Beta (active)')).toBeTruthy()
  expect(screen.getByLabelText('Graph revision').value).toBe('2')
})

it('ST6 keeps the List tool on the generic form', () => {
  const listTool = { ...presetTool, name: 'solar-design-presets-list', capabilities: ['drawing.read'],
    params: listDeclaration.record.params,
    solar: { ...presetTool.solar, name: 'solar-design-presets-list', entitlement: 'run_read', order: 14 } }
  render(<SolarToolForm tool={listTool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.queryByTestId('solar-preset-form')).toBeNull()
  expect(screen.getByRole('button', { name: 'Review & run' })).toBeTruthy()
})

// sf-w3-conversion-graph-surface: the ribbon's generic form starts expected_rev at the drawing's own graph
// revision, read through the same intake loader and rule the Solar step editor uses (solarFlowPrefill).
const conversionTool = {
  name: conversionDeclaration.name, capabilities: conversionDeclaration.record.capabilities,
  params: conversionDeclaration.record.params,
  solar: { schema: 'leaf.solar-tool-view.v1', name: conversionDeclaration.name, family: conversionDeclaration.family,
    wave: conversionDeclaration.wave, order: conversionDeclaration.order, entitlement: conversionDeclaration.entitlement,
    interaction: conversionDeclaration.interaction },
}
const intakeAt = (version, rev) => ({ version, intake: { solar_design_graph: { rev } } })
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

it('TF1 the conversion form starts at the drawing graph revision and submits it', async () => {
  const readIntake = vi.fn(async () => intakeAt(3, 7))
  const onSubmit = vi.fn()
  render(<SolarToolForm tool={conversionTool} readIntake={readIntake} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  expect(readIntake).toHaveBeenCalledTimes(1)
  expect(readIntake).toHaveBeenCalledWith('d1', 3)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 7 })
})

it('TF2 a revision the drafter typed is never overwritten by a late intake', async () => {
  const pending = deferred()
  const onSubmit = vi.fn()
  render(<SolarToolForm tool={conversionTool} readIntake={() => pending.promise} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('5')
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
})

it('TF3 an intake of another version, a malformed intake or a failed read prefills nothing', async () => {
  const answers = [
    () => intakeAt(2, 7),
    () => null,
    () => [intakeAt(3, 7)],
    () => Object.assign(Object.create({ version: 3 }), { intake: { solar_design_graph: { rev: 7 } } }),
    () => intakeAt(3, -1),
    () => intakeAt(3, 1.5),
    () => intakeAt(3, '7'),
    () => intakeAt(3, 2 ** 53),
    () => ({ version: 3, intake: null }),
    () => Promise.reject(new Error('offline')),
    () => { throw new Error('sync failure') },
  ]
  for (const [index, answer] of answers.entries()) {
    const readIntake = vi.fn(answer)
    const onSubmit = vi.fn()
    const view = render(<SolarToolForm tool={conversionTool} readIntake={readIntake} drawingId="d1" drawingVersion={3}
      onSubmit={onSubmit} onClose={vi.fn()} />)
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
    await act(async () => { await new Promise((done) => setTimeout(done, 0)) })
    // An absent required revision stays empty and blocks submission until supplied.
    expect(screen.getByLabelText('Expected rev').value, `answer ${index}`).toBe('')
    const run = screen.getByRole('button', { name: 'Review & run' })
    await waitFor(() => {
      expect(run.title).toBe('')
      expect(run.disabled, `answer ${index}`).toBe(true)
    })
    fireEvent.click(run)
    expect(onSubmit, `answer ${index}`).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
    await waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    expect(onSubmit, `answer ${index}`).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
    view.unmount()
  }
})

it('TF4 a form without an integer expected_rev never reads the intake', async () => {
  const readIntake = vi.fn(async () => intakeAt(3, 7))
  const stringRev = { ...tool, params: { type: 'object', properties: { expected_rev: { type: 'string' } } } }
  const noRev = { ...tool, params: { type: 'object', properties: { label: { type: 'string' } } } }
  // A pick step that binds expected_rev takes it off the form, so the form must not supply one either.
  const pickedRev = { ...conversionTool, solar: { ...conversionTool.solar,
    interaction: { mode: 'form', pick: [{ key: 'expected_rev' }] } } }
  for (const row of [stringRev, noRev, pickedRev]) {
    const view = render(<SolarToolForm tool={row} readIntake={readIntake} drawingId="d1" drawingVersion={3}
      onSubmit={vi.fn()} onClose={vi.fn()} />)
    await act(async () => { await new Promise((done) => setTimeout(done, 0)) })
    view.unmount()
  }
  expect(readIntake).not.toHaveBeenCalled()
})

it('TF5 a new drawing version reads its own revision while the field is untouched', async () => {
  const readIntake = vi.fn(async (id, version) => intakeAt(version, version === 3 ? 7 : 9))
  const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingVersion={3} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('9'))
  expect(readIntake.mock.calls).toEqual([['d1', 3], ['d1', 4]])
})

it('TF6 a late answer for the version the form left behind never lands', async () => {
  const late = deferred()
  const readIntake = vi.fn((id, version) => (version === 3 ? late.promise : Promise.resolve(intakeAt(4, 9))))
  const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingVersion={3} />)
  view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('9'))
  await act(async () => { late.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('9')
})

it('TF7 a new version clears the automatic revision and waits for its own read', async () => {
  const pending = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? Promise.resolve(intakeAt(3, 7)) : pending.promise)
  const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingVersion={3} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(true)
  expect(run.title).toBe("Reading this drawing's design revision.")
  expect(screen.getByLabelText('Expected rev').value).toBe('')
  fireEvent.click(run)
  expect(props.onSubmit).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(4, 8)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('8')
  expect(run.disabled).toBe(false)
  expect(run.title).toBe('')
  fireEvent.click(run)
  expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 8 })
})

it('TF8 a failed or graphless new read leaves the schema default instead of the old automatic revision', async () => {
  for (const answer of [() => Promise.reject(new Error('offline')), () => ({ version: 4, intake: null }),
    () => intakeAt(3, 8)]) {
    const readIntake = vi.fn((id, version) => version === 3 ? intakeAt(3, 7) : answer())
    const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
    const view = render(<SolarToolForm {...props} drawingVersion={3} />)
    await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
    view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
    const run = screen.getByRole('button', { name: 'Review & run' })
    await waitFor(() => {
      expect(run.title).toBe('')
      expect(run.disabled).toBe(true)
    })
    expect(screen.getByLabelText('Expected rev').value).toBe('')
    fireEvent.click(run)
    expect(props.onSubmit).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
    await waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
    view.unmount()
  }
})

it('TF9 a typed revision survives a drawing and version change while Run waits', async () => {
  const pending = deferred()
  const readIntake = vi.fn((id) => id === 'd1' ? intakeAt(3, 7) : pending.promise)
  const props = { tool: conversionTool, readIntake, onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingId="d1" drawingVersion={3} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  view.rerender(<SolarToolForm {...props} drawingId="d2" drawingVersion={4} />)
  expect(screen.getByLabelText('Expected rev').value).toBe('5')
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
  await act(async () => { pending.resolve(intakeAt(4, 8)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('5')
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
})

it('TF10 without a loader there is no pending read and the form keeps its default', () => {
  render(<SolarToolForm tool={tool} drawingId="d1" drawingVersion={3} onSubmit={vi.fn()} onClose={vi.fn()} />)
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(false)
  expect(run.title).toBe('')
  expect(screen.getByLabelText('Expected rev').value).toBe('1')
})

it('TF11 a changed drawing or tool clears the automatic revision to the current schema default', async () => {
  for (const changeTool of [false, true]) {
    const pending = deferred()
    const readIntake = vi.fn().mockResolvedValueOnce(intakeAt(3, 7)).mockImplementation(() => pending.promise)
    const nextTool = { ...conversionTool, params: { ...conversionTool.params,
      properties: { ...conversionTool.params.properties, expected_rev: { type: 'integer', default: 2 } } } }
    const props = { readIntake, drawingVersion: 3, onSubmit: vi.fn(), onClose: vi.fn() }
    const view = render(<SolarToolForm {...props} tool={conversionTool} drawingId="d1" />)
    await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
    view.rerender(<SolarToolForm {...props} tool={changeTool ? nextTool : conversionTool} drawingId={changeTool ? 'd1' : 'd2'} />)
    expect(screen.getByLabelText('Expected rev').value).toBe(changeTool ? '2' : '')
    expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
    await act(async () => { pending.resolve(null) })
    const run = screen.getByRole('button', { name: 'Review & run' })
    await waitFor(() => {
      expect(run.title).toBe('')
      expect(run.disabled).toBe(!changeTool)
    })
    expect(screen.getByLabelText('Expected rev').value).toBe(changeTool ? '2' : '')
    if (!changeTool) {
      fireEvent.click(run)
      expect(props.onSubmit).not.toHaveBeenCalled()
      fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
      await waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
    }
    view.unmount()
  }
})

const presetRevisionField = () => screen.getByLabelText('Graph revision')

it('FORM21 invalid Solar JSON blocks submit and close until corrected', () => {
  const onSubmit = vi.fn(() => ({ runIntent: { intentId: 'armed' } })), onClose = vi.fn()
  render(<SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} />)
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{' } })
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(true); fireEvent.click(run)
  expect(onSubmit).not.toHaveBeenCalled(); expect(onClose).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{"tilt":20}' } })
  expect(run.disabled).toBe(false); fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(tool, { expected_rev: 1, cancel: false, changes: { tilt: 20 }, initialize: null })
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('FORM23a clearing the Solar tool revision counts as a touch before intake', async () => {
  const pending = deferred(), onSubmit = vi.fn()
  const readIntake = vi.fn(() => pending.promise)
  render(<SolarToolForm tool={conversionTool} readIntake={readIntake} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('')
  const run = screen.getByRole('button', { name: 'Review & run' })
  await waitFor(() => {
    expect(run.title).toBe('')
    expect(run.disabled).toBe(true)
  })
  fireEvent.click(run)
  expect(onSubmit).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  await waitFor(() => expect(run.disabled).toBe(false))
  fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
})
it('FORM29 a change to another Solar field leaves the revision automatic', async () => {
  const pending = deferred(), onSubmit = vi.fn()
  const noteTool = { ...conversionTool, params: { ...conversionTool.params,
    properties: { ...conversionTool.params.properties, note: { type: 'string' } } } }
  render(<SolarToolForm tool={noteTool} readIntake={vi.fn(() => pending.promise)} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'east' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('7')
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(noteTool, { expected_rev: 7, note: 'east' })
})
const presetRun = () => screen.getByRole('button', { name: 'Review & run' })
const STALE = (label) => `${label} has a value this tool no longer accepts. Choose or enter a new one.`

it('FORM32 a nullable boolean default submits and closes the Solar form', () => {
  const nullableTool = { ...tool, params: { properties: { x: { type: ['boolean', 'null'], default: null } } } }
  const onSubmit = vi.fn(() => ({ runIntent: { intentId: 'armed' } })), onClose = vi.fn()
  render(<SolarToolForm tool={nullableTool} onSubmit={onSubmit} onClose={onClose} />)
  const input = screen.getByLabelText('X')
  expect(input.getAttribute('aria-invalid')).toBe('false')
  expect(input.validationMessage).toBe('')
  expect(screen.queryByText(STALE('X'))).toBeNull()
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(false)
  fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(nullableTool, { x: null })
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('FORM43 a nullable sibling keeps revision intake automatic after another field edit', async () => {
  const pending = deferred(), onSubmit = vi.fn()
  const noteTool = { ...conversionTool, params: { ...conversionTool.params,
    properties: { ...conversionTool.params.properties, note: { type: 'string' },
      x: { type: ['boolean', 'null'], default: null } } } }
  render(<SolarToolForm tool={noteTool} readIntake={vi.fn(() => pending.promise)} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'east' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('7')
  const input = screen.getByLabelText('X')
  expect(input.getAttribute('aria-invalid')).toBe('false')
  expect(input.validationMessage).toBe('')
  expect(screen.queryByText(STALE('X'))).toBeNull()
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(false)
  fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(noteTool, { expected_rev: 7, note: 'east', x: null })
})

function openPresetRead(props = {}) {
  const bound = { tool: presetTool, drawingId: 'd1', drawingVersion: 3, onSubmit: vi.fn(() => ({ runIntent: { intentId: 'armed' } })), onClose: vi.fn(), ...props }
  return { ...render(<SolarToolForm {...bound} />), props: bound }
}
const presetName = () => fireEvent.change(screen.getByLabelText('Preset name'), { target: { value: 'Alpha' } })

it('G1C-1 reads the bound drawing revision and submits it', async () => {
  const pending = deferred()
  const readIntake = vi.fn(() => pending.promise)
  const { props } = openPresetRead({ readIntake })
  presetName()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(readIntake).toHaveBeenCalledTimes(1)
  expect(readIntake).toHaveBeenCalledWith('d1', 3)
  expect(presetRevisionField().value).toBe('7')
  fireEvent.click(presetRun())
  expect(props.onSubmit).toHaveBeenCalledWith(presetTool, { expected_rev: 7, subcommand: 'Create', name: 'Alpha' })
})

it('G1C-2 prefills revision zero', async () => {
  const pending = deferred()
  openPresetRead({ readIntake: () => pending.promise })
  await act(async () => { pending.resolve(intakeAt(3, 0)) })
  expect(presetRevisionField().value).toBe('0')
})

it('G1C-3 without a reader keeps an empty revision and no reading lock', () => {
  openPresetRead()
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe('')
  presetName()
  fireEvent.change(presetRevisionField(), { target: { value: '0' } })
  expect(presetRun().disabled).toBe(false)
})

it('G1C-4 invalid or failed intakes leave an empty revision and settle the read', async () => {
  const answers = [() => intakeAt(2, 7), () => ({ version: 3, intake: {} }), () => null,
    () => Promise.reject(new Error('offline')), () => { throw new Error('sync') },
    () => [intakeAt(3, 7)], () => Object.assign(Object.create({ version: 3 }), { intake: { solar_design_graph: { rev: 7 } } })]
  for (const answer of answers) {
    const readIntake = vi.fn(answer)
    const view = openPresetRead({ readIntake })
    await act(async () => {})
    expect(readIntake).toHaveBeenCalledTimes(1)
    expect(presetRevisionField().value).toBe('')
    expect(presetRun().title).toBe('')
    presetName()
    fireEvent.change(presetRevisionField(), { target: { value: '0' } })
    expect(presetRun().disabled).toBe(false)
    view.unmount()
  }
})

it('G1C-5 every revision change event protects the draft from a late read', async () => {
  for (const value of ['5', '', '7']) {
    const pending = deferred()
    const first = deferred()
    const readIntake = vi.fn((id, version) => value === '7' && version === 3 ? first.promise : pending.promise)
    const view = openPresetRead({ readIntake })
    // Dispatch even when the value shown is unchanged: any change event is a touch.
    if (value === '7') {
      await act(async () => { first.resolve(intakeAt(3, 7)) })
      // React tracks DOM values; reset its event tracker to dispatch the same-value change.
      presetRevisionField()._valueTracker.setValue('')
      fireEvent.change(presetRevisionField(), { target: { value: '7' } })
      view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
    } else if (value === '') {
      fireEvent.change(presetRevisionField(), { target: { value: '5' } })
      fireEvent.change(presetRevisionField(), { target: { value } })
    } else fireEvent.change(presetRevisionField(), { target: { value } })
    await act(async () => { pending.resolve(intakeAt(value === '7' ? 4 : 3, value === '7' ? 9 : 7)) })
    expect(presetRevisionField().value).toBe(value)
    view.unmount()
  }
})

it('G1C-6 a version move ignores the old read', async () => {
  const old = deferred(), next = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? old.promise : next.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => {})
  view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
  await act(async () => { old.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  await act(async () => { next.resolve(intakeAt(4, 9)) })
  expect(presetRevisionField().value).toBe('9')
  expect(readIntake.mock.calls).toEqual([['d1', 3], ['d1', 4]])
})

it('G1C-7 a drawing move ignores the old drawing at the same version', async () => {
  const old = deferred(), next = deferred()
  const readIntake = vi.fn((id) => id === 'a' ? old.promise : next.promise)
  const view = openPresetRead({ readIntake, drawingId: 'a' })
  await act(async () => {})
  view.rerender(<SolarToolForm {...view.props} drawingId="b" />)
  await act(async () => { old.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('')
  await act(async () => { next.resolve(intakeAt(3, 9)) })
  expect(presetRevisionField().value).toBe('9')
  expect(readIntake.mock.calls).toEqual([['a', 3], ['b', 3]])
})

it('G1C-8 a new version withdraws an automatic revision even when its read fails', async () => {
  const first = deferred(), next = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? first.promise : next.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => { first.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('7')
  view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  await act(async () => { next.resolve(Promise.reject(new Error('offline'))) })
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe('')
})

it('G1C-9 a typed revision survives a drawing and version move', async () => {
  const first = deferred(), next = deferred()
  const readIntake = vi.fn((id) => id === 'd1' ? first.promise : next.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => { first.resolve(intakeAt(3, 7)) })
  fireEvent.change(presetRevisionField(), { target: { value: '5' } })
  view.rerender(<SolarToolForm {...view.props} drawingId="d2" drawingVersion={4} />)
  expect(presetRevisionField().value).toBe('5')
  await act(async () => { next.resolve(intakeAt(4, 9)) })
  expect(presetRevisionField().value).toBe('5')
})

it('G1C-10 refuses invalid revisions and preserves the declared maximum', async () => {
  for (const rev of [-1, 1.5, '7', null, Number.MAX_SAFE_INTEGER + 1, 2147483648, 2147483647]) {
    const pending = deferred()
    const view = openPresetRead({ readIntake: () => pending.promise })
    presetName()
    await act(async () => { pending.resolve(intakeAt(3, rev)) })
    expect(presetRevisionField().value).toBe(rev === 2147483647 ? '2147483647' : '')
    expect(presetRun().title).toBe('')
    if (rev === 2147483647) {
      fireEvent.click(presetRun())
      expect(view.props.onSubmit.mock.calls[0][1].expected_rev).toBe(2147483647)
    }
    view.unmount()
  }
})

it('G1C-11 pending reads block button and keyboard submission without marking fields attempted', async () => {
  const pending = deferred()
  const view = openPresetRead({ readIntake: () => pending.promise })
  presetName()
  fireEvent.change(presetRevisionField(), { target: { value: '5' } })
  expect(presetRun().disabled).toBe(true)
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  fireEvent.click(screen.getByLabelText('Supply the settings'))
  const mppt = screen.getByLabelText('MPPT count')
  fireEvent.change(mppt, { target: { value: '2.5' } })
  expect(presetRun().disabled).toBe(true)
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  fireEvent.submit(view.container.querySelector('form'))
  expect(mppt.hasAttribute('aria-invalid')).toBe(false)
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
})

it('G1C-12 an explicit preset revision wins over the read', async () => {
  const pending = deferred()
  openPresetRead({ presetRevision: 4, readIntake: () => pending.promise })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('4')
})

it('G1C-15 List and generic tools keep the generic form without automatic preset props', () => {
  const listTool = { ...presetTool, name: 'solar-design-presets-list', capabilities: ['drawing.read'],
    params: listDeclaration.record.params,
    solar: { ...presetTool.solar, name: 'solar-design-presets-list', entitlement: 'run_read', order: 14 } }
  for (const generic of [listTool, tool, conversionTool]) {
    const view = render(<SolarToolForm tool={generic} presetRevision={4} onSubmit={vi.fn()} onClose={vi.fn()} />)
    expect(screen.queryByTestId('solar-preset-form')).toBeNull()
    expect(screen.queryByLabelText('Graph revision')).toBeNull()
    expect(view.container.querySelector('form')).toBeNull()
    expect(presetRun().title).toBe('')
    view.unmount()
  }
})

it('G1C-16 the preset form refuses an automatic revision below its declared minimum', () => {
  const params = structuredClone(presetTool.params)
  params.properties.expected_rev.minimum = 5
  render(<SolarPresetForm tool={{ ...presetTool, params }} onSubmit={vi.fn()} onClose={vi.fn()}
    autoRevision={{ key: 'k', value: 3 }} />)
  expect(presetRevisionField().value).toBe('')
})

it('G1C-17 the preset form directly refuses a fractional automatic revision', () => {
  render(<SolarPresetForm tool={presetTool} onSubmit={vi.fn()} onClose={vi.fn()}
    autoRevision={{ key: 'k', value: 1.5 }} />)
  expect(presetRevisionField().value).toBe('')
})

it('G1C-18 a changed automatic key clears the old revision even without an intervening null', () => {
  const props = { tool: presetTool, onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarPresetForm {...props} autoRevision={{ key: 'a', value: 7 }} />)
  expect(presetRevisionField().value).toBe('7')
  view.rerender(<SolarPresetForm {...props} autoRevision={{ key: 'b', value: 2147483648 }} />)
  expect(presetRevisionField().value).toBe('')
})

it('G1C-19 a keyboard submit during reading does not lock a later valid submit', async () => {
  const pending = deferred()
  const view = openPresetRead({ readIntake: () => pending.promise })
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('7')
  presetName()
  expect(presetRun().disabled).toBe(false)
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).toHaveBeenCalledTimes(1)
  expect(view.props.onSubmit).toHaveBeenCalledWith(presetTool, { expected_rev: 7, subcommand: 'Create', name: 'Alpha' })
  expect(view.props.onClose).toHaveBeenCalledTimes(1)
})

it('G1C-23 a valid keyboard submit during reading does not lock a later submit', async () => {
  const pending = deferred()
  const view = openPresetRead({ readIntake: () => pending.promise })
  presetName()
  fireEvent.change(presetRevisionField(), { target: { value: '5' } })
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('5')
  expect(presetRun().disabled).toBe(false)
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).toHaveBeenCalledTimes(1)
  expect(view.props.onSubmit).toHaveBeenCalledWith(presetTool, { expected_rev: 5, subcommand: 'Create', name: 'Alpha' })
  expect(view.props.onClose).toHaveBeenCalledTimes(1)
})

it('G1C-21 an old read resolving after the new read cannot replace its revision or reading state', async () => {
  const oldRead = deferred()
  const newRead = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? oldRead.promise : newRead.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => {})
  view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
  await act(async () => {})
  expect(readIntake.mock.calls).toEqual([['d1', 3], ['d1', 4]])
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  await act(async () => { newRead.resolve(intakeAt(4, 9)) })
  expect(presetRevisionField().value).toBe('9')
  presetName()
  expect(presetRun().disabled).toBe(false)
  await act(async () => { oldRead.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('9')
  expect(presetRun().title).toBe('')
  expect(presetRun().disabled).toBe(false)
})

it('G1C-22 a generic tool without a revision never reads and its form ignores automatic preset props', async () => {
  const generic = { ...tool, params: { type: 'object', properties: { label: { type: 'string', default: 'ordinary' } } } }
  const props = { tool: generic, onSubmit: vi.fn(), onClose: vi.fn() }
  const readIntake = vi.fn(async () => intakeAt(3, 7))
  const publicView = render(<SolarToolForm {...props} readIntake={readIntake} drawingId="d1" drawingVersion={3} />)
  await act(async () => {})
  expect(readIntake).not.toHaveBeenCalled()
  publicView.unmount()
  // The router returns the private generic component; obtain its type without changing production exports.
  const GenericSolarToolForm = SolarToolForm(props).type
  const first = render(<GenericSolarToolForm {...props} />)
  const html = first.container.innerHTML
  first.unmount()
  const second = render(<GenericSolarToolForm {...props} autoRevision={{ key: 'wrong', value: 99 }} />)
  expect(second.container.innerHTML).toBe(html)
  expect([...second.container.querySelectorAll('input, select, textarea')].some((field) => field.value === '99')).toBe(false)
})
