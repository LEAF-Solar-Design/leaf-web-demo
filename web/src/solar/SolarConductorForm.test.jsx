import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import declaration from '../../../server/solar_tools/solar_string_conductors.json'
import SolarConductorForm from './SolarConductorForm.jsx'
import { PROJECT_SIZING_CONDUCTOR_REASONS, projectSizingConductorReason } from './solarSizingModel.js'
import { createSolarProjectClient } from './solarProjectClient.js'

afterEach(() => { cleanup(); vi.useRealTimers() })
const row = declaration.record
const fixture = () => ({ version: 3, intake: { solar_design_graph: { rev: 7, strings: [
  { id: 'T1', circuit_tag: 'A1', wire_gauge: '' }, { id: 'T2', circuit_tag: 'A2', wire_gauge: '8 AWG' },
] } } })
const four = () => ({ version: 3, intake: { solar_design_graph: { rev: 7, strings: ['1', '2', '3', '4'].map((n) => (
  { id: `T${n}`, circuit_tag: `A${n}`, wire_gauge: '' })) } } })
const expected = { operation: 'set-conductors', expected_rev: 7, assignments: [
  { string_ref: 'T1', wire_gauge: '10 AWG' }, { string_ref: 'T2', wire_gauge: '10 AWG' },
] }
const apply = () => screen.getByRole('button', { name: 'Apply to selected strings' })
const choose = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
  fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
}
const stringRow = (tag) => screen.getByLabelText(`Select ${tag}`)
const stringRows = () => [...document.querySelectorAll('tr[data-string-id]')]
const isSelected = (item) => item.querySelector('input[type="checkbox"]').checked
const selectedIds = () => stringRows().filter(isSelected).map((item) => item.dataset.stringId)
// jsdom may lack PointerEvent; React reads pointerType off whatever native event arrives.
function pointer(node, type, { pointerType = 'touch', ...init } = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 0, clientY: 0, ...init })
  Object.defineProperty(event, 'pointerType', { value: pointerType })
  fireEvent(node, event)
}
function tap(node) {
  pointer(node, 'pointerdown'); pointer(node, 'pointerup'); fireEvent.click(node)
}
async function setup(overrides = {}) {
  const props = { row, drawingId: 'd1', drawingVersion: 3, projectId: null,
    readIntake: vi.fn(async () => fixture()), onSubmit: vi.fn(), onClose: vi.fn(), ...overrides }
  const view = render(<SolarConductorForm {...props} />)
  if (props.projectId === null && props.drawingId) await waitFor(() => expect(props.readIntake).toHaveBeenCalled())
  await act(async () => {})
  return { ...view, props }
}


const R9Form = SolarConductorForm
const r9Tool = 'solar-string-conductors'
const r9Row = () => ({ ...row, catalog_digest: r9Digest })
const r9Graph = () => fixture().intake.solar_design_graph
const r9Button = apply

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
const r9Sentences = {
  "binding_invalid": "Open a project drawing version before changing Solar sizing or conductors.",
  "context_unreadable": "This project drawing version could not be read. Reload it and try again.",
  "not_current_head": "Select the current project drawing version before making this change.",
  "version_digest_unavailable": "This project drawing version has no usable intake digest. Reload it and try again.",
  "graph_unreadable": "The Solar design in this project drawing could not be read.",
  "graphless": "Start a Solar design in Solar settings before sizing strings or choosing conductors.",
  "scope_stale": "The drawing or session changed. Review the current inputs before submitting.",
  "catalog_digest_required": "Refresh the Solar tools before preparing this change.",
  "idempotency_key_required": "This request could not be prepared. Try again.",
  "preview_required": "Checking whether this change can be applied to this project drawing.",
  "preview_refused": "This change cannot be applied. Review the refusal and try again.",
  "submit_unavailable": "Project Solar submission is not available in this view.",
  "request_invalid": "This request could not be prepared. Review the inputs and try again.",
  "manual_only": "Project string sizing supports the saved global length only.",
  "project_adapter_unavailable": "Conductor changes are not available for project drawings on this server.",
  "checkout_required": "The project checkout must be available before this new change can run.",
  "access_denied": "Your current session cannot make this project drawing change.",
  "service_unavailable": "Project Solar is unavailable. Try again when the service is available.",
  "request_timeout": "The request timed out. Its submission status is uncertain; retry the same request.",
  "request_cancelled": "The request stopped locally. Check its status before retrying."
}

describe('SolarConductorForm', () => {
  it('R9-47 a conflicting project id refuses before context preview or handoff', async () => {
    const h = r9Harness()
    render(<SolarConductorForm {...h.props} projectId="99999999-9999-9999-9999-999999999999" />)
    await r9Settle()
    expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid)).toBeTruthy()
    expect(apply().disabled).toBe(true)
    fireEvent.click(apply())
    expect(h.client.readContext).not.toHaveBeenCalled()
    expect(h.client.catalog).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
  })

  it('R9-50 a bare successful conductor 202 shows invalid feedback and offers retry', async () => {
    const h = r9Harness()
    h.props.onProjectSubmit.mockResolvedValue({ ok: true, status: 202 })
    render(<SolarConductorForm {...h.props} />)
    await waitFor(() => expect(stringRow('A1')).toBeTruthy())
    choose()
    await r9Ready()
    fireEvent.click(apply())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    expect(screen.getByTestId('project-solar-conductor-feedback').textContent)
      .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.request_invalid)
    expect(screen.queryByText('Request submitted.')).toBeNull()
    expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1)
  })

  it('R9-55 the step editor project marker true admits the canonical binding through the handoff', async () => {
    const h = r9Harness()
    render(<SolarConductorForm {...h.props} projectId />)
    await waitFor(() => expect(stringRow('A1')).toBeTruthy())
    choose()
    await r9Ready()
    fireEvent.click(apply())
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(1))
    expect(h.props.readIntake).not.toHaveBeenCalled()
    for (const projectId of [false, 1, {}, '']) {
      cleanup()
      const refused = r9Harness()
      render(<SolarConductorForm {...refused.props} projectId={projectId} />)
      await r9Settle()
      expect(screen.getByText(PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid)).toBeTruthy()
      expect(refused.client.readContext).not.toHaveBeenCalled()
      expect(refused.client.catalog).not.toHaveBeenCalled()
      expect(refused.props.onProjectSubmit).not.toHaveBeenCalled()
    }
  })


  it('R9-19 ordinary unchecked conductor catalogs open review but cannot arm a run', async () => {
    const h = r9Harness()
    h.preview = r9Preview(r9Tool, r9Digest, { input_ready: false, runnable: false,
      admission_checked: false, admission: null, refusal_reasons: ['project_request_required'] })
    render(<SolarConductorForm {...h.props} />)
    await waitFor(() => expect(stringRows()).toHaveLength(2))
    expect(selectedIds()).toEqual([])
    expect(screen.getByLabelText('Conductor').value).toBe('')
    choose()
    await waitFor(() => expect(screen.getByTestId('project-solar-conductor-feedback')).toBeTruthy())
    expect(apply().disabled).toBe(true)
    fireEvent.click(apply())
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
  })

  it('R9-20 synthetic exact conductor admission arms the canonical intent and handoff', async () => {
    const h = r9Harness()
    h.props.onProjectSubmit = vi.fn((intent, options) => h.real.submitRun(intent, { signal: options.signal }))
    render(<SolarConductorForm {...h.props} />)
    await waitFor(() => expect(stringRows()).toHaveLength(2))
    choose()
    await r9Ready()
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    expect(intent.params).toEqual({ drawing_id: r9Binding.drawing_id, ...expected })
    fireEvent.click(apply())
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    const wire = h.fetchImpl.mock.calls.find(([url]) => url.endsWith('/api/run'))[1]
    expect(JSON.parse(wire.body)).toEqual({ tool: r9Tool, params: intent.params,
      dwg: r9Binding.input_version_id, catalog_digest: r9Digest })
    expect(selectedIds()).toEqual(['T1', 'T2'])
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
  })

  it('R9-21 nested builtin conductor refusals preserve their safe codes and never hand off', async () => {
    for (const code of ['STALE_GRAPH_REVISION', 'DUPLICATE_STRING_ASSIGNMENT', 'MISSING_STRING']) {
      const h = r9Harness()
      h.preview = r9Preview(r9Tool, r9Digest, { runnable: false, admission: { status_code: 409,
        error: { reason_code: code } }, refusal_reasons: [code] })
      render(<SolarConductorForm {...h.props} />)
      await waitFor(() => expect(stringRows()).toHaveLength(2))
      choose()
      await waitFor(() => expect(screen.getByTestId('project-solar-conductor-feedback').textContent)
        .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.preview_refused + ' (' + code + ')'))
      expect(apply().disabled).toBe(true)
      fireEvent.click(apply())
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R9-22 schema admission refusals show INVALID_SETTINGS_REQUEST without promising builtin codes', async () => {
    for (const invalid of [
      { assignments: [{ string_ref: 'T1', wire_gauge: 'invalid' }] },
      { assignments: [] }, { operation: 'wrong' },
      { assignments: [{ string_ref: 'T1', wire_gauge: '10 AWG', extra: true }] },
    ]) {
      const h = r9Harness()
      h.preview = r9Preview(r9Tool, r9Digest, { runnable: false, admission: { status_code: 422,
        error: { reason_code: 'INVALID_SETTINGS_REQUEST', details: invalid } }, refusal_reasons: ['INVALID_SETTINGS_REQUEST'] })
      render(<SolarConductorForm {...h.props} />)
      await waitFor(() => expect(stringRows()).toHaveLength(2))
      choose()
      await waitFor(() => expect(screen.getByTestId('project-solar-conductor-feedback').textContent)
        .toBe(PROJECT_SIZING_CONDUCTOR_REASONS.preview_refused + ' (INVALID_SETTINGS_REQUEST)'))
      expect(apply().disabled).toBe(true)
      expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R9-39 StrictMode unmount and scope invalidation cancel owned work and delayed touch selection', async () => {
    const h = r9Harness()
    const view = render(<React.StrictMode><SolarConductorForm {...h.props} /></React.StrictMode>)
    await waitFor(() => expect(stringRows()).toHaveLength(2))
    expect(h.unsubscribes[0]).toHaveBeenCalledTimes(1)
    vi.useFakeTimers()
    pointer(stringRow('A1'), 'pointerdown')
    const old = stringRow('A1')
    h.props = { ...h.props, solarBinding: r9Next }
    h.context = r9Context(r9Graph(), r9Next)
    act(() => h.real.setScope(r9Next, 'epoch-1'))
    view.rerender(<React.StrictMode><SolarConductorForm {...h.props} /></React.StrictMode>)
    await r9Settle()
    act(() => vi.advanceTimersByTime(1000))
    expect(selectedIds()).toEqual([])
    fireEvent.click(old)
    expect(selectedIds()).toEqual([])
    vi.useRealTimers()
    await waitFor(() => expect(stringRows()).toHaveLength(2))
    const late = r9Deferred(); h.previewQueue.push(late.promise)
    choose()
    await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
    const signal = h.client.catalog.mock.calls[0][0].signal
    view.unmount()
    expect(signal.aborted).toBe(true)
    expect(h.unsubscribes.every((unsubscribe) => unsubscribe.mock.calls.length === 1)).toBe(true)
    await act(async () => late.resolve(r9Preview(r9Tool)))
    expect(h.client.dispose).not.toHaveBeenCalled()
    expect(h.client.setScope).not.toHaveBeenCalled()
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
  })

  it('R9-45 fixed sentence maps and project mouse range keyboard touch and focus semantics survive', async () => {
    expect(Object.isFrozen(PROJECT_SIZING_CONDUCTOR_REASONS)).toBe(true)
    expect(PROJECT_SIZING_CONDUCTOR_REASONS).toEqual(r9Sentences)
    for (const [key, sentence] of Object.entries(r9Sentences)) expect(projectSizingConductorReason(key)).toBe(sentence)
    expect(projectSizingConductorReason('unknown')).toBeNull()
    const h = r9Harness()
    h.context = r9Context(four().intake.solar_design_graph)
    const view = render(<SolarConductorForm {...h.props} />)
    await waitFor(() => expect(stringRows()).toHaveLength(4))
    expect(screen.queryAllByRole('checkbox')).toHaveLength(4)
    expect(stringRows().every((item) => item.tabIndex === 0)).toBe(true)
    fireEvent.click(stringRow('A1'))
    fireEvent.click(stringRow('A3'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3'])
    fireEvent.click(stringRow('A2'), { ctrlKey: true })
    expect(selectedIds()).toEqual(['T1', 'T3'])
    fireEvent.click(stringRow('A4'), { metaKey: true })
    expect(selectedIds()).toEqual(['T1', 'T3', 'T4'])
    stringRow('A1').focus()
    fireEvent.keyDown(stringRow('A1'), { key: 'ArrowDown' })
    expect(stringRow('A2')).toHaveFocus()
    fireEvent.keyDown(stringRow('A2'), { key: 'x' })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3', 'T4'])
    fireEvent.keyDown(stringRow('A3'), { key: 'Enter' })
    expect(selectedIds()).toEqual(['T3'])
    fireEvent.keyDown(stringRow('A4'), { key: ' ', shiftKey: true })
    expect(selectedIds()).toEqual(['T3', 'T4'])
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }))
    vi.useFakeTimers()
    pointer(stringRow('A1'), 'pointerdown')
    act(() => vi.advanceTimersByTime(500))
    pointer(stringRow('A1'), 'pointerup'); fireEvent.click(stringRow('A1'))
    expect(selectedIds()).toEqual(['T1'])
    tap(stringRow('A2'))
    expect(selectedIds()).toEqual(['T1', 'T2'])
    vi.useRealTimers()
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    await r9Ready()
    const oldIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
    const previewCalls = h.client.catalog.mock.calls.length
    fireEvent.click(stringRow('A2'), { ctrlKey: true })
    fireEvent.click(stringRow('A2'), { ctrlKey: true })
    await r9Ready()
    const roundTripIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
    expect(roundTripIntent.idempotencyKey).not.toBe(oldIntent.idempotencyKey)
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    await r9Settle()
    expect(h.client.catalog.mock.calls.length).toBeGreaterThan(previewCalls)
    expect(h.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(roundTripIntent)
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '8 AWG' } })
    expect(apply().disabled).toBe(true)
    await r9Ready()
    const inputIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
    expect(inputIntent.idempotencyKey).not.toBe(oldIntent.idempotencyKey)
    const nextRow = JSON.parse(JSON.stringify(h.props.row))
    nextRow.params.properties.assignments.items.properties.wire_gauge.enum.reverse()
    h.props = { ...h.props, row: nextRow }
    view.rerender(<SolarConductorForm {...h.props} />)
    expect(apply().disabled).toBe(true)
    await r9Ready()
    expect(h.client.catalog.mock.calls.at(-1)[0].intents[0].idempotencyKey).not.toBe(inputIntent.idempotencyKey)
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
  })
  it('CF1 displays stored choices with no implicit selection or conductor', async () => {
    await setup()
    const table = screen.getByRole('table', { name: 'String conductors' })
    expect(within(table).getAllByRole('row').map((item) => item.textContent)).toEqual([
      'Circuit tagCurrent conductor', 'A1Not set', 'A28 AWG',
    ])
    expect(stringRows()).toHaveLength(2)
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('')
    expect(screen.getByRole('option', { name: 'Choose a conductor' }).selected).toBe(true)
    expect(apply().disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toBe('Select at least one string.')
    expect(screen.getByText('This records conductor choices. It does not check ampacity or voltage drop.')).toBeTruthy()
  })

  it('CF2 applies the selected conductor to both strings once', async () => {
    const { props } = await setup()
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    expect(props.onSubmit).toHaveBeenCalledWith(row, expected)
  })

  it('CF3 reverse Ctrl or Cmd clicks still submit assignments in graph order', async () => {
    const { props } = await setup()
    fireEvent.click(stringRow('A2'), { ctrlKey: true })
    fireEvent.click(stringRow('A1'), { metaKey: true })
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledWith(row, expected)
  })

  it('CF4 requires an explicit conductor after selecting a string', async () => {
    const { props } = await setup()
    fireEvent.click(stringRow('A1'))
    expect(screen.getByRole('status').textContent).toBe('Choose a conductor for the selected strings.')
    expect(apply().disabled).toBe(true)
    fireEvent.click(apply())
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('CF5 locks synchronously before a parent render and releases on dismissal', async () => {
    const { props, rerender } = await setup()
    choose()
    const button = apply()
    act(() => { button.click(); button.click() })
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    expect(apply().disabled).toBe(false)
    expect(screen.queryByText('Review and confirm this change.')).toBeNull()
    rerender(<SolarConductorForm {...props} status="pending" />)
    expect(apply().disabled).toBe(true)
    expect(screen.getByText('This step is running. Wait for it to finish.')).toBeTruthy()
    rerender(<SolarConductorForm {...props} status={null} />)
    expect(apply().disabled).toBe(false)
    expect(props.readIntake).toHaveBeenCalledTimes(1)
  })

  it('CF6 failure retains the inputs and Retry stages one equal request', async () => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    rerender(<SolarConductorForm {...props} status="pending" />)
    rerender(<SolarConductorForm {...props} status="failed" failureCode="STALE_GRAPH_REVISION" />)
    expect(stringRows().every(isSelected)).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('10 AWG')
    expect(screen.getByRole('alert').textContent).toBe('This run failed: STALE_GRAPH_REVISION. Your inputs are kept.')
    const retry = screen.getByRole('button', { name: 'Retry' })
    act(() => { retry.click(); retry.click() })
    expect(props.onSubmit).toHaveBeenCalledTimes(2)
    expect(props.onSubmit.mock.calls[1]).toEqual(props.onSubmit.mock.calls[0])
    expect(retry.disabled).toBe(false)
    expect(apply().disabled).toBe(false)
    rerender(<SolarConductorForm {...props} status="pending" />)
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(apply().disabled).toBe(true)
  })

  it('CF16 identical ribbon-host props release the submit guard without a status prop', async () => {
    const { props, rerender } = await setup()
    choose()
    const button = apply()
    act(() => { button.click(); button.click() })
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    rerender(<SolarConductorForm {...props} />)
    expect(apply().disabled).toBe(false)
    expect(stringRows().every((item) => isSelected(item) && !item.hasAttribute('aria-disabled'))).toBe(true)
    expect(screen.queryByText('Review and confirm this change.')).toBeNull()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(2)
    expect(props.onSubmit.mock.calls[1]).toEqual([row, expected])
    expect(props.onSubmit.mock.calls[1]).toEqual(props.onSubmit.mock.calls[0])
    expect(props.readIntake).toHaveBeenCalledTimes(1)
  })

  it('CF17 consecutive finished props release the guard after another submission', async () => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    rerender(<SolarConductorForm {...props} status="finished" />)
    await waitFor(() => expect(props.readIntake).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(2)
    rerender(<SolarConductorForm {...props} status="finished" />)
    expect(apply().disabled).toBe(false)
    expect(stringRows().every((item) => isSelected(item) && !item.hasAttribute('aria-disabled'))).toBe(true)
    expect(screen.queryByText('Review and confirm this change.')).toBeNull()
    expect(screen.getByText('Conductor choices applied.')).toBeTruthy()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(3)
    expect(props.onSubmit.mock.calls[2]).toEqual(props.onSubmit.mock.calls[1])
    expect(props.readIntake).toHaveBeenCalledTimes(2)
  })

  it.each(['STALE_GRAPH_REVISION', null])('CF18 failure after a version change asks for new inputs (code: %s)', async (failureCode) => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    rerender(<SolarConductorForm {...props} status="pending" />)
    let resolveUpdated
    props.readIntake.mockImplementationOnce(() => new Promise((resolve) => { resolveUpdated = resolve }))
    rerender(<SolarConductorForm {...props} drawingVersion={4} status="pending" />)
    await waitFor(() => expect(props.readIntake).toHaveBeenLastCalledWith('d1', 4))
    await act(async () => { resolveUpdated({ ...fixture(), version: 4 }) })
    expect(stringRows()).toHaveLength(2)
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('')
    rerender(<SolarConductorForm {...props} drawingVersion={4} status="failed" failureCode={failureCode} />)
    expect(screen.getByRole('alert').textContent).toBe(failureCode
      ? 'This run failed: STALE_GRAPH_REVISION. Choose the strings and the conductor again.'
      : 'This run failed. Choose the strings and the conductor again.')
    expect(screen.queryByText(/Your inputs are kept\./)).toBeNull()
    expect(apply().disabled).toBe(true)
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    fireEvent.click(stringRow('A1'))
    expect(apply().disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    expect(apply().disabled).toBe(false)
  })

  it('CF7 completion refreshes the persisted revision and resets the draft', async () => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    const updated = fixture()
    updated.intake.solar_design_graph.rev = 8
    updated.intake.solar_design_graph.strings.forEach((item) => { item.wire_gauge = '10 AWG' })
    props.readIntake.mockResolvedValue(updated)
    rerender(<SolarConductorForm {...props} status="finished" />)
    await waitFor(() => expect(within(screen.getByRole('table')).getAllByText('10 AWG')).toHaveLength(2))
    expect(props.readIntake).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Conductor choices applied.')).toBeTruthy()
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('')
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit.mock.calls[1][1]).toEqual({ ...expected, expected_rev: 8 })
  })

  it('CF8a incomplete project intent refuses with binding_invalid before any reader', async () => {
    const readContext = vi.fn()
    const onProjectSubmit = vi.fn()
    const { props } = await setup({ projectId: 'p1', projectClient: { readContext }, onProjectSubmit })
    expect(screen.getByText('Open a project drawing version before changing Solar sizing or conductors.')).toBeTruthy()
    expect(apply().disabled).toBe(true)
    expect(props.readIntake).not.toHaveBeenCalled()
    expect(readContext).not.toHaveBeenCalled()
    expect(props.onSubmit).not.toHaveBeenCalled()
    expect(onProjectSubmit).not.toHaveBeenCalled()
  })

  it('CF8b an absent standalone drawing keeps its sentence and disabled controls without reading intake', async () => {
    const { props } = await setup({ drawingId: null })
    expect(screen.getByText('This form supports standalone drawings only.')).toBeTruthy()
    expect(apply().disabled).toBe(true)
    expect(stringRows()).toHaveLength(0)
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.getByLabelText('Conductor').disabled).toBe(true)
    expect(props.readIntake).not.toHaveBeenCalled()
  })

  it('CF9 invalid graph envelopes produce no string rows', async () => {
    const mismatch = { ...fixture(), version: 4 }
    const missing = fixture()
    delete missing.intake.solar_design_graph.strings
    const duplicate = fixture()
    duplicate.intake.solar_design_graph.strings[1].id = 'T1'
    const oversized = fixture()
    oversized.intake.solar_design_graph.strings = Array.from({ length: 4097 }, (_, i) => ({ id: String(i), circuit_tag: '', wire_gauge: '' }))
    for (const value of [mismatch, missing, duplicate, oversized]) {
      await setup({ readIntake: vi.fn(async () => value) })
      expect(screen.getByText('Conductor choices are unavailable for this drawing.')).toBeTruthy()
      expect(stringRows()).toHaveLength(0)
      expect(apply().disabled).toBe(true)
      cleanup()
    }
  })

  it('CF10 missing or duplicate gauge enums never fall back to a built-in list', async () => {
    for (const options of [undefined, ['10 AWG', '10 AWG']]) {
      const invalid = { ...row, params: { properties: { assignments: { items: { properties: { wire_gauge: { enum: options } } } } } } }
      await setup({ row: invalid })
      expect(screen.getByText('Conductor choices are unavailable for this drawing.')).toBeTruthy()
      expect(screen.getAllByRole('option')).toHaveLength(1)
      expect(apply().disabled).toBe(true)
      cleanup()
    }
  })

  it('CF9 ignores late responses after drawing or version changes', async () => {
    for (const override of [{ drawingId: 'd2' }, { drawingVersion: 4 }]) {
      let resolveOld
      const readIntake = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
        .mockResolvedValue({ version: override.drawingVersion ?? 3, intake: { solar_design_graph: { rev: 8, strings: [] } } })
      const { props, rerender } = await setup({ readIntake })
      rerender(<SolarConductorForm {...props} {...override} />)
      await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(2))
      await act(async () => { resolveOld(fixture()) })
      expect(stringRows()).toHaveLength(0)
      expect(apply().disabled).toBe(true)
      cleanup()
    }
  })

  it('CF19 exposes selection through labeled checkboxes and retains row shortcuts', async () => {
    const { container } = await setup()
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(2)
    expect(screen.queryAllByRole('checkbox')).toHaveLength(2)
    expect(screen.queryByRole('columnheader', { name: 'Select' })).toBeNull()
    for (const item of stringRows()) {
      expect(item.tabIndex).toBe(0)
      expect(isSelected(item)).toBe(false)
    }
    expect(screen.getByRole('table', { name: 'String conductors' })).toHaveAccessibleDescription(/Shift-click selects a range/)
    fireEvent.click(stringRow('A2'))
    expect(screen.getByRole('checkbox', { name: 'Include A2' })).toBeChecked()
    expect(stringRow('A2')).toHaveTextContent('A2 (selected)8 AWG')
  })

  it('CF20 a plain click selects one; Ctrl or Cmd adds or removes; Shift spans from the anchor in graph order', async () => {
    const { props } = await setup({ readIntake: vi.fn(async () => four()) })
    fireEvent.click(stringRow('A1'))
    fireEvent.click(stringRow('A3'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3'])
    fireEvent.click(stringRow('A4'))
    expect(selectedIds()).toEqual(['T4'])
    fireEvent.click(stringRow('A2'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T2', 'T3', 'T4'])
    // The anchor stays put across Shift clicks.
    fireEvent.click(stringRow('A3'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T3', 'T4'])
    fireEvent.click(stringRow('A1'), { ctrlKey: true })
    expect(selectedIds()).toEqual(['T1', 'T3', 'T4'])
    fireEvent.click(stringRow('A3'), { metaKey: true })
    expect(selectedIds()).toEqual(['T1', 'T4'])
    // Ctrl or Cmd with Shift adds the span; the last toggled row is the anchor.
    fireEvent.click(stringRow('A1'), { shiftKey: true, ctrlKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3', 'T4'])
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    fireEvent.click(apply())
    expect(props.onSubmit.mock.calls[0][1].assignments.map((item) => item.string_ref)).toEqual(['T1', 'T2', 'T3', 'T4'])
  })

  it('CF21 X toggles the focused row, Enter selects it alone, arrows move focus, typing never toggles', async () => {
    await setup({ readIntake: vi.fn(async () => four()) })
    const first = stringRow('A1')
    first.focus()
    expect(fireEvent.keyDown(first, { key: 'x' })).toBe(false)
    expect(selectedIds()).toEqual(['T1'])
    fireEvent.keyDown(stringRow('A1'), { key: 'ArrowDown' })
    expect(stringRow('A2')).toHaveFocus()
    fireEvent.keyDown(stringRow('A2'), { key: 'X', shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2'])
    fireEvent.keyDown(stringRow('A1'), { key: 'x' })
    expect(selectedIds()).toEqual(['T2'])
    for (const chord of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }]) {
      expect(fireEvent.keyDown(stringRow('A3'), { key: 'x', ...chord })).toBe(true)
    }
    expect(selectedIds()).toEqual(['T2'])
    fireEvent.keyDown(stringRow('A2'), { key: 'ArrowUp' })
    expect(stringRow('A1')).toHaveFocus()
    // X made A1 the anchor even though it took A1 out.
    fireEvent.keyDown(stringRow('A4'), { key: 'Enter', shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3', 'T4'])
    fireEvent.keyDown(stringRow('A3'), { key: 'Enter' })
    expect(selectedIds()).toEqual(['T3'])
    fireEvent.keyDown(stringRow('A4'), { key: ' ' })
    expect(selectedIds()).toEqual(['T4'])
    const gauge = screen.getByLabelText('Conductor')
    gauge.focus()
    expect(fireEvent.keyDown(gauge, { key: 'x' })).toBe(true)
    expect(selectedIds()).toEqual(['T4'])
  })

  it('CF22 a 500 ms touch press enters selection, then taps toggle until the selection is empty', async () => {
    const { props, rerender } = await setup({ readIntake: vi.fn(async () => four()) })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    pointer(stringRow('A2'), 'pointerdown')
    act(() => { vi.advanceTimersByTime(499) })
    expect(selectedIds()).toEqual([])
    act(() => { vi.advanceTimersByTime(1) })
    expect(selectedIds()).toEqual(['T2'])
    // The release that ends the press is not a second tap.
    pointer(stringRow('A2'), 'pointerup'); fireEvent.click(stringRow('A2'))
    expect(selectedIds()).toEqual(['T2'])
    tap(stringRow('A4'))
    expect(selectedIds()).toEqual(['T2', 'T4'])
    tap(stringRow('A2'))
    tap(stringRow('A4'))
    expect(selectedIds()).toEqual([])
    // Empty again: a plain tap selects that row alone.
    tap(stringRow('A1'))
    tap(stringRow('A3'))
    expect(selectedIds()).toEqual(['T3'])
    // A mouse press never starts one, and a drift past the slop cancels one.
    pointer(stringRow('A1'), 'pointerdown', { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(600) })
    pointer(stringRow('A1'), 'pointerdown')
    pointer(stringRow('A1'), 'pointermove', { clientX: 0, clientY: 20 })
    act(() => { vi.advanceTimersByTime(600) })
    expect(selectedIds()).toEqual(['T3'])
    // A running step ignores presses.
    rerender(<SolarConductorForm {...props} status="pending" />)
    pointer(stringRow('A1'), 'pointerdown')
    act(() => { vi.advanceTimersByTime(600) })
    fireEvent.click(stringRow('A1'), { ctrlKey: true })
    expect(selectedIds()).toEqual(['T3'])
  })
})
