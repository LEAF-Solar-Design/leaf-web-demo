import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectSolarGraphForm from './ProjectSolarGraphForm.jsx'
import { SEED_SETTINGS } from './solarSettingsModel.js'

afterEach(cleanup)

const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const nextBinding = { ...binding, input_version_id: '66666666-6666-6666-6666-666666666666' }
const job = '55555555-5555-5555-5555-555555555555'
const output = '77777777-7777-7777-7777-777777777777'
const digest = 'sha256:' + 'd'.repeat(64)
const published = 'The Solar run published a drawing version.'
const unknown = 'The Solar run outcome is unknown. Check its job before submitting again.'
const generic = 'The server refused this Solar request. Review the reported code.'
function context(value = binding, rev = 6) {
  return { context: { organization_id: value.organization_id, project_id: value.project_id,
    drawing_id: value.drawing_id, input_version_id: value.input_version_id,
    head_version_id: value.input_version_id, is_head: true, intake_sha256: 'a'.repeat(64) },
  intake: { solar_design_graph: { rev, settings: { ...SEED_SETTINGS },
    project: { name: 'Field', zip_code: '', latitude: null, longitude: null } }, solar_design_graph_sha256: 'b'.repeat(64) } }
}
function catalog(name, code = null) {
  return { ok: true, status: 200, data: { families: [{ capabilities: [{ name, catalog_digest: digest,
    availability: { admission_checked: true, admission: code === null ? { status_code: 200, error: null }
      : { status_code: 409, error: { reason_code: code, message: 'private path /srv/secret', detail: 'hostile detail' } },
    entitled: true, engine_ready: true, implemented: true, input_ready: code === null, runnable: code === null,
    refusal_reasons: code === null ? [] : [code] } }] }] } }
}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const accepted = () => ({ ok: true, status: 202, data: { job_id: job, status: 'submitted' } })
function harness(name = 'solar-homeruns', code = null) {
  const h = { name, code, current: true, epoch: 0, lease: 0, snapshot: { binding, authorityKey: 'authority-a' },
    reads: [], previews: [], submissions: [], rev: 6, listeners: new Set() }
  h.client = {
    getSnapshot: vi.fn(() => h.snapshot),
    subscribe: vi.fn((listener) => { h.listeners.add(listener); return () => h.listeners.delete(listener) }),
    captureCatalogScope: vi.fn(() => {
      const epoch = h.epoch, lease = h.lease
      return () => h.epoch === epoch && h.lease === lease
    }),
    readContext: vi.fn(() => h.reads.length ? h.reads.shift() : Promise.resolve({ ok: true, status: 200,
      data: context(h.snapshot.binding, h.rev) })),
    prepareRun: vi.fn((request) => Object.freeze({ binding: h.snapshot.binding, authorityKey: h.snapshot.authorityKey,
      tool: request.tool, params: request.params, catalogDigest: request.catalogDigest, idempotencyKey: request.idempotencyKey })),
    catalog: vi.fn(() => h.previews.length ? h.previews.shift() : Promise.resolve(catalog(h.name, h.code))),
  }
  h.submit = vi.fn(() => h.submissions.length ? h.submissions.shift() : Promise.resolve(accepted()))
  h.key = vi.fn(() => 'intent-' + h.key.mock.calls.length)
  h.guard = vi.fn(() => h.current)
  h.parent = new AbortController()
  h.close = vi.fn()
  h.props = { tool: { name, label: 'private tool label' }, solarBinding: binding, authorityKey: 'authority-a',
    projectClient: h.client, catalogDigest: digest, newProjectIntentKey: h.key, onProjectSubmit: h.submit,
    isCurrent: h.guard, submissionSignal: h.parent.signal, onClose: h.close }
  h.emit = () => { for (const listener of [...h.listeners]) listener() }
  h.rotate = () => { h.lease += 1; h.emit() }
  h.move = (value = binding, authorityKey = 'authority-a') => {
    h.epoch += 1; h.snapshot = { binding: value, authorityKey }; h.emit()
  }
  return h
}
const action = () => screen.getByRole('button', { name: 'Review & run' })
const refresh = () => screen.getByRole('button', { name: 'Refresh preview' })
const location = () => screen.getByRole('textbox', { name: 'Insertion point (x, y, optional z)' })
const feedback = () => screen.getByTestId('project-solar-graph-feedback').textContent
const arm = async () => waitFor(() => expect(action().disabled).toBe(false))
async function submitAndWait(h) {
  await arm()
  fireEvent.click(action())
  await waitFor(() => expect(h.submit).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.queryByTestId('project-solar-graph-feedback')).not.toBe(null))
}
function receipt(h, state, changes = {}, metaChanges = {}) {
  const options = h.submit.mock.calls[0][1]
  return { outcome: { state, jobId: job, outputVersionId: state === 'succeeded' ? output : null,
    reasonCode: null, message: 'private completion message /srv/secret', ...changes },
  meta: { ...options, tool: h.name, ...metaChanges } }
}
async function assertRefusal(name, code, sentence) {
  const h = harness(name, code)
  const mounted = render(<ProjectSolarGraphForm {...h.props} />)
  if (name === 'solar-schedule') fireEvent.change(location(), { target: { value: '1,2' } })
  await waitFor(() => expect(feedback()).toContain(sentence))
  expect(feedback()).toContain(code)
  expect(h.submit).toHaveBeenCalledTimes(0)
  expect(action().disabled).toBe(true)
  expect(screen.queryByText(published)).toBe(null)
  expect(mounted.container.textContent).not.toContain('private path')
  mounted.unmount()
  return h
}

it('R13C-15 renders equipment assignment refusal', async () => {
  for (const code of ['EQUIPMENT_ASSIGNMENT_REQUIRED', 'equipment_assignment_required']) {
    await assertRefusal('solar-homeruns', code,
      'Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.')
  }
})
it('R13C-16 renders unknown voltage', async () => {
  await assertRefusal('solar-homeruns', 'EQUIPMENT_VOLTAGE_UNKNOWN', 'The inverter has no verified string voltage.')
})
it('R13C-17 renders unknown power', async () => {
  await assertRefusal('solar-homeruns', 'EQUIPMENT_POWER_UNKNOWN', 'The inverter has no verified module power.')
})
it('R13C-18 renders feeder settings refusal', async () => {
  await assertRefusal('solar-feeders', 'VALID_SETTINGS_REQUIRED', 'Feeders require valid L2 collector settings.')
})
it('R13C-19 renders missing collectors', async () => {
  await assertRefusal('solar-feeders', 'STRING_COLLECTORS_REQUIRED', 'Feeders require string collectors.')
})
it('R13C-20 renders topology refusal', async () => {
  await assertRefusal('solar-schedule', 'ROUTING_TOPOLOGY_REQUIRED', 'The schedule requires routing topology.')
  await assertRefusal('solar-homeruns', 'ROUTING_TOPOLOGY_REQUIRED', 'This tool requires valid routing topology.')
})
it('R13C-21 renders incomplete routing', async () => {
  await assertRefusal('solar-schedule', 'COMPLETE_ROUTING_REQUIRED', 'The schedule requires complete current homeruns.')
})
it('R13C-22 renders unavailable electrical output', async () => {
  const h = await assertRefusal('solar-schedule', 'SOLAR_OUTPUT_NOT_CURRENT',
    'Electrical output is unavailable because this Solar design is not current.')
  expect(h.client).not.toHaveProperty('downloadArtifact')
  expect(screen.queryByRole('link')).toBe(null)
})
it('R13C-23 bounds unknown refusal display', async () => {
  await assertRefusal('solar-homeruns', 'NEW_SERVER_CODE', generic)
  await assertRefusal('solar-homeruns', 'SUCCEEDED', generic)
  for (const code of ['bad code /srv/secret', 'A'.repeat(129)]) {
    const h = harness('solar-homeruns', code)
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await waitFor(() => expect(feedback()).toContain(generic))
    expect(mounted.container.textContent).not.toContain(code)
    expect(mounted.container.textContent).not.toContain('hostile detail')
    expect(mounted.container.textContent).not.toContain('/srv/secret')
    expect(h.submit).toHaveBeenCalledTimes(0)
    mounted.unmount()
  }
})
it('R13C-24 renders panel import refusals', async () => {
  for (const code of ['INVALID_PANEL_IMPORT_REQUEST', 'PANELS_ALREADY_PRESENT', 'PANEL_LAYER_FILTER_REQUIRED',
    'SOURCE_INTAKE_REQUIRED', 'INVALID_SOURCE_INTAKE', 'AMBIGUOUS_PANEL_HANDLE', 'NO_PANELS_RECOGNISED',
    'PANEL_LIMIT_EXCEEDED', 'INVALID_PANEL_HANDLE', 'UNSUPPORTED_PANEL_OUTLINE']) {
    await assertRefusal('solar-panels-from-drawing', code, generic)
  }
})
it('R13C-25 renders routing and feeder refusals', async () => {
  for (const code of ['INVALID_ROUTING_REQUEST', 'PERSISTED_GRAPH_UNAVAILABLE', 'INVALID_FEEDER_REQUEST',
    'INVALID_DRAWING_CONTEXT', 'CAPABILITY_NOT_READY', 'INVALID_SCHEDULE_REQUEST', 'INVALID_ROUTE_POINT',
    'INVERTER_ASSIGNMENT_MISMATCH', 'DEGENERATE_ROUTE']) await assertRefusal('solar-feeders', code, generic)
  for (const [code, sentence] of [
    ['VALID_SETTINGS_REQUIRED', 'Feeders require valid L2 collector settings.'],
    ['EQUIPMENT_ASSIGNMENT_REQUIRED', 'This tool requires valid equipment assignments. Review the electrical reasons on the inverter.'],
    ['STRING_COLLECTORS_REQUIRED', 'Feeders require string collectors.'],
    ['ROUTING_TOPOLOGY_REQUIRED', 'This tool requires valid routing topology.'],
    ['COMPLETE_ROUTING_REQUIRED', 'The schedule requires complete current homeruns.'],
  ]) await assertRefusal('solar-feeders', code, sentence)
})
it('R13C-26 renders adapter and graph refusals', async () => {
  for (const code of ['SIP_R3_PROOF_REJECTED', 'INVALID_SEED_PARENT', 'GRAPH_DIGEST_MISMATCH', 'INTAKE_DIGEST_MISMATCH',
    'GRAPH_NOT_EMBEDDED', 'SIP_R3_TOOL_UNSUPPORTED', 'SIP_R3_TOOL_MANIFEST_MISMATCH', 'INVALID_SETTINGS_REQUEST',
    'INVALID_NUMERIC_PARAM', 'DRAWING_ID_CONFLICT', 'GRAPH_ALREADY_EMBEDDED', 'GRAPH_LIMIT_EXCEEDED',
    'INVALID_JSON_OBJECT', 'INVALID_JSON_STRING', 'NONFINITE_NUMBER', 'NUMBER_LIMIT_EXCEEDED', 'INVALID_JSON_VALUE',
    'UNRESOLVED_UNITS']) await assertRefusal('solar-homeruns', code, generic)
  await assertRefusal('solar-homeruns', 'STALE_GRAPH_REVISION', 'The Solar graph revision changed. Refresh before continuing.')
  await assertRefusal('solar-homeruns', 'GRAPH_COMMIT_CANCELLED',
    'The Solar request was cancelled without publishing a drawing version.')
})
it('R13C-27 renders project unavailability', async () => {
  for (const code of ['project_execution_disabled', 'project_adapter_unavailable']) {
    await assertRefusal('solar-feeders', code, 'Project Solar runs are unavailable in this view.')
  }
})
it('R13C-28 starts no request while busy', async () => {
  const h = harness()
  const mounted = render(<ProjectSolarGraphForm {...h.props} busy />)
  expect(screen.getByText('A Solar run is in progress. Wait for it to finish.')).not.toBe(null)
  expect(action().disabled).toBe(true)
  fireEvent.click(action()); fireEvent.click(refresh())
  await act(async () => {})
  expect(h.client.readContext).toHaveBeenCalledTimes(0)
  expect(h.client.catalog).toHaveBeenCalledTimes(0)
  expect(h.submit).toHaveBeenCalledTimes(0)
  mounted.rerender(<ProjectSolarGraphForm {...h.props} />)
  await arm()
  const preflight = deferred()
  h.reads.push(preflight.promise)
  fireEvent.click(action())
  mounted.rerender(<ProjectSolarGraphForm {...h.props} busy />)
  await act(async () => { preflight.resolve({ ok: true, data: context() }); await preflight.promise })
  expect(h.submit).toHaveBeenCalledTimes(0)
})
it('R13C-29 suppresses a second click', async () => {
  const h = harness()
  render(<ProjectSolarGraphForm {...h.props} />)
  await arm()
  const preflight = deferred()
  h.reads.push(preflight.promise)
  const before = h.client.readContext.mock.calls.length
  act(() => { fireEvent.click(action()); fireEvent.click(action()) })
  expect(h.client.readContext).toHaveBeenCalledTimes(before + 1)
  expect(h.submit).toHaveBeenCalledTimes(0)
  await act(async () => { preflight.resolve({ ok: true, data: context() }); await preflight.promise })
  await waitFor(() => expect(h.submit).toHaveBeenCalledTimes(1))
  expect(h.client.readContext).toHaveBeenCalledTimes(before + 1)
})
it('R13C-30 refuses stale scope before requests', async () => {
  for (const variant of ['guard', 'snapshot', 'authority', 'binding', 'client']) {
    const h = harness()
    if (variant === 'guard') h.current = false
    if (variant === 'snapshot') h.snapshot.binding = nextBinding
    if (variant === 'authority') h.snapshot.authorityKey = 'authority-b'
    const props = variant === 'binding' ? { ...h.props, solarBinding: null }
      : variant === 'client' ? { ...h.props, projectClient: null } : h.props
    const mounted = render(<ProjectSolarGraphForm {...props} />)
    fireEvent.click(action()); fireEvent.click(refresh())
    await act(async () => {})
    expect(h.client.readContext).toHaveBeenCalledTimes(0)
    expect(h.client.catalog).toHaveBeenCalledTimes(0)
    expect(h.submit).toHaveBeenCalledTimes(0)
    expect(action().disabled).toBe(true)
    mounted.unmount()
  }
})
it('R13C-31 rejects a changed preflight revision', async () => {
  const h = harness()
  render(<ProjectSolarGraphForm {...h.props} />)
  await arm()
  const before = h.client.readContext.mock.calls.length
  h.rev = 7
  fireEvent.click(action())
  await waitFor(() => expect(screen.getByText('The Solar graph revision changed. Refresh before continuing.')).not.toBe(null))
  expect(h.client.readContext).toHaveBeenCalledTimes(before + 1)
  expect(h.client.catalog).toHaveBeenCalledTimes(1)
  expect(h.submit).toHaveBeenCalledTimes(0)
  expect(action().disabled).toBe(true)
})
it('R13C-32 discards late scope results', async () => {
  for (const stage of ['context', 'preview', 'submission']) {
    for (const transition of ['round-trip', 'authority']) {
      const h = harness()
      const late = deferred()
      if (stage === 'context') h.reads.push(late.promise)
      if (stage === 'preview') h.previews.push(late.promise)
      if (stage === 'submission') h.submissions.push(late.promise)
      const mounted = render(<ProjectSolarGraphForm {...h.props} />)
      if (stage === 'context') await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(1))
      if (stage === 'preview') await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
      if (stage === 'submission') {
        await arm(); fireEvent.click(action())
        await waitFor(() => expect(h.submit).toHaveBeenCalledTimes(1))
      }
      act(() => {
        h.move(nextBinding)
        if (transition === 'round-trip') h.move(binding)
        else h.move(binding, 'authority-b')
      })
      if (transition === 'authority') {
        mounted.rerender(<ProjectSolarGraphForm {...h.props} authorityKey="authority-b" busy />)
      }
      const lateValue = stage === 'context' ? { ok: true, data: context(binding, 99) }
        : stage === 'preview' ? catalog(h.name, 'OLD_SCOPE_CODE') : accepted()
      await act(async () => { late.resolve(lateValue); await late.promise })
      expect(mounted.container.textContent).not.toContain('Graph revision 99')
      expect(mounted.container.textContent).not.toContain('OLD_SCOPE_CODE')
      expect(mounted.container.textContent).not.toContain('The Solar request was submitted.')
      expect(mounted.container.textContent).not.toContain(job)
      expect(mounted.container.textContent).not.toContain(published)
      mounted.unmount()
    }
  }
})
it('R13C-33 disarms after checkout rotation', async () => {
  for (const stage of ['pending', 'armed']) {
    const h = harness()
    const late = deferred()
    if (stage === 'pending') h.previews.push(late.promise)
    render(<ProjectSolarGraphForm {...h.props} />)
    if (stage === 'pending') await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
    else await arm()
    act(() => h.rotate())
    if (stage === 'pending') await act(async () => { late.resolve(catalog(h.name)); await late.promise })
    expect(action().disabled).toBe(true)
    fireEvent.click(action())
    expect(h.submit).toHaveBeenCalledTimes(0)
    const reads = h.client.readContext.mock.calls.length
    fireEvent.click(refresh())
    await arm()
    expect(h.client.readContext).toHaveBeenCalledTimes(reads + 1)
    expect(h.client.catalog).toHaveBeenCalledTimes(2)
    cleanup()
  }
})
it('R13C-34 requires a valid schedule location', async () => {
  const h = harness('solar-schedule')
  render(<ProjectSolarGraphForm {...h.props} />)
  expect(location().value).toBe('')
  await waitFor(() => expect(screen.getByText('Enter two or three comma-separated coordinates, each between -1000000000000 and 1000000000000.')).not.toBe(null))
  for (const text of ['1', '[1,2]', '1e2,3']) {
    fireEvent.change(location(), { target: { value: text } })
    fireEvent.click(action())
    expect(h.client.catalog).toHaveBeenCalledTimes(0)
    expect(h.submit).toHaveBeenCalledTimes(0)
  }
  fireEvent.change(location(), { target: { value: '1,2' } })
  await arm()
  const intent = h.client.catalog.mock.calls[0][0].intents[0]
  expect(intent.params.insertion_point).toEqual([1, 2, 0])
  fireEvent.change(location(), { target: { value: ' +1.0, 2.0, -0 ' } })
  expect(action().disabled).toBe(false)
  expect(h.key).toHaveBeenCalledTimes(1)
  await submitAndWait(h)
  expect(h.submit.mock.calls[0][0]).toBe(intent)
  expect(h.submit.mock.calls[0][0].params.insertion_point).toEqual([1, 2, 0])
})
it('R13C-35 submits the identical prepared object', async () => {
  const h = harness()
  const mounted = render(<ProjectSolarGraphForm {...h.props} />)
  await submitAndWait(h)
  const previewIntent = h.client.catalog.mock.calls[0][0].intents[0]
  const [intent, options] = h.submit.mock.calls[0]
  expect(intent).toBe(previewIntent)
  expect(Object.keys(intent)).toEqual(['binding', 'authorityKey', 'tool', 'params', 'catalogDigest', 'idempotencyKey'])
  expect(intent.params).toEqual({ drawing_id: binding.drawing_id, expected_rev: 6 })
  expect(intent.catalogDigest).toBe(digest)
  expect(intent.idempotencyKey).toBe('intent-1')
  expect(options).toEqual({ binding, authorityKey: 'authority-a', signal: h.parent.signal, isCurrent: expect.any(Function) })
  expect(options.isCurrent()).toBe(true)
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  expect(h.key).toHaveBeenCalledTimes(1)
  expect(mounted.container.textContent).not.toContain('private tool label')
  mounted.unmount()
  expect(options.isCurrent()).toBe(true)
  expect(options.signal.aborted).toBe(false)
})
it('R13C-36 treats acceptance as submitted', async () => {
  for (const [submission, text] of [[accepted(), 'The Solar request was submitted.'],
    [{ ...accepted(), status: 200 }, unknown],
    [{ ...accepted(), data: { status: 'submitted' } }, unknown],
    [{ ...accepted(), data: { job_id: job, status: 'completed' } }, unknown],
    [{ ...accepted(), reason: 'FAIL_CODE' }, unknown],
    [{ ok: false, status: null, reason: 'SIP_R13B_RUN_IN_PROGRESS', envelope: null },
      'A Solar run is in progress. Wait for it to finish.']]) {
    const h = harness(); h.submissions.push(Promise.resolve(submission))
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await submitAndWait(h)
    expect(feedback()).toContain(text)
    expect(mounted.container.textContent).not.toContain(published)
    mounted.unmount()
  }
})
it('R13C-37 renders scoped terminal outcomes', async () => {
  const h = harness()
  const mounted = render(<ProjectSolarGraphForm {...h.props} />)
  await submitAndWait(h)
  for (const [state, text] of [['pending', 'The Solar run is still in progress.'], ['succeeded', published],
    ['failed', 'The Solar run did not publish a drawing version.'], ['refused', generic],
    ['unreadable', 'This Solar run finished, but its output could not be read.'], ['outcome_unknown', unknown],
    ['aborted', 'Observation stopped. The server job may still be running.'],
    ['stale', 'The drawing or session changed. Refresh before continuing.']]) {
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={receipt(h, state)} />)
    await waitFor(() => expect(mounted.container.textContent).toContain(text))
    expect(mounted.container.textContent).not.toContain('private completion message')
    if (state !== 'succeeded') expect(mounted.container.textContent).not.toContain(published)
  }
  mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={receipt(h, 'refused', {
    reasonCode: 'equipment_assignment_required' })} />)
  await waitFor(() => expect(feedback()).toContain('Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.'))
  expect(feedback()).toContain('equipment_assignment_required')
  for (const value of [receipt(h, 'succeeded', { jobId: output }),
    receipt(h, 'succeeded', {}, { binding: nextBinding }), receipt(h, 'succeeded', {}, { authorityKey: 'foreign' }),
    receipt(h, 'succeeded', {}, { tool: 'solar-feeders' }), receipt(h, 'succeeded', {}, { isCurrent: () => false }),
    receipt(h, 'succeeded', {}, { signal: { aborted: true } })]) {
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={value} />)
    expect(mounted.container.textContent).not.toContain(published)
  }
  for (const changes of [{ outputVersionId: null }, { outputVersionId: 'bad' }, { reasonCode: 'FAILED_CODE' }]) {
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={receipt(h, 'succeeded', changes)} />)
    await waitFor(() => expect(feedback()).toContain(unknown))
    expect(mounted.container.textContent).not.toContain(published)
  }
  mounted.unmount()
  const early = harness()
  const delayed = deferred()
  early.submissions.push(delayed.promise)
  const earlyMounted = render(<ProjectSolarGraphForm {...early.props} />)
  await arm()
  fireEvent.click(action())
  await waitFor(() => expect(early.submit).toHaveBeenCalledTimes(1))
  const earlyReceipt = receipt(early, 'succeeded')
  earlyMounted.rerender(<ProjectSolarGraphForm {...early.props} projectOutcome={earlyReceipt} />)
  expect(earlyMounted.container.textContent).not.toContain(published)
  await act(async () => { delayed.resolve(accepted()); await delayed.promise })
  await waitFor(() => expect(feedback()).toContain(published))
})
it('R13C-38 never reports cancellation as success', async () => {
  await assertRefusal('solar-homeruns', 'GRAPH_COMMIT_CANCELLED',
    'The Solar request was cancelled without publishing a drawing version.')
  const h = harness()
  const mounted = render(<ProjectSolarGraphForm {...h.props} />)
  await submitAndWait(h)
  mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={{
    outcome: { state: 'failed', jobId: job, outputVersionId: null, reasonCode: null, message: 'cancelled' },
    meta: { ...h.submit.mock.calls[0][1], tool: h.name } }} />)
  await waitFor(() => expect(feedback()).toContain('The Solar run did not publish a drawing version.'))
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(h.close).toHaveBeenCalledTimes(1)
  expect(h.submit).toHaveBeenCalledTimes(1)
  expect(h.submit.mock.calls[0][0].params).toEqual({ drawing_id: binding.drawing_id, expected_rev: 6 })
  expect(mounted.container.textContent).not.toContain(published)
  expect(h.parent.signal.aborted).toBe(false)
})
it('R13C-39 preserves uncertain submission identity', async () => {
  for (const variant of ['throw', 'lost', 'reported']) {
    const h = harness()
    if (variant === 'throw') h.submit.mockImplementationOnce(() => { throw new Error('private exception /srv/secret') })
    if (variant === 'lost') h.submissions.push(Promise.resolve(null))
    if (variant === 'reported') h.submissions.push(Promise.resolve({ ok: false, status: null,
      reason: 'SIP_R13B_OUTCOME_UNKNOWN', envelope: { message: 'private envelope' } }))
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await submitAndWait(h)
    expect(feedback()).toContain(unknown)
    fireEvent.click(action()); fireEvent.click(refresh())
    await act(async () => {})
    expect(action().disabled).toBe(true)
    expect(h.submit).toHaveBeenCalledTimes(1)
    expect(h.key).toHaveBeenCalledTimes(1)
    expect(h.client.readContext).toHaveBeenCalledTimes(2)
    expect(mounted.container.textContent).not.toContain('private')
    mounted.unmount()
  }
})
it('R13C-40 preserves the manual chain boundary', async () => {
  const fixture = context()
  fixture.intake.solar_design_graph.inverters = [{ validity: { valid: false,
    reasons: ['EQUIPMENT_VOLTAGE_UNKNOWN', 'EQUIPMENT_POWER_UNKNOWN'] } }]
  fixture.intake.solar_design_graph.strings = [{ validity: { valid: false, reasons: ['EQUIPMENT_ASSIGNMENT_REQUIRED'] } }]
  fixture.intake.solar_design_graph.routes = []
  const before = JSON.stringify(fixture)
  const h = harness('solar-homeruns', 'EQUIPMENT_ASSIGNMENT_REQUIRED')
  h.client.readContext.mockResolvedValue({ ok: true, data: fixture })
  render(<ProjectSolarGraphForm {...h.props} />)
  await waitFor(() => expect(feedback()).toContain('Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.'))
  expect(h.submit).toHaveBeenCalledTimes(0)
  expect(JSON.stringify(fixture)).toBe(before)
  expect(fixture.intake.solar_design_graph.routes).toEqual([])
})
it('R13C-41 closes without persistence or job cancellation', async () => {
  const writes = [vi.spyOn(Storage.prototype, 'setItem'), vi.spyOn(Storage.prototype, 'removeItem'),
    vi.spyOn(Storage.prototype, 'clear')]
  try {
    for (const stage of ['context', 'preview', 'submission', 'accepted']) {
      const h = harness('solar-schedule')
      const late = deferred()
      if (stage === 'context') h.reads.push(late.promise, late.promise)
      if (stage === 'preview') h.previews.push(late.promise)
      if (stage === 'submission') h.submissions.push(late.promise)
      const mounted = render(<ProjectSolarGraphForm {...h.props} />)
      fireEvent.change(location(), { target: { value: '1,2' } })
      if (stage === 'context') await waitFor(() => expect(h.client.readContext).toHaveBeenCalled())
      if (stage === 'preview') await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(1))
      if (stage === 'submission') { await arm(); fireEvent.click(action()); await waitFor(() => expect(h.submit).toHaveBeenCalledTimes(1)) }
      if (stage === 'accepted') await submitAndWait(h)
      const localSignals = [...h.client.readContext.mock.calls, ...h.client.catalog.mock.calls].map(([options]) => options.signal)
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      expect(h.close).toHaveBeenCalledTimes(1)
      if (stage === 'context' || stage === 'preview') {
        expect(localSignals.length).toBeGreaterThan(0)
        for (const signal of localSignals) expect(signal.aborted).toBe(true)
      }
      mounted.unmount()
      if (stage === 'context' || stage === 'preview') expect(localSignals.some((signal) => signal.aborted)).toBe(true)
      expect(h.parent.signal.aborted).toBe(false)
      if (h.submit.mock.calls.length) {
        expect(h.submit.mock.calls[0][1].signal).toBe(h.parent.signal)
        expect(h.submit.mock.calls[0][1].isCurrent()).toBe(true)
        expect(h.submit.mock.calls[0][0].params).not.toHaveProperty('cancel')
      }
      if (stage !== 'accepted') {
        const value = stage === 'context' ? { ok: true, data: context() }
          : stage === 'preview' ? catalog(h.name) : accepted()
        await act(async () => { late.resolve(value); await late.promise })
      }
      const remounted = render(<ProjectSolarGraphForm {...h.props} busy />)
      expect(location().value).toBe('')
      remounted.unmount()
    }
    for (const write of writes) expect(write).toHaveBeenCalledTimes(0)
  } finally { for (const write of writes) write.mockRestore() }
})
it('R13C-43 guards rejected submissions with the submitted scope', async () => {
  for (const variant of ['current', 'round-trip', 'replacement', 'checkout']) {
    const h = harness()
    const pending = deferred()
    h.submissions.push(pending.promise)
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await arm()
    fireEvent.click(action())
    await waitFor(() => expect(h.submit).toHaveBeenCalledTimes(1))
    if (variant === 'round-trip') {
      act(() => { h.move(nextBinding); h.move(binding) })
      mounted.rerender(<ProjectSolarGraphForm {...h.props} />)
    }
    if (variant === 'replacement') {
      act(() => h.move(nextBinding))
      mounted.rerender(<ProjectSolarGraphForm {...h.props} solarBinding={nextBinding} busy />)
      act(() => h.move(binding))
      mounted.rerender(<ProjectSolarGraphForm {...h.props} busy />)
    }
    if (variant === 'checkout') act(() => h.rotate())
    await act(async () => {
      pending.reject(new Error('Lost submission response'))
      await pending.promise.catch(() => {})
    })
    if (variant === 'current') expect(feedback()).toContain(unknown)
    else expect(mounted.container.textContent).not.toContain(unknown)
    expect(h.submit).toHaveBeenCalledTimes(1)
    mounted.unmount()
  }
})
it('R13C-44 withdraws completion feedback when its receipt expires', async () => {
  for (const variant of ['false', 'throw', 'accepted']) {
    const h = harness()
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await submitAndWait(h)
    let receiptCurrent = true
    const projectOutcome = receipt(h, 'succeeded', {}, { isCurrent: () => {
      if (!receiptCurrent && variant === 'throw') throw new Error('Expired receipt')
      return receiptCurrent
    } })
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} />)
    await waitFor(() => expect(feedback()).toContain(published))
    if (variant === 'accepted') h.lease += 1
    else receiptCurrent = false
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} />)
    expect(screen.queryByTestId('project-solar-graph-feedback')).toBe(null)
    expect(screen.getByText('The drawing or session changed. Refresh before continuing.')).not.toBe(null)
    expect(mounted.container.textContent).not.toContain(published)
    mounted.unmount()
  }
})
it('R13C-45 contains hostile completion getters', async () => {
  class CompletionBoundary extends React.Component {
    state = { failed: false }
    static getDerivedStateFromError() { return { failed: true } }
    render() { return this.state.failed ? <p>Completion boundary fallback.</p> : this.props.children }
  }
  for (const field of ['reasonCode', 'jobId']) {
    const h = harness()
    const mounted = render(<CompletionBoundary><ProjectSolarGraphForm {...h.props} /></CompletionBoundary>)
    await submitAndWait(h)
    const before = feedback()
    const projectOutcome = receipt(h, 'failed')
    Object.defineProperty(projectOutcome.outcome, field, { get() { throw new Error('Hostile completion') } })
    mounted.rerender(<CompletionBoundary><ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} /></CompletionBoundary>)
    if (field === 'reasonCode') await waitFor(() => expect(feedback()).toContain(unknown))
    else expect(feedback()).toBe(before)
    expect(screen.queryByText('Completion boundary fallback.')).toBe(null)
    expect(mounted.container.textContent).not.toContain('Hostile completion')
    mounted.unmount()
  }
})
it('R13C-49 a replaced receipt predicate withdraws published feedback', async () => {
  for (const variant of ['assigned', 'accessor']) {
    const h = harness()
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await submitAndWait(h)
    const base = receipt(h, 'succeeded')
    let predicate = () => true
    const meta = variant === 'assigned' ? { ...base.meta, isCurrent: predicate }
      : Object.freeze(Object.defineProperty({ ...base.meta }, 'isCurrent', { get: () => predicate, enumerable: true }))
    const projectOutcome = { outcome: base.outcome, meta }
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} />)
    await waitFor(() => expect(feedback()).toContain(published))
    predicate = () => false
    if (variant === 'assigned') meta.isCurrent = predicate
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} />)
    expect(screen.queryByTestId('project-solar-graph-feedback')).toBe(null)
    expect(screen.getByText('The drawing or session changed. Refresh before continuing.')).not.toBe(null)
    expect(mounted.container.textContent).not.toContain(published)
    mounted.unmount()
  }
})
it('R13C-50 a run in progress does not hide an expired receipt', async () => {
  const h = harness()
  const mounted = render(<ProjectSolarGraphForm {...h.props} />)
  await submitAndWait(h)
  let receiptCurrent = true
  const projectOutcome = receipt(h, 'succeeded', {}, { isCurrent: () => receiptCurrent })
  mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} />)
  await waitFor(() => expect(feedback()).toContain(published))
  mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} busy />)
  expect(feedback()).toContain(published)
  expect(screen.getByText('A Solar run is in progress. Wait for it to finish.')).not.toBe(null)
  receiptCurrent = false
  mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} busy />)
  expect(screen.queryByTestId('project-solar-graph-feedback')).toBe(null)
  expect(screen.getByText('The drawing or session changed. Refresh before continuing.')).not.toBe(null)
  expect(screen.queryByText('A Solar run is in progress. Wait for it to finish.')).toBe(null)
  mounted.unmount()
})
it('R13C-51 a completion job id is read once', async () => {
  const other = '88888888-8888-8888-8888-888888888888'
  for (const first of [job, other]) {
    const h = harness()
    const mounted = render(<ProjectSolarGraphForm {...h.props} />)
    await submitAndWait(h)
    const before = feedback()
    const projectOutcome = receipt(h, 'succeeded')
    let reads = 0
    Object.defineProperty(projectOutcome.outcome, 'jobId', { get() {
      reads += 1
      return reads === 1 ? first : (first === job ? other : job)
    } })
    mounted.rerender(<ProjectSolarGraphForm {...h.props} projectOutcome={projectOutcome} />)
    if (first === job) await waitFor(() => expect(feedback()).toContain(published))
    else {
      await act(async () => {})
      expect(feedback()).toBe(before)
      expect(mounted.container.textContent).not.toContain(published)
    }
    expect(reads).toBe(1)
    mounted.unmount()
  }
})
it('R13C-47 prepares a fresh candidate for a changed intake digest', async () => {
  const h = harness()
  const mounted = render(<ProjectSolarGraphForm {...h.props} />)
  await arm()
  expect(h.client.prepareRun).toHaveBeenCalledTimes(1)
  const changed = context()
  changed.context.intake_sha256 = 'c'.repeat(64)
  h.reads.push(Promise.resolve({ ok: true, data: changed }), Promise.resolve({ ok: true, data: changed }))
  fireEvent.click(refresh())
  await waitFor(() => expect(h.client.catalog).toHaveBeenCalledTimes(2))
  await arm()
  expect(h.client.prepareRun).toHaveBeenCalledTimes(2)
  const secondIntent = h.client.prepareRun.mock.results[1].value
  expect(h.client.catalog.mock.calls[1][0].intents[0]).toBe(secondIntent)
  await submitAndWait(h)
  expect(h.submit).toHaveBeenCalledTimes(1)
  expect(h.submit.mock.calls[0][0]).toBe(secondIntent)
  expect(feedback()).toContain('The Solar request was submitted.')
  expect(mounted.container.textContent).not.toContain('The drawing or session changed. Refresh before continuing.')
})
