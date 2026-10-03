import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarSizingForm from './SolarSizingForm.jsx'
import { recordToEnvelope, runToolAsync } from '../api.js'
import { solarFlowRunOutcome } from './solarFlowModel.js'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'

const MIN = { cells: 81, voc: 52.58, isc: 13.9965, pmp: 595.5, vmp: 44.64, imp: 13.33,
  bpmp: -1.4875, bvoc: -0.13145, alpha_sc: .007, min_temp: -2.7,
  simulation_results: { standard: { Conditions: 'P99.5 Voc', max_module_voltage: 51.26,
    string_design_voltage: 1500, string_length: 28 } } }

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
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
const fill = (skip = []) => Object.entries(fields).forEach(([label, value]) => {
  if (!skip.includes(label)) change(label, value)
})
const view = (graph, version = 3) => ({ version, intake: { solar_design_graph: graph } })
async function mount(graph = G7(), overrides = {}) {
  const props = { row: { name: 'solar-size-strings' }, drawingId: 'd1', drawingVersion: 3, projectId: null,
    readIntake: vi.fn(async () => view(graph)), onSubmit: vi.fn(), ...overrides }
  const result = render(<SolarSizingForm {...props} />)
  await act(async () => {})
  return { ...result, props }
}
function invalid(label) {
  expect(screen.getByText('Check the marked sizing fields before submitting.')).toBeTruthy()
  expect(screen.getByLabelText(label).getAttribute('aria-invalid')).toBe('true')
  expect(run().disabled).toBe(true)
}

describe('SolarSizingForm', () => {
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
    const { props, rerender } = await mount(); fill()
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
    fill()
    fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, {
      expected_rev: 7, mode: 'global', requests: { [S]: R }, grant_ref: 'grant_1', confirm: true,
    })
  })

  it('SZ2 zones bind each saved model and show both targets', async () => {
    const { props } = await mount()
    change('Scope', 'zones')
    fill(['Module', 'Inverter'])
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
    fill()
    expect(screen.getByText('Save a project ZIP code in Solar settings before sizing strings.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    fireEvent.click(run())
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('SZ4 overlap and incomplete zones block only zone sizing', async () => {
    for (const refs of [[P1, P2], []]) {
      const graph = G7(); graph.electrical_zones[1].panel_refs = refs
      const { props } = await mount(graph)
      fill(); change('Scope', 'zones')
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
    await mount(graph); fill()
    expect(screen.getByText('Place panels before sizing strings.')).toBeTruthy()
    expect(run().disabled).toBe(true)
  })

  it('SZ6 a missing zone model blocks zone sizing', async () => {
    const graph = G7(); graph.electrical_zones[1].inverter_model_a = ''
    await mount(graph); fill(); change('Scope', 'zones')
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
        change('Scope', 'zones'); fill(['Module', 'Inverter'])
        expect(screen.getByText('Zone module and inverter model names must be at most 256 characters of valid text.')).toBeTruthy()
        expect(run().disabled).toBe(true)
        fireEvent.click(run())
        expect(props.onSubmit).not.toHaveBeenCalled()
        cleanup()
      }
      const graph = G7(); graph.electrical_zones[0][key] = 'A'.repeat(256)
      const { props } = await mount(graph)
      change('Scope', 'zones'); fill(['Module', 'Inverter'])
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
    const { props } = await mount(); fill()
    for (const [label, value] of [['Grant reference', 'g'.repeat(65)], ['Module', '🌞'.repeat(257)], ['Module', '   ']]) {
      change(label, value); invalid(label); fireEvent.click(run())
      change(label, fields[label])
    }
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('SZ10 bifacial requires an explicit boolean', async () => {
    await mount(); fill(['Bifacial']); invalid('Bifacial')
  })

  it('SZ11 single axis includes tracker fields and fixed tilt omits them', async () => {
    const { props } = await mount(); fill(); change('Racking', 'single_axis')
    change('Axis tilt', '0'); change('Axis azimuth', '180'); change('Max angle', '60')
    change('Ground coverage ratio', '.4'); change('Backtrack', 'true')
    fireEvent.click(run())
    expect(props.onSubmit.mock.calls[0][1].requests[S].racking_params).toEqual({ ...R.racking_params,
      racking_type: 'single_axis', axis_tilt: '0', axis_azimuth: '180', max_angle: '60', gcr: '.4', backtrack: true })
    change('Racking', 'fixed_tilt'); fireEvent.click(run())
    expect(props.onSubmit.mock.calls[1][1].requests[S].racking_params).toEqual(R.racking_params)
  })

  it('SZ12 missing tracker max angle is invalid', async () => {
    await mount(); fill(); change('Racking', 'single_axis')
    change('Axis tilt', '0'); change('Axis azimuth', '180')
    change('Ground coverage ratio', '.4'); change('Backtrack', 'true')
    invalid('Max angle')
  })

  it('SZ13 module parameters require all ten numbers and a bounded integer count', async () => {
    const { props } = await mount(); fill()
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
    const { props } = await mount(); change('Scope', 'zones'); fill(['Module', 'Inverter'])
    expect(screen.queryByLabelText('Module')).toBeNull()
    expect(screen.queryByLabelText('Inverter')).toBeNull()
    fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('SZ15 two clicks before a render arm one intent', async () => {
    const { props } = await mount(); fill()
    fireEvent.click(run()); fireEvent.click(run())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('SZ16 pending failure retry and finish keep inputs and refresh the graph', async () => {
    const { props, rerender } = await mount(); fill()
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

  it('SZ17 project scope never reads intake', async () => {
    const { props } = await mount(G7(), { projectId: 'p1' })
    expect(screen.getByText('This form supports standalone drawings only.')).toBeTruthy()
    expect(run().disabled).toBe(true)
    expect(props.readIntake).not.toHaveBeenCalled()
  })

  it('SZ18 mismatched and late views cannot supply the current graph', async () => {
    const mismatch = await mount(G7(), { readIntake: vi.fn(async () => view(G7(), 2)) })
    fill(); expect(run().disabled).toBe(true); expect(mismatch.props.onSubmit).not.toHaveBeenCalled()
    cleanup()
    let resolveOld, resolveNew
    const readIntake = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve }))
    const { props, rerender } = await mount(G7(), { readIntake })
    fill()
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
