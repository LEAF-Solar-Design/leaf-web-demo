import React from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import SolarSizingForm from './SolarSizingForm.jsx'
import { projectSizingConductorFeedback, useProjectSizingConductor } from './SolarSizingForm.jsx'
import { PROJECT_SIZING_CONDUCTOR_REASONS, projectSizingConductorReason, MODULE_PARAMETER_KEYS,
  deriveProjectSizingState, buildProjectSizingRequest } from './solarSizingModel.js'
import { createSolarProjectClient } from './solarProjectClient.js'
import { recordToEnvelope, runToolAsync } from '../api.js'
import { solarFlowRunOutcome } from './solarFlowModel.js'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'

const MIN = { cells: 81, voc: 52.58, isc: 13.9965, pmp: 595.5, vmp: 44.64, imp: 13.33,
  bpmp: -1.4875, bvoc: -0.13145, alpha_sc: .007, min_temp: -2.7,
  simulation_results: { standard: { Conditions: 'P99.5 Voc', max_module_voltage: 51.26,
    string_design_voltage: 1500, string_length: 28 } } }

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })
const id = (kind, n) => `leaf:${kind}:00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const S = id('settings', 1), P1 = id('panel', 1), P2 = id('panel', 2)
const ZA = id('zone', 1), ZB = id('zone', 2)
const G7 = () => ({ rev: 7, settings: { id: S, extra: {} }, project: { zip_code: '44224-1234' },
  panels: [{ id: P1 }, { id: P2 }], electrical_zones: [
    { id: ZA, panel_refs: [P1], module_model: 'Module A', inverter_model_a: 'Inverter A' },
    { id: ZB, panel_refs: [P2], module_model: 'Module B', inverter_model_a: 'Inverter B' },
  ] })
const R = { module_name: 'JA_Solar_JAM72D40-595/MB', full_inverter_name: 'Sungrow SG-HX SG250HX',
  bifacial: false, bifacial_coefficient: '.7', racking_params: { racking_type: 'fixed_tilt', surface_tilt: '5',
    surface_azimuth: '180', albedo: '.25' }, max_voltage: '1500', thermal_model_type: 'close mount glass glass',
  open_circuit_rise: false, zip_code: '44224' }
const fields = { Module: R.module_name, Inverter: R.full_inverter_name, 'Bifacial coefficient': '.7',
  'Surface tilt': '5', 'Surface azimuth': '180', Albedo: '.25', 'Maximum voltage': '1500',
  'Thermal model': R.thermal_model_type, 'Grant reference': 'grant_1', Bifacial: 'false',
  'Open circuit rise': 'false', Racking: 'fixed_tilt' }
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const run = () => screen.getByRole('button', { name: 'Review & run' })
const moduleResult = (key = R.module_name) => ({ ok: true, data: { modules: [
  { key, stc_w: 595.5, n_s: 72 },
], truncated: false } })
async function chooseTypedModule() {
  const name = screen.getByLabelText('Module').value
  const option = await screen.findByRole('option', { name: name + ' (595.5 W, 72 cells)' })
  fireEvent.click(option)
}
const fill = async (skip = []) => {
  vi.useFakeTimers()
  Object.entries(fields).forEach(([label, value]) => {
    if (!skip.includes(label)) change(label, value)
  })
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  vi.useRealTimers()
  if (!skip.includes('Module')) await chooseTypedModule()
}
const view = (graph, version = 3) => ({ version, intake: { solar_design_graph: graph } })
async function mount(graph = G7(), overrides = {}, mode = 'global') {
  const props = { row: { name: 'solar-size-strings' }, drawingId: 'd1', drawingVersion: 3, projectId: null,
    readIntake: vi.fn(async () => view(graph)), onSubmit: vi.fn(), projectClient: { searchSizingModules: vi.fn(async () => moduleResult()) }, ...overrides }
  const result = render(<SolarSizingForm {...props} />)
  await act(async () => {})
  if (mode !== 'manual-global') change('Scope', mode)
  return { ...result, props }
}
function invalid(label) {
  expect(screen.getByText(label === 'Module' ? 'Choose a module from the sizer catalog.' :
    'Check the marked sizing fields before submitting.')).toBeTruthy()
  expect(screen.getByLabelText(label).getAttribute('aria-invalid')).toBe('true')
  expect(run().disabled).toBe(true)
}


const R9Form = SolarSizingForm
const r9Tool = 'solar-size-strings'
const r9Row = () => ({ name: r9Tool, catalog_digest: r9Digest })
const r9Graph = () => ({ ...G7(), settings: { ...G7().settings, panels_in_sequence: 3 },
  project: { zip_code: '' }, electrical_zones: [] })
const r9Button = run

const r9Binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const r9Next = { ...r9Binding, input_version_id: '55555555-5555-5555-5555-555555555555' }
const r9Digest = 'sha256:' + 'd'.repeat(64)
const r9Job = '77777777-7777-7777-7777-777777777777'
const r9Clients = []
afterEach(() => { for (const client of r9Clients.splice(0)) client.dispose() })
const r9Deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const r9Submitted = () => ({ ok: true, status: 202, data: { status: 'submitted', job_id: r9Job } })
function r9Preview(tool, digest = r9Digest, availability = {}) {
  return { ok: true, status: 200, data: { families: [{ capabilities: [{ name: tool,
    catalog_digest: digest, availability: { entitled: true, engine_ready: true, implemented: true,
      input_ready: true, runnable: true, admission_checked: true, admission: { status_code: 200, error: null },
      refusal_reasons: [], ...availability } }] }] } }
}
function r9Context(graph = r9Graph(), binding = r9Binding) {
  return { context: { ...binding, head_version_id: binding.input_version_id,
    is_head: true, intake_sha256: 'a'.repeat(64) },
  intake: { solar_design_graph: graph, solar_design_graph_sha256: 'b'.repeat(64) } }
}
function r9Harness() {
  const h = { context: r9Context(), contextQueue: [], previewQueue: [], unsubscribes: [],
    preview: r9Preview(r9Tool) }
  h.fetchImpl = vi.fn(async (url, options) => {
    let data = h.context, status = 200
    if (url.includes('/api/capabilities?')) data = h.preview.data
    else if (url.endsWith('/checkout')) {
      const b = h.real.getSnapshot().binding
      data = { organization_id: b.organization_id, project_id: b.project_id, drawing_id: b.drawing_id,
        acquired: true, checkout: { holder: 'synthetic editor', acquired: '2026-01-01T00:00:00Z',
          expires: '2099-01-01T00:00:00Z', fence: '1' }, checkout_capability: 'lco1.' + 'c'.repeat(64) }
    } else if (url.endsWith('/api/run')) { data = r9Submitted().data; status = 202 }
    return { ok: true, status, json: async () => data }
  })
  h.real = createSolarProjectClient({ fetchImpl: h.fetchImpl, authHeaders: () => ({ Authorization: 'tok' }) })
  h.real.setScope(r9Binding, 'epoch-1')
  r9Clients.push(h.real)
  h.client = { ...h.real,
    searchSizingModules: vi.fn(async () => moduleResult()),
    readContext: vi.fn((options) => h.contextQueue.length ? Promise.resolve(h.contextQueue.shift()) : h.real.readContext(options)),
    catalog: vi.fn((options) => h.previewQueue.length ? Promise.resolve(h.previewQueue.shift()) : h.real.catalog(options)),
    prepareRun: vi.fn(h.real.prepareRun),
    subscribe: vi.fn((listener) => {
      const unsubscribe = vi.fn(h.real.subscribe(listener)); h.unsubscribes.push(unsubscribe); return unsubscribe
    }), dispose: vi.fn(h.real.dispose), acquireCheckout: vi.fn(h.real.acquireCheckout), releaseCheckout: vi.fn(h.real.releaseCheckout),
    setScope: vi.fn(h.real.setScope),
  }
  h.props = { row: r9Row(), solarBinding: r9Binding, authorityKey: 'epoch-1', projectClient: h.client,
    catalogDigest: r9Digest, newProjectIntentKey: vi.fn(() => 'r9-key-' + (h.client.prepareRun.mock.calls.length + 1)),
    onProjectSubmit: vi.fn(async () => r9Submitted()), onSubmit: vi.fn(), readIntake: vi.fn(), onClose: vi.fn() }
  return h
}
const r9Settle = async () => { await act(async () => {}) }
async function r9Ready() { await waitFor(() => expect(r9Button().disabled).toBe(false)) }
function r9Rebind(view, h, changes) {
  h.props = { ...h.props, ...changes }
  h.context = r9Context(r9Graph(), h.props.solarBinding)
  act(() => h.real.setScope(h.props.solarBinding, h.props.authorityKey))
  view.rerender(<R9Form {...h.props} />)
}

const r14Tracker = { 'Axis tilt': '0', 'Axis azimuth': '180', 'Max angle': '60',
  'Ground coverage ratio': '.4', Backtrack: 'true' }
async function r14Fill({ tracker = false, parameters = false } = {}) {
  change('Scope', 'global')
  await fill()
  if (tracker) {
    change('Racking', 'single_axis')
    Object.entries(r14Tracker).forEach(([label, value]) => change(label, value))
  }
  if (parameters) {
    fireEvent.click(screen.getByLabelText('Use module parameters'))
    MODULE_PARAMETER_KEYS.forEach((key) => change(key, key === 'N_s' ? '72' : '1'))
  }
}
function r14Harness() {
  const h = r9Harness()
  h.context.intake.solar_design_graph.project.zip_code = '44224-1234'
  return h
}

function sizingAvailabilityHarness() {
  const client = {
    searchSizingModules: vi.fn(async () => moduleResult()),
    readContext: vi.fn(async () => ({ ok: true, status: 200,
      data: r9Context({ ...r9Graph(), project: { zip_code: '44224-1234' } }) })),
    prepareRun: vi.fn((request) => request),
    catalog: vi.fn(async () => r9Preview(r9Tool)),
    captureCatalogScope: () => () => true,
    subscribe: () => () => {},
    getSnapshot: () => ({ binding: r9Binding, authorityKey: 'epoch-1' }),
  }
  const props = { row: r9Row(), solarBinding: r9Binding, authorityKey: 'epoch-1', projectClient: client,
    catalogDigest: r9Digest, newProjectIntentKey: vi.fn(() => 'sizing-availability-key'),
    onProjectSubmit: vi.fn(async () => r9Submitted()), busy: false }
  return { client, props }
}

const coldVoltage = () => ({ standard_string_length: 32, suggested_string_length: 31,
  per_module: 48.3, string_voltage: 1545.6, max_dc_voltage: 1500 })
const coldFailure = (cold_voltage = coldVoltage()) => ({ ok: true, data: { job: {
  status: 'failed', error: { reason_code: 'COLD_VOLTAGE_FAILED', cold_voltage },
} } })
async function submitColdRun(outcome = coldFailure()) {
  const h = sizingAvailabilityHarness()
  h.observation = r9Deferred()
  h.client.observeJob = vi.fn(() => h.observation.promise)
  const view = render(<SolarSizingForm {...h.props} />)
  await r9Ready()
  await r14Fill()
  await r9Ready()
  fireEvent.click(run())
  await waitFor(() => expect(h.client.observeJob).toHaveBeenCalledTimes(1))
  if (outcome) await act(async () => h.observation.resolve(outcome))
  return { ...h, view }
}

describe('SolarSizingForm', () => {
  async function moduleSearchForm(search = vi.fn(async () => moduleResult())) {
    const h = r14Harness()
    h.client.searchSizingModules = search
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    change('Scope', 'global')
    await fill(['Module'])
    vi.useFakeTimers()
    return h
  }
  const debounce = async (ms = 300) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

  it.each(['click', 'Enter'])('debounces module searches and confirms a catalog key by %s', async (method) => {
    const search = vi.fn(async () => ({ ok: true, data: { modules: [
      { key: 'CS6U_330', stc_w: 330, n_s: 72 },
      { key: 'CS6U_340', stc_w: null, n_s: 72 },
      { key: 'CS6U_350', stc_w: 350, n_s: null },
      { key: 'CS6U_unknown', stc_w: null, n_s: null },
    ], truncated: true } }))
    const h = await moduleSearchForm(search)
    const input = screen.getByRole('combobox', { name: 'Module' })
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(input.getAttribute('aria-controls')).toBeNull()
    change('Module', 'cs')
    await debounce(200)
    change('Module', 'cs6u')
    await debounce(299)
    expect(search).not.toHaveBeenCalled()
    await debounce(1)
    expect(search).toHaveBeenCalledTimes(1)
    expect(search.mock.calls[0][0]).toMatchObject({ q: 'cs6u', limit: 20, grantRef: 'grant_1' })
    expect(search.mock.calls[0][0].signal.aborted).toBe(false)
    expect(input.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('listbox').id).toBe(input.getAttribute('aria-controls'))
    expect(screen.getByRole('option', { name: 'CS6U_340 (72 cells)' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'CS6U_350 (350 W)' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'CS6U_unknown' })).toBeTruthy()
    expect(screen.getByText('More modules match; keep typing.')).toBeTruthy()
    if (method === 'click') fireEvent.click(screen.getByRole('option', { name: 'CS6U_330 (330 W, 72 cells)' }))
    else {
      fireEvent.keyDown(input, { key: 'ArrowDown' })
      expect(screen.getByRole('option', { name: 'CS6U_340 (72 cells)' }).getAttribute('aria-selected')).toBe('true')
      fireEvent.keyDown(input, { key: 'ArrowUp' })
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(input.getAttribute('aria-expanded')).toBe('false')
      expect(input.getAttribute('aria-controls')).toBeNull()
      fireEvent.keyDown(input, { key: 'ArrowDown' })
      expect(document.getElementById(input.getAttribute('aria-controls'))).toBe(screen.getByRole('listbox'))
      fireEvent.keyDown(input, { key: 'ArrowUp' })
      fireEvent.keyDown(input, { key: 'Enter' })
    }
    expect(input.value).toBe('CS6U_330')
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(input.getAttribute('aria-controls')).toBeNull()
    vi.useRealTimers()
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    await r9Settle()
    const params = h.props.onProjectSubmit.mock.calls[0][0].params
    expect(params.requests[S].module_name).toBe('CS6U_330')
    expect(JSON.stringify(params)).not.toContain('module_confirmed')
    change('Module', 'CS6U_330x')
    expect(run().disabled).toBe(true)
    expect(screen.getByText('Choose a module from the sizer catalog.')).toBeTruthy()
  })

  it.each(['project', 'binding', 'authority', 'client', 'projectId'])('keeps module confirmation when project %s changes until the name is edited', async (scope) => {
    const h = sizingAvailabilityHarness()
    const rendered = render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    await r14Fill()
    await r9Ready()
    const binding = scope === 'project' ? { ...r9Binding, project_id: r9Job }
      : scope === 'binding' ? r9Next : r9Binding
    const authorityKey = scope === 'authority' ? 'epoch-2' : h.props.authorityKey
    const client = scope === 'client' ? { ...h.client } : h.client
    client.getSnapshot = () => ({ binding, authorityKey })
    client.readContext = vi.fn(async () => ({ ok: true, status: 200,
      data: r9Context({ ...r9Graph(), project: { zip_code: '44224' } }, binding) }))
    rendered.rerender(<SolarSizingForm {...h.props} solarBinding={binding} authorityKey={authorityKey}
      projectClient={client} projectId={scope === 'projectId' ? true : undefined} />)
    await r9Ready()
    expect(screen.getByLabelText('Module').value).toBe(R.module_name)
    expect(screen.queryByText('Choose a module from the sizer catalog.')).toBeNull()
    change('Module', R.module_name + 'x')
    await screen.findByText('Choose a module from the sizer catalog.')
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
  })

  it.each(['drawingId', 'drawingVersion'])('keeps module confirmation when standalone %s changes until the name is edited', async (scope) => {
    const { props, rerender } = await mount(G7(), {
      readIntake: vi.fn(async (_id, version) => view(G7(), version)),
    })
    await fill()
    expect(run().disabled).toBe(false)
    rerender(<SolarSizingForm {...props} {...{ [scope]: scope === 'drawingId' ? 'd2' : 4 }} />)
    await waitFor(() => expect(run().disabled).toBe(false))
    expect(screen.getByLabelText('Module').value).toBe(R.module_name)
    expect(screen.queryByText('Choose a module from the sizer catalog.')).toBeNull()
    change('Module', R.module_name + 'x')
    await screen.findByText('Choose a module from the sizer catalog.')
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('hides old catalog options on the first commit after a drawing change, before passive effects', async () => {
    const observations = []
    function ObserveForm(props) {
      React.useLayoutEffect(() => {
        observations.push({ drawingId: props.drawingId,
          option: screen.queryByRole('option', { name: 'Old (595.5 W, 72 cells)' }),
          controls: screen.queryByRole('combobox', { name: 'Module' })?.getAttribute('aria-controls') })
      }, [props.drawingId])
      return <SolarSizingForm {...props} />
    }
    const search = vi.fn(async () => moduleResult('Old'))
    const props = { row: { name: 'solar-size-strings' }, drawingId: 'd1', drawingVersion: 3,
      readIntake: vi.fn(async () => view(G7())), onSubmit: vi.fn(),
      projectClient: { searchSizingModules: search } }
    const rendered = render(<ObserveForm {...props} />)
    await r9Settle()
    change('Scope', 'global')
    vi.useFakeTimers()
    change('Grant reference', 'grant_1')
    change('Module', 'old')
    await debounce()
    expect(screen.getByRole('option', { name: 'Old (595.5 W, 72 cells)' })).toBeTruthy()
    rendered.rerender(<ObserveForm {...props} drawingId="d2" />)
    expect(observations.at(-1)).toEqual({ drawingId: 'd2', option: null, controls: null })
    expect(screen.queryByRole('option', { name: 'Old (595.5 W, 72 cells)' })).toBeNull()
    expect(search).toHaveBeenCalledTimes(1)
    await r9Settle()
  })

  it('aborts superseded module searches and ignores their late results', async () => {
    const old = r9Deferred(), next = r9Deferred()
    const search = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    await moduleSearchForm(search)
    change('Module', 'cs6')
    await debounce()
    change('Module', 'cs6u')
    expect(search.mock.calls[0][0].signal.aborted).toBe(true)
    await debounce()
    await act(async () => next.resolve(moduleResult('Current')))
    expect(screen.getByRole('option', { name: 'Current (595.5 W, 72 cells)' })).toBeTruthy()
    await act(async () => old.resolve(moduleResult('Old')))
    expect(screen.queryByRole('option', { name: 'Old (595.5 W, 72 cells)' })).toBeNull()
    expect(screen.getByLabelText('Module').value).toBe('cs6u')
    cleanup()
    expect(search.mock.calls[1][0].signal.aborted).toBe(true)
  })

  it('needs a valid grant reference and two characters before searching', async () => {
    const search = vi.fn(async () => moduleResult())
    await moduleSearchForm(search)
    change('Grant reference', '')
    change('Module', 'cs6u')
    await debounce()
    expect(screen.getByText('Enter the grant reference to search the module catalog.')).toBeTruthy()
    change('Grant reference', 'bad ref')
    await debounce()
    expect(search).not.toHaveBeenCalled()
    change('Module', ' c ')
    change('Grant reference', 'grant_1')
    await debounce()
    expect(search).not.toHaveBeenCalled()
  })

  it.each(['refused', 'rejected', 'empty'])('keeps module text after an %s search', async (kind) => {
    const search = vi.fn(() => kind === 'rejected' ? Promise.reject(new Error('failed')) :
      Promise.resolve(kind === 'refused' ? { ok: false } : { ok: true, data: { modules: [], truncated: false } }))
    await moduleSearchForm(search)
    change('Module', 'cs6u')
    await debounce()
    expect(screen.getByText(kind === 'empty' ? 'No modules match.' : 'The module catalog could not be searched.')).toBeTruthy()
    expect(screen.getByLabelText('Module').value).toBe('cs6u')
    expect(run().disabled).toBe(true)
  })

  it('W1C-34c offers the cold-voltage arithmetic and submits an explicitly accepted length', async () => {
    const h = await submitColdRun()
    expect(h.client.observeJob.mock.calls[0][0]).toBe(r9Job)
    expect(h.client.observeJob.mock.calls[0][1].tool).toBe(r9Tool)
    expect(screen.getByText('At 32 modules the cold-weather open-circuit voltage is 1545.6 V, above the 1500.0 V limit. 31 modules stay within it.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Use 31 modules' }))
    expect(screen.queryByRole('button', { name: 'Use 31 modules' })).toBeNull()
    expect(screen.getByText('Accepted string length: 31')).toBeTruthy()
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2))
    expect(h.props.onProjectSubmit.mock.calls[1][0].params.accepted_string_length).toBe(31)
    change('Surface tilt', '6')
    expect(screen.queryByText('Accepted string length: 31')).toBeNull()
    await r9Ready()
    expect(h.client.catalog.mock.calls.at(-1)[0].intents[0].params).not.toHaveProperty('accepted_string_length')
  })

  it.each(['clear', 'mode', 'scope'])('W1C-34c clears the accepted length on %s', async (action) => {
    const h = await submitColdRun()
    fireEvent.click(screen.getByRole('button', { name: 'Use 31 modules' }))
    await r9Ready()
    if (action === 'clear') fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    else if (action === 'mode') change('Scope', 'manual-global')
    else h.view.rerender(<SolarSizingForm {...h.props} authorityKey="epoch-2" />)
    expect(screen.queryByText('Accepted string length: 31')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Use 31 modules' })).toBeNull()
    if (action !== 'scope') {
      change('Scope', 'global')
      await r9Ready()
      expect(h.client.catalog.mock.calls.at(-1)[0].intents[0].params).not.toHaveProperty('accepted_string_length')
    }
  })

  it.each(['catalog', 'client', 'preview'])('W1C-34c retains the job and suggestion across a %s refresh', async (refresh) => {
    for (const phase of ['pending', 'failed']) {
      const h = r14Harness(), observation = r9Deferred()
      h.client.observeJob = vi.fn(() => observation.promise)
      const view = render(<SolarSizingForm {...h.props} />)
      await r9Ready()
      await r14Fill()
      await r9Ready()
      fireEvent.click(run())
      await waitFor(() => expect(h.client.observeJob).toHaveBeenCalledTimes(1))
      const signal = h.client.observeJob.mock.calls[0][1].signal
      if (phase === 'failed') await act(async () => observation.resolve(coldFailure()))
      const previews = h.client.catalog.mock.calls.length
      if (refresh === 'catalog') {
        h.props = { ...h.props, catalogDigest: 'sha256:' + 'e'.repeat(64) }
        h.preview = r9Preview(r9Tool, h.props.catalogDigest)
        view.rerender(<SolarSizingForm {...h.props} />)
      } else if (refresh === 'client') {
        await act(async () => h.real.acquireCheckout())
      } else fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }))
      await waitFor(() => expect(h.client.catalog.mock.calls.length).toBeGreaterThan(previews))
      await r9Ready()
      expect(signal.aborted).toBe(false)
      if (phase === 'pending') await act(async () => observation.resolve(coldFailure()))
      expect(screen.getByRole('button', { name: 'Use 31 modules' })).toBeTruthy()
      expect(screen.getByText('At 32 modules the cold-weather open-circuit voltage is 1545.6 V, above the 1500.0 V limit. 31 modules stay within it.')).toBeTruthy()
      change('Surface tilt', '6')
      expect(screen.queryByRole('button', { name: 'Use 31 modules' })).toBeNull()
      expect(screen.queryByText(/cold-weather open-circuit voltage is/)).toBeNull()
      cleanup()
    }
  })

  it.each([
    null, {}, { ...coldVoltage(), per_module: '48.3' }, { ...coldVoltage(), string_voltage: Infinity },
    { ...coldVoltage(), max_dc_voltage: NaN }, { ...coldVoltage(), standard_string_length: 32.5 },
    { ...coldVoltage(), suggested_string_length: 31.5 }, { ...coldVoltage(), suggested_string_length: 0 },
    { ...coldVoltage(), suggested_string_length: 32 },
  ])('W1C-34c ignores malformed cold-voltage detail %j', async (detail) => {
    await submitColdRun(coldFailure(detail))
    expect(screen.queryByRole('button', { name: /Use .* modules/ })).toBeNull()
    expect(screen.queryByText(/cold-weather open-circuit voltage is/)).toBeNull()
  })

  it.each(['edit', 'mode', 'scope', 'submit', 'unmount'])('W1C-34c disposes and ignores a late observation after %s', async (action) => {
    const h = await submitColdRun(null)
    const signal = h.client.observeJob.mock.calls[0][1].signal
    if (action === 'edit') change('Surface tilt', '6')
    else if (action === 'mode') change('Scope', 'manual-global')
    else if (action === 'scope') h.view.rerender(<SolarSizingForm {...h.props} solarBinding={r9Next} />)
    else if (action === 'unmount') h.view.unmount()
    else {
      h.client.observeJob.mockReturnValueOnce(new Promise(() => {}))
      await r9Ready()
      fireEvent.click(run())
      await waitFor(() => expect(h.client.observeJob).toHaveBeenCalledTimes(2))
    }
    expect(signal.aborted).toBe(true)
    await act(async () => h.observation.resolve(coldFailure()))
    expect(screen.queryByRole('button', { name: 'Use 31 modules' })).toBeNull()
    expect(screen.queryByText(/cold-weather open-circuit voltage is/)).toBeNull()
  })

  it.each(['complete', 'cancelled', 'running'])('W1C-34c adds no suggestion for a %s outcome', async (status) => {
    const outcome = coldFailure()
    outcome.data.job.status = status
    await submitColdRun(outcome)
    expect(screen.queryByRole('button', { name: 'Use 31 modules' })).toBeNull()
  })

  it.each([true, false])('keeps sizing loaded while busy and re-arms without reading (callback withheld: %s)', async (withheld) => {
    const h = sizingAvailabilityHarness()
    const view = render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    await r14Fill()
    await r9Ready()
    const scopeInput = screen.getByLabelText('Scope')
    const moduleInput = screen.getByLabelText('Module')
    const previews = h.client.catalog.mock.calls.length
    view.rerender(<SolarSizingForm {...h.props} busy
      onProjectSubmit={withheld ? undefined : h.props.onProjectSubmit} />)
    await r9Settle()
    expect(screen.getByLabelText('Scope')).toBe(scopeInput)
    expect(screen.getByLabelText('Module')).toBe(moduleInput)
    for (const [label, value] of Object.entries(fields)) expect(screen.getByLabelText(label).value).toBe(value)
    expect(screen.getByTestId('project-solar-sizing-form').getAttribute('data-mode')).toBe('edit')
    expect(screen.getByText('A Solar run is in progress. Wait for it to finish.')).toBeTruthy()
    expect(screen.queryByText('Project Solar submission is not available in this view.')).toBeNull()
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(previews)

    const preview = r9Deferred()
    h.client.catalog.mockReturnValueOnce(preview.promise)
    view.rerender(<SolarSizingForm {...h.props} />)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(previews + 1))
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    await act(async () => preview.resolve(r9Preview(r9Tool)))
    await r9Ready()
    expect(screen.getByLabelText('Module')).toBe(moduleInput)
    expect(screen.queryByText('A Solar run is in progress. Wait for it to finish.')).toBeNull()
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
  })

  it.each(['onProjectSubmit', 'newProjectIntentKey'])('loads sizing without %s and keeps it loaded when the callback changes', async (callback) => {
    const h = sizingAvailabilityHarness()
    const view = render(<SolarSizingForm {...h.props} {...{ [callback]: undefined }} />)
    await waitFor(() => expect(screen.getByLabelText('Scope')).toBeTruthy())
    const scopeInput = screen.getByLabelText('Scope')
    expect(screen.getByText('Project Solar submission is not available in this view.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).not.toHaveBeenCalled()

    view.rerender(<SolarSizingForm {...h.props} />)
    await r9Ready()
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(1)
    view.rerender(<SolarSizingForm {...h.props} {...{ [callback]: undefined }} />)
    await r9Settle()
    expect(screen.getByLabelText('Scope')).toBe(scopeInput)
    expect(screen.getByText('Project Solar submission is not available in this view.')).toBeTruthy()
    expect(screen.getByTestId('project-solar-sizing-form').getAttribute('data-mode')).toBe('edit')
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(1)
  })

  it('shows run in progress while sizing is submitting before the workspace becomes busy', async () => {
    const h = sizingAvailabilityHarness(), pending = r9Deferred()
    const context = { ok: true, status: 200, data: r9Context() }
    h.client.readContext.mockResolvedValue(context)
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    h.client.readContext.mockReturnValueOnce(pending.promise)
    fireEvent.click(run())
    expect(screen.getByText('A Solar run is in progress. Wait for it to finish.')).toBeTruthy()
    expect(screen.getByLabelText('Scope')).toBeTruthy()
    expect(run().disabled).toBe(true)
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    await act(async () => pending.resolve(context))
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    expect(screen.queryByText('A Solar run is in progress. Wait for it to finish.')).toBeNull()
  })

  it.each([true, false])('keeps sizing loaded while busy and re-arms without reading (callback withheld: %s)', async (withheld) => {
    const h = sizingAvailabilityHarness()
    const view = render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    await r14Fill()
    await r9Ready()
    const scopeInput = screen.getByLabelText('Scope')
    const moduleInput = screen.getByLabelText('Module')
    const previews = h.client.catalog.mock.calls.length
    view.rerender(<SolarSizingForm {...h.props} busy
      onProjectSubmit={withheld ? undefined : h.props.onProjectSubmit} />)
    await r9Settle()
    expect(screen.getByLabelText('Scope')).toBe(scopeInput)
    expect(screen.getByLabelText('Module')).toBe(moduleInput)
    for (const [label, value] of Object.entries(fields)) expect(screen.getByLabelText(label).value).toBe(value)
    expect(screen.getByTestId('project-solar-sizing-form').getAttribute('data-mode')).toBe('edit')
    expect(screen.getByText('A Solar run is in progress. Wait for it to finish.')).toBeTruthy()
    expect(screen.queryByText('Project Solar submission is not available in this view.')).toBeNull()
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(previews)

    const preview = r9Deferred()
    h.client.catalog.mockReturnValueOnce(preview.promise)
    view.rerender(<SolarSizingForm {...h.props} />)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(previews + 1))
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    await act(async () => preview.resolve(r9Preview(r9Tool)))
    await r9Ready()
    expect(screen.getByLabelText('Module')).toBe(moduleInput)
    expect(screen.queryByText('A Solar run is in progress. Wait for it to finish.')).toBeNull()
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
  })

  it.each(['onProjectSubmit', 'newProjectIntentKey'])('loads sizing without %s and keeps it loaded when the callback changes', async (callback) => {
    const h = sizingAvailabilityHarness()
    const view = render(<SolarSizingForm {...h.props} {...{ [callback]: undefined }} />)
    await waitFor(() => expect(screen.getByLabelText('Scope')).toBeTruthy())
    const scopeInput = screen.getByLabelText('Scope')
    expect(screen.getByText('Project Solar submission is not available in this view.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).not.toHaveBeenCalled()

    view.rerender(<SolarSizingForm {...h.props} />)
    await r9Ready()
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(1)
    view.rerender(<SolarSizingForm {...h.props} {...{ [callback]: undefined }} />)
    await r9Settle()
    expect(screen.getByLabelText('Scope')).toBe(scopeInput)
    expect(screen.getByText('Project Solar submission is not available in this view.')).toBeTruthy()
    expect(screen.getByTestId('project-solar-sizing-form').getAttribute('data-mode')).toBe('edit')
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(1)
  })

  it('shows run in progress while sizing is submitting before the workspace becomes busy', async () => {
    const h = sizingAvailabilityHarness(), pending = r9Deferred()
    const context = { ok: true, status: 200, data: r9Context() }
    h.client.readContext.mockResolvedValue(context)
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    h.client.readContext.mockReturnValueOnce(pending.promise)
    fireEvent.click(run())
    expect(screen.getByText('A Solar run is in progress. Wait for it to finish.')).toBeTruthy()
    expect(screen.getByLabelText('Scope')).toBeTruthy()
    expect(run().disabled).toBe(true)
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    await act(async () => pending.resolve(context))
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    expect(screen.queryByText('A Solar run is in progress. Wait for it to finish.')).toBeNull()
  })

  it('R14D-01 test_project_global_cloud_intent', async () => {
    for (const variant of [{}, { tracker: true }, { parameters: true }]) {
      const h = r14Harness()
      await h.real.acquireCheckout()
      h.props.onProjectSubmit = vi.fn((intent, options) => h.real.submitRun(intent, { signal: options.signal }))
      render(<SolarSizingForm {...h.props} />)
      await r9Ready()
      expect(screen.getByLabelText('Scope').value).toBe('manual-global')
      expect(screen.getByLabelText('Scope').disabled).toBe(false)
      expect(screen.queryByRole('option', { name: 'Zones' })).toBeNull()
      expect(screen.getByRole('option', { name: 'Global cloud sizing' })).toBeTruthy()
      await r14Fill(variant)
      await r9Ready()
      const intent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
      const expected = { drawing_id: r9Binding.drawing_id, expected_rev: 7, mode: 'global',
        requests: { [S]: { ...R, racking_params: variant.tracker ? { ...R.racking_params,
          racking_type: 'single_axis', axis_tilt: '0', axis_azimuth: '180', max_angle: '60', gcr: '.4', backtrack: true }
          : R.racking_params,
        ...(variant.parameters ? { module_parameters: Object.fromEntries(MODULE_PARAMETER_KEYS.map(key => [key, key === 'N_s' ? 72 : 1])) } : {}) } },
        grant_ref: 'grant_1', confirm: true }
      expect(intent.params).toEqual(expected)
      expect(intent.binding).toEqual(r9Binding)
      expect(intent.catalogDigest).toBe(r9Digest)
      expect(Object.isFrozen(intent)).toBe(true)
      expect(screen.getByText('Saved project ZIP: 44224-1234')).toBeTruthy()
      expect(screen.getByText('Saved global string length: 3')).toBeTruthy()
      const catalog = h.fetchImpl.mock.calls.filter(([url]) => url.includes('/api/capabilities?')).at(-1)
      expect(catalog[1].method).toBe('POST')
      expect(new URL(catalog[0], 'https://example.test').searchParams.has('project_runs')).toBe(false)
      const preview = JSON.parse(catalog[1].body).project_runs
      expect(preview[r9Tool]).toEqual({ params: expected, catalog_digest: r9Digest, idempotency_key: intent.idempotencyKey })
      fireEvent.click(run())
      await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
      expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
      const submitted = h.fetchImpl.mock.calls.find(([url]) => url.endsWith('/api/run'))[1]
      expect(JSON.parse(submitted.body)).toEqual({ tool: r9Tool, params: expected,
        dwg: r9Binding.input_version_id, catalog_digest: r9Digest })
      expect(submitted.headers).toMatchObject({ 'Idempotency-Key': intent.idempotencyKey,
        'X-Org-Id': r9Binding.organization_id, 'X-Project-Id': r9Binding.project_id })
      expect(h.props.onSubmit).not.toHaveBeenCalled()
      expect(h.props.readIntake).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R14D-02 test_cloud_draft_edit_invalidates_preview', async () => {
    const labels = [...Object.keys(fields), ...Object.keys(r14Tracker), 'Use module parameters', ...MODULE_PARAMETER_KEYS]
    expect(labels).toHaveLength(28)
    for (const mode of ['global', 'manual-global']) {
      for (const label of labels) {
        const h = r14Harness()
        render(<SolarSizingForm {...h.props} />)
        await r9Ready()
        await r14Fill({ tracker: true, parameters: true })
        await r9Ready()
        const old = r9Deferred(), current = r9Deferred()
        h.client.catalog.mockReturnValueOnce(old.promise).mockReturnValue(current.promise)
        fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }))
        await r9Settle()
        const oldIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
        const control = screen.getByLabelText(label)
        const original = control.value
        if (label === 'Use module parameters') {
          fireEvent.click(control); fireEvent.click(control)
        } else {
          const replacement = control.tagName === 'SELECT'
            ? (label === 'Racking' ? 'fixed_tilt' : original === 'true' ? 'false' : 'true')
            : label === 'N_s' ? '73' : original + '2'
          change(label, replacement)
          change(label, original)
          if (label === 'Module') await chooseTypedModule()
        }
        if (mode === 'manual-global') change('Scope', mode)
        expect(run().disabled).toBe(true)
        await act(async () => { old.resolve(r9Preview(r9Tool)); await Promise.resolve() })
        expect(run().disabled).toBe(true)
        fireEvent.click(run())
        expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
        const nextIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
        expect(nextIntent).not.toBe(oldIntent)
        expect(nextIntent.idempotencyKey).not.toBe(oldIntent.idempotencyKey)
        await act(async () => current.resolve(r9Preview(r9Tool)))
        await r9Ready()
        cleanup()
      }
    }
    // Hidden tracker and module fields remain part of the raw declaration, even in manual mode.
    const source = readFileSync(resolve(process.cwd(), 'src/solar/SolarSizingForm.jsx'), 'utf8')
    const declaration = source.slice(source.indexOf('const declarationIdentity ='), source.indexOf('const form = useProjectSizingConductor', source.indexOf('const declarationIdentity =')))
    const keys = ['module_name', 'module_confirmed', 'full_inverter_name', 'bifacial', 'bifacial_coefficient', 'racking_type',
      'surface_tilt', 'surface_azimuth', 'albedo', 'axis_tilt', 'axis_azimuth', 'max_angle', 'gcr', 'backtrack',
      'max_voltage', 'thermal_model_type', 'open_circuit_rise', 'grant_ref', 'use_module_parameters']
    expect(declaration.match(/'[^']+'/g).map(value => value.slice(1, -1))).toEqual([...keys, 'accepted_string_length'])
    expect(declaration).toContain('JSON.stringify([mode,')
    expect(declaration).toContain('...MODULE_PARAMETER_KEYS')
    expect(declaration).toContain('draft[key] ?? null')
    // Exercise raw updates to currently hidden fields through the real lifecycle as well.
    const rawDraft = { module_name: R.module_name, module_confirmed: R.module_name, full_inverter_name: R.full_inverter_name,
      bifacial: false, bifacial_coefficient: '.7', racking_type: 'fixed_tilt', surface_tilt: '5',
      surface_azimuth: '180', albedo: '.25', axis_tilt: '0', axis_azimuth: '180', max_angle: '60',
      gcr: '.4', backtrack: true, max_voltage: '1500', thermal_model_type: R.thermal_model_type,
      open_circuit_rise: false, grant_ref: 'grant_1', use_module_parameters: false,
      ...Object.fromEntries(MODULE_PARAMETER_KEYS.map(key => [key, key === 'N_s' ? '72' : '1'])) }
    for (const mode of ['manual-global', 'global']) {
      for (const key of [...keys, ...MODULE_PARAMETER_KEYS]) {
        const h = r14Harness(), old = r9Deferred(), current = r9Deferred()
        h.client.catalog.mockReturnValueOnce(old.promise).mockReturnValue(current.promise)
        const hook = renderHook(({ draft }) => useProjectSizingConductor(h.props, {
          toolName: r9Tool, deriveState: deriveProjectSizingState,
          buildRequest: args => buildProjectSizingRequest({ ...args, mode, draft }),
          declarationIdentity: JSON.stringify([mode, ...[...keys, ...MODULE_PARAMETER_KEYS].map(field => draft[field] ?? null)]),
        }), { initialProps: { draft: rawDraft } })
        await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
        const oldIntent = h.client.catalog.mock.calls[0][0].intents[0]
        const value = rawDraft[key]
        const next = typeof value === 'boolean' ? !value
          : key === 'racking_type' ? 'single_axis' : key === 'N_s' ? '73' : value + '2'
        act(() => hook.rerender({ draft: { ...rawDraft, [key]: next } }))
        const hidden = mode === 'manual-global' || ['axis_tilt', 'axis_azimuth', 'max_angle', 'gcr', 'backtrack', ...MODULE_PARAMETER_KEYS].includes(key)
        if (hidden) expect(h.client.catalog.mock.calls.at(-1)[0].intents[0].params).toEqual(oldIntent.params)
        act(() => hook.rerender({ draft: rawDraft }))
        await act(async () => old.resolve(r9Preview(r9Tool)))
        expect(hook.result.current.armed).toBeFalsy()
        await act(async () => hook.result.current.submit())
        expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
        const nextIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
        expect(nextIntent.params).toEqual(oldIntent.params)
        expect(nextIntent).not.toBe(oldIntent)
        expect(nextIntent.idempotencyKey).not.toBe(oldIntent.idempotencyKey)
        await act(async () => current.resolve(r9Preview(r9Tool)))
        expect(hook.result.current.armed).toBeTruthy()
        hook.unmount()
      }
    }
  }, 30000)

  it('R14D-03 test_missing_grant_keeps_cloud_mode', async () => {
    for (const phase of ['preview', 'submission']) {
      const h = r14Harness()
      render(<SolarSizingForm {...h.props} />)
      await r9Ready()
      const refusal = { ok: false, status: 401, envelope: { error: {
        reason_code: 'CLOUD_AUTH_MISSING', message: 'hostile server prose' } } }
      if (phase === 'preview') {
        h.client.catalog.mockResolvedValue(r9Preview(r9Tool, r9Digest, { runnable: false,
          refusal_reasons: ['CLOUD_AUTH_MISSING'], admission: { status_code: 401, error: refusal.envelope.error } }))
      } else h.props.onProjectSubmit.mockResolvedValue(refusal)
      await r14Fill()
      change('Grant reference', 'unavailable_grant')
      if (phase === 'submission') { await r9Ready(); fireEvent.click(run()) }
      await waitFor(() => expect(screen.getByTestId('project-solar-sizing-feedback').textContent)
        .toBe(SOLAR_SIZING_RUN_REASONS.grant_missing + ' (CLOUD_AUTH_MISSING)'))
      expect(screen.getByLabelText('Scope').value).toBe('global')
      for (const [label, value] of Object.entries(fields)) {
        expect(screen.getByLabelText(label).value).toBe(label === 'Grant reference' ? 'unavailable_grant' : value)
      }
      expect(screen.queryByText('hostile server prose')).toBeNull()
      if (phase === 'preview') {
        expect(run().disabled).toBe(true)
        fireEvent.click(run())
        expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
      } else {
        expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
        expect(h.props.onProjectSubmit.mock.calls[0][0].params.mode).toBe('global')
      }
      expect(h.props.onSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R14D-05 test_unknown_submission_reuses_intent', async () => {
    for (const stale of [false, true]) {
      const h = r14Harness()
      h.props.onProjectSubmit.mockResolvedValueOnce({ ok: false, status: null, reason: 'SIP_R7_TIMEOUT', envelope: null })
        .mockResolvedValueOnce(r9Submitted())
      render(<SolarSizingForm {...h.props} />)
      await r9Ready()
      await r14Fill()
      await r9Ready()
      const intent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
      const allocations = h.props.newProjectIntentKey.mock.calls.length
      const reads = h.client.readContext.mock.calls.length
      fireEvent.click(run())
      await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
      expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
      expect(screen.getByLabelText('Scope').value).toBe('global')
      for (const [label, value] of Object.entries(fields)) expect(screen.getByLabelText(label).value).toBe(value)
      expect(screen.getByTestId('project-solar-sizing-feedback').textContent)
        .toContain(PROJECT_SIZING_CONDUCTOR_REASONS.request_timeout)
      if (stale) {
        h.context.context.head_version_id = r9Next.input_version_id
        h.context.context.is_head = false
      }
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      if (stale) {
        await waitFor(() => expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.not_current_head)).toBeTruthy())
        expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
      } else {
        await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
        expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2)
        expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(intent)
        expect(h.props.onProjectSubmit.mock.calls[1][0].params).toBe(intent.params)
        expect(h.props.onProjectSubmit.mock.calls[1][0].idempotencyKey).toBe(intent.idempotencyKey)
      }
      expect(h.client.readContext).toHaveBeenCalledTimes(reads + 2)
      expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(allocations)
      expect(h.props.onSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R9-46 a conflicting project id refuses before context preview or handoff', async () => {
    const h = r9Harness()
    render(<SolarSizingForm {...h.props} projectId="99999999-9999-9999-9999-999999999999" />)
    await r9Settle()
    expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid)).toBeTruthy()
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(h.client.readContext).not.toHaveBeenCalled()
    expect(h.client.catalog).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
  })

  it('R9-48 a truthy nonboolean ok cannot report a submitted request and offers retry', async () => {
    const h = r9Harness()
    h.props.onProjectSubmit.mockResolvedValue({ ok: 'false', status: 202,
      data: { status: 'submitted', job_id: r9Job } })
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    expect(screen.queryByText('Request submitted.')).toBeNull()
    expect(screen.getByTestId('project-solar-sizing-feedback').textContent)
      .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.request_invalid)
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
  })

  it('R9-49 a bare successful 202 offers explicit retry of the same prepared intent', async () => {
    const h = r9Harness()
    h.props.onProjectSubmit.mockResolvedValueOnce({ ok: true, status: 202 })
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    fireEvent.click(run())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    expect(screen.getByTestId('project-solar-sizing-feedback').textContent)
      .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.request_invalid)
    expect(screen.queryByText('Request submitted.')).toBeNull()
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2)
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(intent)
    expect(h.client.readContext).toHaveBeenCalledTimes(3)
    expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    expect(h.client.catalog).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('R9-51 object and string saved lengths refuse without rendering the server value', async () => {
    let view
    for (const length of [{ bad: 3 }, '12']) {
      const h = r9Harness()
      h.context.intake.solar_design_graph.settings.panels_in_sequence = length
      expect(() => {
        if (view) view.rerender(<SolarSizingForm {...h.props} />)
        else view = render(<SolarSizingForm {...h.props} />)
      }).not.toThrow()
      await waitFor(() => expect(screen.getByText('Save a whole-number string length from 1 to 4096 in Solar settings.')).toBeTruthy())
      expect(screen.queryByText(/Saved global string length/)).toBeNull()
      expect(run().disabled).toBe(true)
      expect(h.client.readContext).toHaveBeenCalledTimes(1)
      expect(h.client.catalog).not.toHaveBeenCalled()
      fireEvent.click(run())
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    }
  })

  it('R9-52 an older refused preview cannot replace newer accepted feedback or handoff', async () => {
    const h = r9Harness(), older = r9Deferred(), newer = r9Deferred()
    h.previewQueue.push(older.promise, newer.promise)
    const view = render(<SolarSizingForm {...h.props} />)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
    const oldIntent = h.client.catalog.mock.calls[0][0].intents[0]
    h.props = { ...h.props, catalogDigest: 'sha256:' + 'e'.repeat(64) }
    view.rerender(<SolarSizingForm {...h.props} />)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(2))
    const intent = h.client.catalog.mock.calls[1][0].intents[0]
    expect(intent).not.toBe(oldIntent)
    expect(h.client.catalog.mock.calls[0][0].signal.aborted).toBe(true)
    await act(async () => newer.resolve(r9Preview(r9Tool, h.props.catalogDigest)))
    await r9Ready()
    expect(screen.queryByTestId('project-solar-sizing-feedback')).toBeNull()
    await act(async () => older.resolve(r9Preview(r9Tool, r9Digest, {
      runnable: false, admission: { status_code: 409, error: { reason_code: 'OLD_PREVIEW_REFUSAL' } },
      refusal_reasons: ['OLD_PREVIEW_REFUSAL'],
    })))
    expect(screen.queryByTestId('project-solar-sizing-feedback')).toBeNull()
    expect(screen.queryByText(PROJECT_SIZING_CONDUCTOR_REASONS.preview_refused)).toBeNull()
    expect(run().disabled).toBe(false)
    fireEvent.click(run())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(2)
  })

  it('R9-53 a submitted envelope at HTTP 200 cannot report success and offers retry', async () => {
    const h = r9Harness()
    h.props.onProjectSubmit.mockResolvedValue({ ...r9Submitted(), status: 200 })
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    expect(screen.queryByText('Request submitted.')).toBeNull()
    expect(screen.getByTestId('project-solar-sizing-feedback').textContent)
      .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.request_invalid)
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
  })

  it('R9-54 the step editor project marker true admits the canonical binding through the handoff', async () => {
    const h = r9Harness()
    render(<SolarSizingForm {...h.props} projectId />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    fireEvent.click(run())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(h.props.readIntake).not.toHaveBeenCalled()
    for (const projectId of [false, 1, {}, '']) {
      cleanup()
      const refused = r9Harness()
      render(<SolarSizingForm {...refused.props} projectId={projectId} />)
      await r9Settle()
      expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid)).toBeTruthy()
      expect(refused.client.readContext).not.toHaveBeenCalled()
      expect(refused.client.catalog).not.toHaveBeenCalled()
      expect(refused.props.onProjectSubmit).not.toHaveBeenCalled()
    }
  })

  it('R9-05 an own undefined binding refuses without a standalone fallback', async () => {
    const h = r9Harness()
    render(<SolarSizingForm {...h.props} solarBinding={undefined} drawingId="d1" drawingVersion={3} />)
    await r9Settle()
    expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid)).toBeTruthy()
    expect(h.client.readContext).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    cleanup()
    const malformed = r9Harness()
    render(<SolarSizingForm {...malformed.props} solarBinding={{ ...r9Binding, drawing_id: r9Binding.drawing_id + '\n' }} />)
    await r9Settle()
    expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid)).toBeTruthy()
    expect(malformed.client.readContext).not.toHaveBeenCalled()
    expect(malformed.props.readIntake).not.toHaveBeenCalled()
    cleanup()
    for (const dependency of ['authorityKey', 'newProjectIntentKey', 'onProjectSubmit',
      'readContext', 'prepareRun', 'catalog', 'captureCatalogScope', 'subscribe', 'getSnapshot']) {
      const missing = r9Harness()
      if (['authorityKey', 'newProjectIntentKey', 'onProjectSubmit'].includes(dependency)) missing.props[dependency] = null
      else missing.props.projectClient = { ...missing.client, [dependency]: undefined }
      render(<SolarSizingForm {...missing.props} />)
      await r9Settle()
      expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.submit_unavailable)).toBeTruthy()
      if (['newProjectIntentKey', 'onProjectSubmit'].includes(dependency)) {
        expect(missing.client.readContext).toHaveBeenCalledTimes(1)
        expect(screen.getByLabelText('Scope')).toBeTruthy()
      } else expect(missing.client.readContext).not.toHaveBeenCalled()
      expect(missing.props.readIntake).not.toHaveBeenCalled()
      expect(missing.props.onSubmit).not.toHaveBeenCalled()
      expect(run().disabled).toBe(true)
      cleanup()
    }
  })

  it('R9-14 server nonmanual service evidence refusal displays the fixed manual sentence', async () => {
    const h = r9Harness()
    h.preview = r9Preview(r9Tool, r9Digest, { runnable: false, admission: {
      status_code: 409, error: { reason_code: 'SIP_R3_SERVICE_EVIDENCE_REQUIRED' } }, refusal_reasons: ['SIP_R3_SERVICE_EVIDENCE_REQUIRED'] })
    render(<SolarSizingForm {...h.props} />)
    await waitFor(() => expect(screen.getByTestId('project-solar-sizing-feedback').textContent)
      .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.manual_only + ' (SIP_R3_SERVICE_EVIDENCE_REQUIRED)'))
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Grant reference')).toBeNull()
  })

  it('R9-23 only the exact accepted prepared object is armed and handed off', async () => {
    const h = r9Harness()
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    expect(h.client.prepareRun).toHaveBeenCalledTimes(1)
    fireEvent.click(run())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(h.props.onProjectSubmit.mock.calls[0][1]).toMatchObject({ binding: r9Binding, authorityKey: 'epoch-1' })
    expect(h.props.onProjectSubmit.mock.calls[0][1].isCurrent()).toBe(true)
    expect(h.props.readIntake).not.toHaveBeenCalled()
  })

  it('R9-24 a successful HTTP envelope cannot replace complete exact admission', async () => {
    const accepted = r9Preview(r9Tool).data.families[0].capabilities[0]
    const variants = [
      r9Preview(r9Tool, r9Digest, { admission_checked: false }),
      r9Preview(r9Tool, r9Digest, { admission: { status_code: 200 } }),
      r9Preview(r9Tool, r9Digest, { refusal_reasons: ['project_request_required'] }),
      r9Preview(r9Tool, 'sha256:' + 'e'.repeat(64)),
      { ok: true, status: 200, data: { families: [] } },
      { ok: true, status: 200, data: { families: [{ capabilities: [accepted,
        { ...accepted, availability: { ...accepted.availability, runnable: false } }] }] } },
      r9Preview(r9Tool, r9Digest, { admission: { status_code: 409, error: null } }),
      r9Preview(r9Tool, r9Digest, { admission: { status_code: 200, error: {} } }),
      r9Preview(r9Tool, r9Digest, { refusal_reasons: null }),
      { ok: true, status: 200, data: { families: [{ capabilities: [accepted,
        { ...accepted, catalog_digest: 'sha256:' + 'e'.repeat(64) }] }] } },
    ]
    for (const key of ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable', 'admission_checked']) {
      for (const value of [false, undefined]) variants.push(r9Preview(r9Tool, r9Digest, { [key]: value }))
    }
    for (const preview of variants) {
      const h = r9Harness(); h.preview = preview
      h.previewQueue.push(preview)
      render(<SolarSizingForm {...h.props} />)
      await waitFor(() => expect(screen.getByTestId('project-solar-sizing-feedback')).toBeTruthy())
      expect(run().disabled).toBe(true)
      fireEvent.click(run())
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R9-25 real R7 preparation freezes exactly the six canonical intent keys', async () => {
    const h = r9Harness()
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    expect(Object.keys(intent)).toEqual(['binding', 'authorityKey', 'tool', 'params', 'catalogDigest', 'idempotencyKey'])
    expect(Object.isFrozen(intent)).toBe(true)
    expect(Object.isFrozen(intent.params)).toBe(true)
    expect(Object.isFrozen(intent.binding)).toBe(true)
    expect(intent.params).toEqual({ drawing_id: r9Binding.drawing_id, expected_rev: 7, mode: 'manual-global', confirm: true })
  })

  it('R9-26 an R7 authority ABA excursion permanently invalidates its captured predicate', async () => {
    const h = r9Harness()
    const predicate = h.client.captureCatalogScope()
    h.real.setScope(r9Binding, 'epoch-2')
    h.real.setScope(r9Binding, 'epoch-1')
    expect(predicate()).toBe(false)
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const nextPredicate = h.client.captureCatalogScope()
    expect(nextPredicate()).toBe(true)
  })

  it('R9-27 prop binding authority and client changes discard stale awaited results', async () => {
    for (const phase of ['read', 'preview', 'final', 'callback']) {
      for (const changeScope of ['binding', 'authority', 'client']) {
        for (const settlement of ['success', 'failure']) {
          const h = r9Harness(), late = r9Deferred()
          if (phase === 'read') h.contextQueue.push(late.promise)
          if (phase === 'preview') h.previewQueue.push(late.promise)
          if (phase === 'callback') h.props.onProjectSubmit.mockImplementationOnce(() => late.promise)
          const view = render(<SolarSizingForm {...h.props} />)
          await r9Settle()
          if (phase === 'read') await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1))
          else if (phase === 'preview') await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
          else {
            await r9Ready()
            if (phase === 'final') h.contextQueue.push(late.promise)
            fireEvent.click(run())
            await r9Settle()
            await waitFor(() => expect(phase === 'final' ? h.client.readContext.mock.calls.length
              : h.props.onProjectSubmit.mock.calls.length).toBe(phase === 'final' ? 2 : 1))
          }
          const oldSignal = phase === 'preview' ? h.client.catalog.mock.calls[0][0].signal
            : phase === 'callback' ? h.props.onProjectSubmit.mock.calls[0][1].signal
              : h.client.readContext.mock.calls.at(-1)[0].signal
          if (changeScope === 'client') {
            const replacement = r9Harness()
            view.rerender(<SolarSizingForm {...replacement.props} />)
          } else r9Rebind(view, h, changeScope === 'binding' ? { solarBinding: r9Next } : { authorityKey: 'epoch-2' })
          expect(oldSignal.aborted).toBe(true)
          await r9Settle()
          await r9Ready()
          const calls = h.props.onProjectSubmit.mock.calls.length
          await act(async () => {
            if (settlement === 'failure') late.reject(new Error('old scope failure'))
            else late.resolve(phase === 'preview' ? r9Preview(r9Tool)
              : phase === 'callback' ? r9Submitted() : { ok: true, status: 200, data: r9Context() })
          })
          expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(calls)
          expect(screen.queryByText('Request submitted.')).toBeNull()
          cleanup()
        }
      }
    }
  })

  it('R9-28 a client mutation between promise resolution and continuation rejects settlement', async () => {
    const h = r9Harness(), late = r9Deferred()
    h.previewQueue.push(late.promise)
    render(<SolarSizingForm {...h.props} />)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
    const oldIntent = h.client.catalog.mock.calls[0][0].intents[0]
    await act(async () => {
      late.resolve(r9Preview(r9Tool))
      h.real.setScope(r9Next, 'epoch-1')
    })
    expect(run().disabled).toBe(true)
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(h.client.catalog.mock.calls[0][0].intents[0]).toBe(oldIntent)
  })

  it('R9-29 an unsettled initial read restarts after lease rotation and client-only ABA', async () => {
    for (const rotate of ['lease', 'aba']) {
      const h = r9Harness(), late = r9Deferred()
      h.contextQueue.push(late.promise)
      render(<SolarSizingForm {...h.props} />)
      await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1))
      const signal = h.client.readContext.mock.calls[0][0].signal
      await act(async () => {
        if (rotate === 'lease') await h.real.acquireCheckout()
        else { h.real.setScope(r9Next, 'epoch-1'); h.real.setScope(r9Binding, 'epoch-1') }
      })
      await r9Ready()
      expect(signal.aborted).toBe(true)
      expect(h.client.readContext).toHaveBeenCalledTimes(2)
      await act(async () => late.resolve({ ok: false, status: 500, reason: 'OLD' }))
      expect(run().disabled).toBe(false)
      expect(screen.queryByTestId('project-solar-sizing-feedback')).toBeNull()
      cleanup()
    }
  })

  it('R9-30 lease rotation withdraws readiness and previews the same retained intent and key', async () => {
    const h = r9Harness()
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    const late = r9Deferred(); h.previewQueue.push(late.promise)
    await act(async () => h.real.acquireCheckout())
    expect(run().disabled).toBe(true)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(2))
    expect(h.client.catalog.mock.calls[1][0].intents[0]).toBe(intent)
    expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    await act(async () => late.resolve(r9Preview(r9Tool)))
    await r9Ready()
  })

  it('R9-31 changed catalog digests invalidate the preview and mint a new valid candidate', async () => {
    const h = r9Harness(), late = r9Deferred()
    const view = render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const oldIntent = h.client.catalog.mock.calls[0][0].intents[0]
    h.previewQueue.push(late.promise)
    h.props = { ...h.props, catalogDigest: 'sha256:' + 'e'.repeat(64) }
    view.rerender(<SolarSizingForm {...h.props} />)
    expect(run().disabled).toBe(true)
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(2))
    const intent = h.client.catalog.mock.calls[1][0].intents[0]
    expect(intent).not.toBe(oldIntent)
    expect(intent.idempotencyKey).not.toBe(oldIntent.idempotencyKey)
    await act(async () => late.resolve(r9Preview(r9Tool, h.props.catalogDigest)))
    await r9Ready()
  })

  it('R9-32 key factory identity alone retains the prepared candidate and key', async () => {
    const h = r9Harness()
    const view = render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    const factory = vi.fn(() => 'new-function-key')
    h.props = { ...h.props, newProjectIntentKey: factory }
    view.rerender(<SolarSizingForm {...h.props} />)
    await r9Settle()
    expect(factory).not.toHaveBeenCalled()
    expect(h.client.catalog).toHaveBeenCalledTimes(1)
    fireEvent.click(run())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
  })

  it('R9-33 final rereads refuse head identity intake graph digest and revision changes', async () => {
    for (const mutation of ['head', 'binding', 'intake', 'graph', 'rev']) {
      const h = r9Harness()
      render(<SolarSizingForm {...h.props} />)
      await r9Ready()
      const intent = h.client.catalog.mock.calls[0][0].intents[0]
      const changed = r9Context()
      if (mutation === 'head') { changed.context.head_version_id = r9Next.input_version_id; changed.context.is_head = false }
      if (mutation === 'binding') changed.context.drawing_id = '66666666-6666-6666-6666-666666666666'
      if (mutation === 'intake') changed.context.intake_sha256 = 'e'.repeat(64)
      if (mutation === 'graph') changed.intake.solar_design_graph_sha256 = 'e'.repeat(64)
      if (mutation === 'rev') changed.intake.solar_design_graph.rev = 8
      h.contextQueue.push({ ok: true, status: 200, data: changed })
      fireEvent.click(run())
      await waitFor(() => expect(screen.getByText(mutation === 'head'
        ? PROJECT_SIZING_CONDUCTOR_REASONS.not_current_head : PROJECT_SIZING_CONDUCTOR_REASONS.scope_stale)).toBeTruthy())
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
      expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
      expect(h.client.catalog.mock.calls[0][0].intents[0]).toBe(intent)
      cleanup()
    }
  })

  it('R9-34 double clicks and unrelated parent renders hold the callback lock through settlement', async () => {
    const h = r9Harness(), late = r9Deferred()
    h.props.onProjectSubmit.mockImplementationOnce(() => late.promise)
    const view = render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const button = run()
    act(() => { button.click(); button.click() })
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    view.rerender(<SolarSizingForm {...h.props} status="finished" failureCode="unsafe" />)
    fireEvent.click(run())
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
    expect(run().disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(2)
    await act(async () => late.resolve(r9Submitted()))
    expect(screen.getByText('Request submitted.')).toBeTruthy()
    cleanup()
    const next = r9Harness(), oldCallback = r9Deferred(), newCallback = r9Deferred()
    next.props.onProjectSubmit.mockImplementationOnce(() => oldCallback.promise)
      .mockImplementationOnce(() => newCallback.promise)
    const nextView = render(<SolarSizingForm {...next.props} />)
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(next.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    r9Rebind(nextView, next, { solarBinding: r9Next })
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(next.props.onProjectSubmit).toHaveBeenCalledTimes(2))
    await act(async () => oldCallback.resolve(r9Submitted()))
    expect(run().disabled).toBe(true)
    expect(screen.queryByText('Request submitted.')).toBeNull()
    fireEvent.click(run())
    expect(next.props.onProjectSubmit).toHaveBeenCalledTimes(2)
    await act(async () => newCallback.resolve(r9Submitted()))
    expect(screen.getByText('Request submitted.')).toBeTruthy()
  })

  it('R9-35 real R7 transport emits exact context catalog and canonical run bodies with private headers', async () => {
    const h = r9Harness()
    await h.real.acquireCheckout()
    h.props.onProjectSubmit = vi.fn((intent, options) => h.real.submitRun(intent, { signal: options.signal }))
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    fireEvent.click(run())
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    const requests = h.fetchImpl.mock.calls
    const contexts = requests.filter(([url]) => url.includes('/context?'))
    expect(contexts).toHaveLength(2)
    expect(contexts[0][0]).toContain('/drawing-versions/' + r9Binding.input_version_id + '/context?drawing_id=' + r9Binding.drawing_id)
    const catalog = requests.find(([url]) => url.includes('/api/capabilities?'))
    expect(catalog[1].method).toBe('POST')
    expect(new URL(catalog[0], 'https://example.test').searchParams.has('project_runs')).toBe(false)
    const preview = JSON.parse(catalog[1].body).project_runs
    expect(preview[r9Tool]).toEqual({ params: h.client.catalog.mock.calls[0][0].intents[0].params,
      catalog_digest: r9Digest, idempotency_key: 'r9-key-1' })
    const submitted = requests.find(([url]) => url.endsWith('/api/run'))[1]
    expect(JSON.parse(submitted.body)).toEqual({ tool: r9Tool,
      params: { drawing_id: r9Binding.drawing_id, expected_rev: 7, mode: 'manual-global', confirm: true },
      dwg: r9Binding.input_version_id, catalog_digest: r9Digest })
    expect(submitted.headers).toMatchObject({ Authorization: 'tok', 'X-Org-Id': r9Binding.organization_id,
      'X-Project-Id': r9Binding.project_id, 'X-Tenant-Id': r9Binding.organization_id,
      'Idempotency-Key': 'r9-key-1', 'X-Checkout-Capability': 'lco1.' + 'c'.repeat(64) })
    expect(h.client.setScope).not.toHaveBeenCalled()
    expect(h.client.dispose).not.toHaveBeenCalled()
    expect(h.client.acquireCheckout).not.toHaveBeenCalled()
    expect(h.client.releaseCheckout).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
  })

  it('R9-36 submitted feedback requires the entire valid envelope rather than a bare 202', async () => {
    for (const result of [r9Submitted(), undefined, null, { ok: true, status: 202 }, { ok: true, status: 202, data: { status: 'submitted', job_id: 'BAD' } },
      { ok: true, status: 202, data: { status: 'submitted', job_id: r9Job + '\n' } }]) {
      const h = r9Harness(); h.props.onProjectSubmit.mockResolvedValue(result)
      render(<SolarSizingForm {...h.props} />)
      await r9Ready()
      fireEvent.click(run())
      await waitFor(() => expect(screen.getByTestId('project-solar-sizing-feedback')).toBeTruthy())
      expect(screen.getByTestId('project-solar-sizing-feedback').textContent).toBe(result?.data?.job_id === r9Job
        ? 'Request submitted.' : PROJECT_SIZING_CONDUCTOR_REASONS.request_invalid)
      expect(screen.queryByText('String sizing applied.')).toBeNull()
      cleanup()
    }
  })

  it('R9-37 timeout retains intent inputs and key for an explicit retry with fresh checks', async () => {
    const h = r9Harness()
    h.props.onProjectSubmit.mockResolvedValueOnce({ ok: false, status: null, reason: 'SIP_R7_TIMEOUT', envelope: null })
      .mockResolvedValueOnce(r9Submitted())
    render(<SolarSizingForm {...h.props} />)
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    fireEvent.click(run())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    expect(screen.getByText('Saved global string length: 3')).toBeTruthy()
    expect(screen.getByTestId('project-solar-sizing-feedback').textContent).toContain(PROJECT_SIZING_CONDUCTOR_REASONS.request_timeout)
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    expect(h.props.onProjectSubmit.mock.calls[1][0]).toBe(intent)
    expect(h.client.readContext).toHaveBeenCalledTimes(3)
    expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    cleanup()
    const moved = r9Harness()
    moved.props.onProjectSubmit.mockResolvedValue({ ok: false, status: null, reason: 'SIP_R7_TIMEOUT', envelope: null })
    render(<SolarSizingForm {...moved.props} />)
    await r9Ready()
    const retained = moved.client.catalog.mock.calls[0][0].intents[0]
    fireEvent.click(run())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    moved.context.context.head_version_id = r9Next.input_version_id
    moved.context.context.is_head = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.not_current_head)).toBeTruthy())
    expect(moved.props.onProjectSubmit).toHaveBeenCalledTimes(1)
    expect(moved.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    expect(moved.client.catalog.mock.calls[0][0].intents[0]).toBe(retained)
  })

  it('R9-38 bounded safe codes choose fixed feedback while preserving outcome and admission', async () => {
    const known = [
      ['SIP_R3_TOOL_UNSUPPORTED', 'project_adapter_unavailable'],
      ['SIP_R3_SERVICE_EVIDENCE_REQUIRED', 'manual_only'], ['CHECKOUT_EXPIRED', 'checkout_required'],
      ['SIP_R1_CHECKOUT_REQUIRED', 'checkout_required'], ['SIP_R1_CHECKOUT_EXPIRED', 'checkout_required'],
      ['SIP_R1_CHECKOUT_STALE', 'checkout_required'],
      ['SIP_R7_SCOPE_STALE', 'scope_stale'], ['SIP_R7_TIMEOUT', 'request_timeout'],
      ['SIP_R7_ABORTED', 'request_cancelled'], ['SIP_R7_TRANSPORT_FAILED', 'service_unavailable'],
      ['SIP_R7_RESPONSE_INVALID', 'request_invalid'],
    ]
    for (const [reason, key] of known) {
      const outcome = { ok: false, status: null, reason, envelope: null }
      expect(projectSizingConductorFeedback(outcome)).toMatchObject({ outcome, reason: key,
        text: projectSizingConductorReason(key), code: reason })
    }
    for (const [status, key] of [[401, 'access_denied'], [403, 'access_denied'], [503, 'service_unavailable']]) {
      expect(projectSizingConductorFeedback({ ok: false, status }).text).toBe(projectSizingConductorReason(key))
    }
    for (const code of ['STALE_GRAPH_REVISION', 'dotted.code', null, '<unsafe>', 'SAFE\n']) {
      const h = r9Harness()
      const admission = { status_code: 409, error: { reason_code: code, message: 'arbitrary server prose' } }
      h.preview = r9Preview(r9Tool, r9Digest, { admission, runnable: false, refusal_reasons: ['refused'] })
      render(<SolarSizingForm {...h.props} />)
      await waitFor(() => expect(screen.getByTestId('project-solar-sizing-feedback')).toBeTruthy())
      expect(screen.getByTestId('project-solar-sizing-feedback').textContent).toBe(PROJECT_SIZING_CONDUCTOR_REASONS.preview_refused
        + (code === 'STALE_GRAPH_REVISION' ? ' (STALE_GRAPH_REVISION)' : ''))
      const feedback = projectSizingConductorFeedback(h.preview, admission)
      expect(feedback.outcome).toBe(h.preview)
      expect(feedback.admission).toBe(admission)
      expect(screen.queryByText('arbitrary server prose')).toBeNull()
      cleanup()
    }
  })
  it('W21C2-manual-default', async () => {
    const graph = G7()
    graph.settings.panels_in_sequence = 3
    graph.project.zip_code = ''
    const { props } = await mount(graph, {}, 'manual-global')
    expect(screen.getByLabelText('Scope').value).toBe('manual-global')
    expect(screen.getByRole('option', { name: 'Saved global length' })).toBeTruthy()
    expect(screen.getByText('Saved global string length: 3')).toBeTruthy()
    expect(screen.getByText('Saved project ZIP:')).toBeTruthy()
    expect(screen.getByText('Change the length in Solar settings.')).toBeTruthy()
    expect(screen.getByText('Manual confirmation does not calculate voltage limits or module power.')).toBeTruthy()
    for (const label of [...Object.keys(fields), 'Use module parameters']) {
      expect(screen.queryByLabelText(label)).toBeNull()
    }
    expect(screen.queryByText('Confirmed module power is unavailable. Re-size strings.')).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(run()); fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, {
      expected_rev: 7, mode: 'manual-global', confirm: true,
    })
  })

  it('W21C2-manual-invalid-length', async () => {
    for (const length of [undefined, null, true, '3', 0, -1, 1.5, Infinity, 4097]) {
      const graph = G7()
      graph.settings.panels_in_sequence = length
      const { props } = await mount(graph, {}, 'manual-global')
      expect(screen.getByText('Save a whole-number string length from 1 to 4096 in Solar settings.')).toBeTruthy()
      expect(run().disabled).toBe(true)
      fireEvent.click(run())
      expect(props.onSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('W21C2-manual-lifecycle', async () => {
    const graph = G7()
    graph.settings.panels_in_sequence = 3
    const updated = G7()
    updated.rev = 8
    updated.settings.panels_in_sequence = 12
    const readIntake = vi.fn().mockResolvedValueOnce(view(graph)).mockResolvedValueOnce(view(updated))
    const onClose = vi.fn()
    const { props, rerender } = await mount(graph, { readIntake, onClose }, 'manual-global')
    rerender(<SolarSizingForm {...props} status="pending" />)
    expect(run().disabled).toBe(true)
    expect(screen.getByLabelText('Scope').disabled).toBe(true)
    expect(screen.getByText('This step is running. Confirm or wait for it to finish.')).toBeTruthy()
    fireEvent.click(run())
    expect(props.onSubmit).not.toHaveBeenCalled()
    rerender(<SolarSizingForm {...props} status="failed" failureCode="STALE_GRAPH_REVISION" />)
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_SIZING_RUN_REASONS.stale)
    const retry = screen.getByRole('button', { name: 'Retry' })
    fireEvent.click(retry); fireEvent.click(retry)
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, {
      expected_rev: 7, mode: 'manual-global', confirm: true,
    })
    rerender(<SolarSizingForm {...props} status="finished" />)
    await waitFor(() => expect(screen.getByText('Saved global string length: 12')).toBeTruthy())
    expect(readIntake).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenLastCalledWith(props.row, {
      expected_rev: 8, mode: 'manual-global', confirm: true,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('G1A-7 terminal job record preserves the nested cloud reason into the alert', async () => {
    const envelope = recordToEnvelope({ status: 'failed', tool: 'solar-size-strings',
      error: { reason_code: 'CLOUD_AUTH_MISSING', error_code: 'FORBIDDEN', message: '<script>hostile</script>', retryable: false } })
    const outcome = solarFlowRunOutcome(envelope)
    expect(outcome).toEqual({ ok: false, code: 'CLOUD_AUTH_MISSING' })
    await mount(G7(), { status: 'failed', failureCode: outcome.code })
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_SIZING_RUN_REASONS.grant_missing)
  })

  it('G1A-9 no-ok settings refusal reaches the sizing alert through runToolAsync', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({
      reason_code: 'valid_settings_required',
      error: { error_code: 'BAD_PARAMS', retryable: false, message: '<script>hostile</script>' },
    }) }))
    const envelope = await runToolAsync({ name: 'solar-size-strings' }, {}, 'd1', {})
    await mount(G7(), { status: 'failed', failureCode: solarFlowRunOutcome(envelope).code })
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_SIZING_RUN_REASONS.settings)
  })

  it.each([
    ['CLOUD_AUTH_MISSING', 'grant_missing'],
    [null, 'access'],
  ])('G1A-8 async 202 and failed GET render %s as %s', async (reason_code, klass) => {
    const response = (body, status) => ({ ok: status >= 200 && status < 300, status, json: async () => body })
    const fetch = vi.fn().mockResolvedValueOnce(response({ job_id: 'sizing-job' }, 202))
      .mockResolvedValueOnce(response({ job_id: 'sizing-job', tool: 'solar-size-strings', status: 'failed',
        error: { ...(reason_code ? { reason_code } : {}), error_code: 'FORBIDDEN', retryable: false,
          message: '<script>hostile</script>' } }, 200))
    vi.stubGlobal('fetch', fetch)
    vi.stubGlobal('EventSource', undefined)
    const envelope = await runToolAsync({ name: 'solar-size-strings' }, {}, 'd1', {})
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls[0][1].method).toBe('POST')
    expect(new URL(fetch.mock.calls[1][0], 'https://example.test').pathname).toBe('/api/jobs/sizing-job')
    await mount(G7(), { status: 'failed', failureCode: solarFlowRunOutcome(envelope).code })
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_SIZING_RUN_REASONS[klass])
  })

  it.each([
    ["grant_missing","CLOUD_AUTH_MISSING","FORBIDDEN"],
    ["grant_scope","CLOUD_TENANT_UNAUTHORIZED","FORBIDDEN"],
    ["request_invalid","CLOUD_REQUEST_INVALID","BAD_PARAMS"],
    ["coverage_invalid","INVALID_SIZING_COVERAGE","BAD_PARAMS"],
    ["models_mismatch","SIZING_MODEL_MISMATCH","BAD_PARAMS"],
    ["project_mismatch","SIZING_PROJECT_MISMATCH","BAD_PARAMS"],
    ["cloud_unavailable","CLOUD_UPSTREAM_FAILURE","WORKITEM_FAILED"],
    ["cloud_response_invalid","CLOUD_RESPONSE_INVALID","WORKITEM_FAILED"],
    ["cold_voltage","COLD_VOLTAGE_FAILED","BAD_PARAMS"],
    ["stale","STALE_GRAPH_REVISION","BAD_PARAMS"],
    ["settings","valid_settings_required","BAD_PARAMS"],
    ["units","UNRESOLVED_UNITS","BAD_PARAMS"],
    ["panels","MISSING_PANEL","BAD_PARAMS"],
    ["confirmation","SIZING_CONFIRMATION_REQUIRED","BAD_PARAMS"],
    ["graph_unavailable","drawing_context_required","BAD_PARAMS"],
    ["graph_invalid","GRAPH_DIGEST_MISMATCH","BAD_PARAMS"],
    ["checkout","CHECKOUT_REQUIRED","FORBIDDEN"],
    ["storage","GRAPH_STORE_UNAVAILABLE","INTERNAL"],
    ["commit_unconfirmed","GRAPH_COMMIT_READBACK_FAILED","INTERNAL"],
    ["commit_refused","INVALID_JOB_BINDING","BAD_PARAMS"],
    ["cancelled","GRAPH_COMMIT_CANCELLED","BAD_PARAMS"],
    ["broker_unavailable","BROKER_UNREACHABLE","BROKER_UNREACHABLE"],
    ["timeout","TIMEOUT","TIMEOUT"],
    ["access","FORBIDDEN","FORBIDDEN"],
    ["quota","quota_exceeded","quota_exceeded"],
    ["unknown","INTERNAL","INTERNAL"],
  ])('G1A-10 %s renders fixed alert copy for %s', async (klass, reason_code, error_code) => {
    for (const envelope of [
      { ok: false, reason_code, error: { error_code, message: '<script>hostile</script>' } },
      { ok: false, error: { reason_code, error_code, message: '<script>hostile</script>' } },
    ]) {
      await mount(G7(), { status: 'failed', failureCode: solarFlowRunOutcome(envelope).code })
      const alert = screen.getByRole('alert')
      expect(alert.textContent).toBe(SOLAR_SIZING_RUN_REASONS[klass])
      expect(alert.textContent).not.toContain(reason_code)
      expect(alert.textContent).not.toContain(error_code)
      expect(alert.textContent).not.toContain('<script>hostile</script>')
      expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
      cleanup()
    }
  })

  it('G1A-11 Retry retains the request once and is independent of copy selection', async () => {
    const { props, rerender } = await mount(); await fill()
    for (const failureCode of ['CLOUD_AUTH_MISSING', 'FORBIDDEN', 'UNKNOWN_REFUSAL', '<script>hostile</script>', null]) {
      rerender(<SolarSizingForm {...props} status="failed" failureCode={failureCode} />)
      expect(screen.getByRole('button', { name: 'Retry' }).disabled).toBe(false)
      expect(screen.getByLabelText('Module').value).toBe(R.module_name)
      expect(screen.getByLabelText('Grant reference').value).toBe('grant_1')
    }
    const retry = screen.getByRole('button', { name: 'Retry' })
    fireEvent.click(retry); fireEvent.click(retry)
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, {
      expected_rev: 7, mode: 'global', requests: { [S]: R }, grant_ref: 'grant_1', confirm: true,
    })
    rerender(<SolarSizingForm {...props} status="pending" />)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    rerender(<SolarSizingForm {...props} status="finished" failureCode="CLOUD_AUTH_MISSING" />)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('G1A-10 unknown top-level refusal keeps generic copy over a known nested reason', async () => {
    const envelope = { ok: false, reason_code: 'UNLISTED_REASON',
      error: { reason_code: 'CLOUD_AUTH_MISSING', error_code: 'FORBIDDEN', message: '<script>hostile</script>' } }
    await mount(G7(), { status: 'failed', failureCode: solarFlowRunOutcome(envelope).code })
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_SIZING_RUN_REASONS.unknown)
  })

  it('SZ1 global submits the saved revision and ZIP with the typed request', async () => {
    const { props } = await mount()
    expect(screen.getByText('Saved project ZIP: 44224-1234')).toBeTruthy()
    await fill()
    fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, {
      expected_rev: 7, mode: 'global', requests: { [S]: R }, grant_ref: 'grant_1', confirm: true,
    })
  })

  it('SZ2 zones bind each saved model and show both targets', async () => {
    const { props } = await mount()
    change('Scope', 'zones')
    await fill(['Module', 'Inverter'])
    expect(screen.getByText(ZA)).toBeTruthy()
    expect(screen.getByText(ZB)).toBeTruthy()
    fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, {
      expected_rev: 7, mode: 'zones', requests: {
        [ZA]: { ...R, module_name: 'Module A', full_inverter_name: 'Inverter A' },
        [ZB]: { ...R, module_name: 'Module B', full_inverter_name: 'Inverter B' },
      }, grant_ref: 'grant_1', confirm: true,
    })
  })

  it('SZ3 blank saved ZIP blocks submission', async () => {
    const graph = G7(); graph.project.zip_code = '  '
    const { props } = await mount(graph)
    await fill()
    expect(screen.getByText('Save a project ZIP code in Solar settings before sizing strings.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('SZ4 overlap and incomplete zones block only zone sizing', async () => {
    for (const refs of [[P1, P2], []]) {
      const graph = G7(); graph.electrical_zones[1].panel_refs = refs
      const { props } = await mount(graph)
      await fill(); change('Scope', 'zones')
      expect(screen.getByText('Zones must cover every panel exactly once.')).toBeTruthy()
      expect(run().disabled).toBe(true)
      fireEvent.click(run())
      expect(props.onSubmit).not.toHaveBeenCalled()
      change('Scope', 'global')
      expect(run().disabled).toBe(false)
      cleanup()
    }
  })

  it('SZ5 no panels blocks sizing', async () => {
    const graph = G7(); graph.panels = []
    await mount(graph); await fill()
    expect(screen.getByText('Place panels before sizing strings.')).toBeTruthy()
    expect(run().disabled).toBe(true)
  })

  it('SZ6 a missing zone model blocks zone sizing', async () => {
    const graph = G7(); graph.electrical_zones[1].inverter_model_a = ''
    await mount(graph); await fill(); change('Scope', 'zones')
    expect(screen.getByText('Every zone needs a module and inverter model before sizing by zone.')).toBeTruthy()
    expect(run().disabled).toBe(true)
  })

  it('SZ7 confirmed module watts are read only', async () => {
    const graph = G7()
    graph.settings.extra.string_sizing = { records: { [S]: { adapter_version: '2.0.0', response: MIN } } }
    await mount(graph)
    expect(screen.getByText('Confirmed module power: 595.5 W')).toBeTruthy()
    expect(screen.queryByLabelText(/power|watts/i)).toBeNull()
  })

  it('SZ8 unconfirmed or invalid power is unavailable', async () => {
    for (const record of [undefined, { adapter_version: '1.0.0', response: { ...MIN, pmp: 595 } },
      ...[0, true, 1000001].map((pmp) => ({ adapter_version: '2.0.0', response: { ...MIN, pmp } }))]) {
      const graph = G7(); graph.settings.extra.string_sizing = { records: { [S]: record } }
      await mount(graph)
      expect(screen.getByText('Confirmed module power is unavailable. Re-size strings.')).toBeTruthy()
      cleanup()
    }
  })

  it('SZ24 an overlong or invalid zone model blocks zone sizing with its own reason', async () => {
    for (const key of ['module_model', 'inverter_model_a']) {
      for (const value of ['A'.repeat(257), '\ud800']) {
        const graph = G7(); graph.electrical_zones[0][key] = value
        const { props } = await mount(graph)
        change('Scope', 'zones'); await fill(['Module', 'Inverter'])
        expect(screen.getByText('Zone module and inverter model names must be at most 256 characters of valid text.')).toBeTruthy()
        expect(run().disabled).toBe(true)
        fireEvent.click(run())
        expect(props.onSubmit).not.toHaveBeenCalled()
        cleanup()
      }
      const graph = G7(); graph.electrical_zones[0][key] = 'A'.repeat(256)
      const { props } = await mount(graph)
      change('Scope', 'zones'); await fill(['Module', 'Inverter'])
      expect(run().disabled).toBe(false)
      fireEvent.click(run())
      expect(props.onSubmit).toHaveBeenCalledTimes(1)
      const requestKey = key === 'module_model' ? 'module_name' : 'full_inverter_name'
      expect(props.onSubmit.mock.calls[0][1].requests[ZA][requestKey]).toBe('A'.repeat(256))
      cleanup()
    }
  })

  it('SZ25 an incomplete stored response shows power as unavailable', async () => {
    const withoutCells = { ...MIN }; delete withoutCells.cells
    for (const response of [{ pmp: 595.5 }, withoutCells, MIN]) {
      const graph = G7()
      graph.settings.extra.string_sizing = { records: { [S]: { adapter_version: '2.0.0', response } } }
      await mount(graph)
      expect(screen.getByText(response === MIN ? 'Confirmed module power: 595.5 W' :
        'Confirmed module power is unavailable. Re-size strings.')).toBeTruthy()
      cleanup()
    }
  })

  it('SZ9 invalid text and grants mark their fields', async () => {
    const { props } = await mount(); await fill()
    for (const [label, value] of [['Grant reference', 'g'.repeat(65)], ['Module', '🌞'.repeat(257)], ['Module', '   ']]) {
      change(label, value); invalid(label); fireEvent.click(run())
      change(label, fields[label])
    }
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('SZ10 bifacial requires an explicit boolean', async () => {
    await mount(); await fill(['Bifacial']); invalid('Bifacial')
  })

  it('SZ11 single axis includes tracker fields and fixed tilt omits them', async () => {
    const { props } = await mount(); await fill(); change('Racking', 'single_axis')
    change('Axis tilt', '0'); change('Axis azimuth', '180'); change('Max angle', '60')
    change('Ground coverage ratio', '.4'); change('Backtrack', 'true')
    fireEvent.click(run())
    expect(props.onSubmit.mock.calls[0][1].requests[S].racking_params).toEqual({ ...R.racking_params,
      racking_type: 'single_axis', axis_tilt: '0', axis_azimuth: '180', max_angle: '60', gcr: '.4', backtrack: true })
    change('Racking', 'fixed_tilt'); fireEvent.click(run())
    expect(props.onSubmit.mock.calls[1][1].requests[S].racking_params).toEqual(R.racking_params)
  })

  it('SZ12 missing tracker max angle is invalid', async () => {
    await mount(); await fill(); change('Racking', 'single_axis')
    change('Axis tilt', '0'); change('Axis azimuth', '180')
    change('Ground coverage ratio', '.4'); change('Backtrack', 'true')
    invalid('Max angle')
  })

  it('SZ13 module parameters require all ten numbers and a bounded integer count', async () => {
    const { props } = await mount(); await fill()
    fireEvent.click(screen.getByLabelText('Use module parameters'))
    const numbers = { V_oc_ref: 50, I_sc_ref: 10, V_mp_ref: 40, I_mp_ref: 9, alpha_sc: .01,
      beta_oc: -.1, STC: 360, gamma_r: -.3, T_NOCT: 45, N_s: 72 }
    for (const [key, value] of Object.entries(numbers)) change(key, String(value))
    fireEvent.click(run())
    expect(props.onSubmit.mock.calls[0][1].requests[S].module_parameters).toEqual(numbers)
    for (const value of ['72.5', '0']) { change('N_s', value); invalid('N_s') }
    fireEvent.click(screen.getByLabelText('Use module parameters')); fireEvent.click(run())
    expect(props.onSubmit.mock.calls[1][1].requests[S]).not.toHaveProperty('module_parameters')
  })

  it('SZ14 zones ignore blank draft models and hide the model inputs', async () => {
    const { props } = await mount(); change('Scope', 'zones'); await fill(['Module', 'Inverter'])
    expect(screen.queryByLabelText('Module')).toBeNull()
    expect(screen.queryByLabelText('Inverter')).toBeNull()
    fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('SZ15 two clicks before a render arm one intent', async () => {
    const { props } = await mount(); await fill()
    fireEvent.click(run()); fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('SZ16 pending failure retry and finish keep inputs and refresh the graph', async () => {
    const { props, rerender } = await mount(); await fill()
    rerender(<SolarSizingForm {...props} status="pending" />)
    expect(screen.getByText('This step is running. Confirm or wait for it to finish.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    rerender(<SolarSizingForm {...props} status="failed" failureCode="STALE_GRAPH_REVISION" />)
    expect(screen.getByText('String sizing needs the current drawing revision. Review the drawing before retrying. Your inputs are kept.')).toBeTruthy()
    expect(screen.getByLabelText('Module').value).toBe(R.module_name)
    const retry = screen.getByRole('button', { name: 'Retry' })
    fireEvent.click(retry); fireEvent.click(retry)
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    rerender(<SolarSizingForm {...props} status="finished" />)
    expect(screen.getByText('String sizing applied.')).toBeTruthy()
    await waitFor(() => expect(props.readIntake).toHaveBeenCalledTimes(2))
    expect(screen.getByLabelText('Module').value).toBe(R.module_name)
  })

  it('SZ17 project scope without a canonical binding refuses before either reader', async () => {
    const readContext = vi.fn()
    const onProjectSubmit = vi.fn()
    const { props } = await mount(G7(), { projectId: 'p1', projectClient: { readContext }, onProjectSubmit }, 'manual-global')
    expect(screen.getByText('Open a project drawing version before changing Solar sizing or conductors.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    expect(props.readIntake).not.toHaveBeenCalled()
    expect(readContext).not.toHaveBeenCalled()
    expect(onProjectSubmit).not.toHaveBeenCalled()
  })

  it('SZ18 mismatched and late views cannot supply the current graph', async () => {
    const mismatch = await mount(G7(), { readIntake: vi.fn(async () => view(G7(), 2)) })
    await fill(); expect(run().disabled).toBe(true); expect(mismatch.props.onSubmit).not.toHaveBeenCalled()
    cleanup()
    let resolveOld, resolveNew
    const readIntake = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve }))
    const { props, rerender } = await mount(G7(), { readIntake })
    await fill()
    rerender(<SolarSizingForm {...props} drawingVersion={4} />)
    await act(async () => {})
    await act(async () => { resolveOld(view(G7())) })
    expect(run().disabled).toBe(true)
    expect(screen.queryByText('Saved project ZIP: 44224-1234')).toBeNull()
    const graph = G7(); graph.rev = 8
    await act(async () => { resolveNew(view(graph, 4)) })
    expect(run().disabled).toBe(false)
    expect(screen.getByLabelText('Module').value).toBe(R.module_name)
    fireEvent.click(run())
    expect(props.onSubmit.mock.calls[0][1].expected_rev).toBe(8)
  })
})
