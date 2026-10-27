import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import * as bridgeModule from './createProjectSolarRunController.js'
import { useAppProjectSolarRuntime } from './useAppProjectSolarRuntime.js'
import { projectSolarAppReason } from './solarProjectAppModel.js'
import { PROJECT_SOLAR_RUN_REASONS } from './solarProjectCompletion.js'
import { projectSizingConductorFeedback } from './SolarSizingForm.jsx'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'
import { getDrawingIntake } from '../api.js'
import { createSolarProjectClient } from './solarProjectClient.js'
import workerUnavailable from './__fixtures__/workerUnavailable.json'

vi.mock('../api.js', () => ({ getDrawingIntake: vi.fn(async () => null) }))

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const J = '55555555-5555-5555-5555-555555555555'
const W = '66666666-6666-6666-6666-666666666666'
const X = '77777777-7777-7777-7777-777777777777'
const T = 'solar-assign-equipment'
const B = Object.freeze({ kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V })
const digest = 'sha256:' + 'a'.repeat(64)
const accepted = { ok: true, status: 202, data: { job_id: J, status: 'submitted' } }
const local = (reason, status = null) => ({ ok: false, status, reason, envelope: null })
const response = data => ({ ok: true, status: 200, data })
const job = (changes = {}) => ({ job_id: J, org_id: O, project_id: P, input_version_id: V,
  tool_name: T, execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: D },
  status: 'complete', output_version_id: W, error: null,
  result: { output_version_id: W, graph_commit: { output_version_id: W } }, ...changes })
const context = (version = V, head = version) => ({ context: { organization_id: O, project_id: P,
  drawing_id: D, input_version_id: version, head_version_id: head, is_head: version === head,
  intake_sha256: 'b'.repeat(64) }, intake: { layers: ['panels'], electrical_reasons: ['EQUIPMENT_VOLTAGE_UNKNOWN'] } })
const row = (changes = {}) => ({ version_id: W, org_id: O, project_id: P, drawing_id: D, ...changes })
const workspace = (rows = [row()]) => ({ project: { project_id: P }, drawing_versions: rows, head_version_id: X })
const deferred = () => {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const drain = async () => { for (let i = 0; i < 45; i += 1) await Promise.resolve() }
function store(initial) {
  let snapshot = initial
  const listeners = new Set()
  return { getSnapshot: () => snapshot,
    subscribe: vi.fn(listener => { listeners.add(listener); return () => listeners.delete(listener) }),
    change(patch) { snapshot = { ...snapshot, ...patch }; for (const listener of [...listeners]) listener() },
    listeners }
}
const realCreate = bridgeModule.createProjectSolarRunController
let bridges
beforeEach(() => {
  bridges = []
  vi.spyOn(bridgeModule, 'createProjectSolarRunController').mockImplementation(options => {
    const real = realCreate(options)
    const bridge = { ...real, onProjectSubmit: vi.fn(real.onProjectSubmit), dispose: vi.fn(real.dispose),
      whenSettled: vi.fn(real.whenSettled) }
    bridges.push(bridge)
    return bridge
  })
})
afterEach(cleanup)

it('G17 runtime worker refusal clears pending and run lock', async () => {
  vi.useFakeTimers()
  const gate = deferred()
  const fetchImpl = vi.fn().mockReturnValueOnce(gate.promise)
    .mockResolvedValueOnce({ ok: false, status: 503, json: async () => workerUnavailable })
  const wire = createSolarProjectClient({ fetchImpl })
  wire.setScope(B, 'session-1')
  try {
    const h = await setup({ toolName: 'solar-panels-from-drawing' })
    h.client.submitRun.mockImplementation(wire.submitRun)
    const intent = wire.prepareRun({ tool: h.prepared.tool, params: { drawing_id: D, expected_rev: 0 },
      catalogDigest: digest, idempotencyKey: 'g17-runtime-1' })
    let pending
    await act(async () => {
      pending = h.result.current.onProjectSubmit(intent, h.options)
      await drain()
    })
    expect(h.result.current.busy).toBe(true)
    expect(h.result.current.projectPending).toMatchObject({ tool: intent.tool, binding: B })
    await act(async () => {
      gate.resolve({ ok: false, status: 503, json: async () => workerUnavailable })
      expect(await pending).toEqual(local('sip_r4_worker_unavailable', 503))
      await drain()
    })
    const message = 'No worker was available to start Import panels from drawing. Try again later: Refresh preview, then Review & run.'
    expect(h.result.current.busy).toBe(false)
    expect(h.result.current.projectPending).toBeNull()
    expect(h.result.current.notice).toBe(message)
    expect(h.result.current.projectOutcome.outcome).toEqual({ state: 'refused', jobId: null,
      outputVersionId: null, reasonCode: 'sip_r4_worker_unavailable', message })
    expect(h.result.current.publishedVersionId).toBeNull()
    expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    expect(h.ws.rehydrate).not.toHaveBeenCalled()
    expect(h.client.readJob).not.toHaveBeenCalled()
    expect(h.client.readSolar).not.toHaveBeenCalled()
    expect(h.client.downloadArtifact).not.toHaveBeenCalled()
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
    // The synchronous outcome callback must release m.run before the awaited
    // submission continuation decides whether the 503 needs an uncertainty lock.
    expect(await submitWith(h, { ...intent, idempotencyKey: 'g17-runtime-2' }))
      .toEqual(local('sip_r4_worker_unavailable', 503))
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(h.client.submitRun).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(0)
    h.unmount()
  } finally { cleanup(); wire.dispose(); vi.useRealTimers() }
})

it('G17 runtime lost-submit refusal retains uncertainty lock', async () => {
  const fetchImpl = vi.fn().mockRejectedValueOnce(new Error('private transport loss'))
    .mockResolvedValueOnce({ ok: false, status: 503, json: async () => workerUnavailable })
  const wire = createSolarProjectClient({ fetchImpl })
  wire.setScope(B, 'session-1')
  try {
    const h = await setup({ toolName: 'solar-panels-from-drawing' })
    h.client.submitRun.mockImplementation(wire.submitRun)
    const intent = wire.prepareRun({ tool: h.prepared.tool, params: { drawing_id: D, expected_rev: 0 },
      catalogDigest: digest, idempotencyKey: 'g17-lost-1' })
    expect(await submitWith(h, intent)).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
    expect(h.result.current.projectOutcome.outcome.state).toBe('outcome_unknown')
    expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
    expect(h.result.current.busy).toBe(false)
    expect(h.result.current.projectPending).toBeNull()
    expect(h.client.readJob).not.toHaveBeenCalled()
    expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    expect(h.ws.rehydrate).not.toHaveBeenCalled()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl.mock.calls[0][1].body).toBe(fetchImpl.mock.calls[1][1].body)
    expect(await submitWith(h, { ...intent, idempotencyKey: 'g17-lost-2' }))
      .toEqual(local('SIP_R13B_RUN_IN_PROGRESS'))
    await h.install(V)
    expect(await submitWith(h, { ...intent, idempotencyKey: 'g17-lost-3' }))
      .toEqual(local('SIP_R13B_RUN_IN_PROGRESS'))
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    h.unmount()
  } finally { cleanup(); wire.dispose() }
})

it('G17 runtime suppresses a late worker refusal after the drawing scope changes', async () => {
  const gate = deferred()
  const wire = createSolarProjectClient({ fetchImpl: vi.fn(() => gate.promise) })
  wire.setScope(B, 'session-1')
  try {
    const h = await setup({ toolName: 'solar-panels-from-drawing' })
    h.client.submitRun.mockImplementation(wire.submitRun)
    const intent = wire.prepareRun({ tool: h.prepared.tool, params: { drawing_id: D, expected_rev: 0 },
      catalogDigest: digest, idempotencyKey: 'g17-stale' })
    let pending
    await act(async () => { pending = h.result.current.onProjectSubmit(intent, h.options); await drain() })
    await h.install(X)
    await act(async () => {
      gate.resolve({ ok: false, status: 503, json: async () => workerUnavailable })
      await pending
      await drain()
    })
    expect(h.result.current.notice ?? '').not.toContain('No worker was available')
    expect(h.result.current.projectOutcome).toBeNull()
    expect(h.client.readJob).not.toHaveBeenCalled()
    expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
    h.unmount()
  } finally { cleanup(); wire.dispose() }
})

it('S1-123 keeps the specific panel-group refusal in the run notice', async () => {
  const h = await setup()
  await h.submit()
  await h.finish({ status: 'failed', result: null, output_version_id: null,
    error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'pvg_no_panel_groups', retryable: false } })
  expect(h.result.current.notice).toBe('Choose a G33 capture that contains panel groups')
  expect(h.result.current.notice).not.toContain('reported code')
  expect(h.result.current.busy).toBe(false)
  h.unmount()
})

it('S1-123 gives a readable fallback for an unlisted refusal', async () => {
  const h = await setup()
  await h.submit()
  await h.finish({ status: 'failed', result: null, output_version_id: null,
    error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'new_internal_refusal', retryable: false } })
  expect(h.result.current.notice).toBe('The server refused this solar tool')
  expect(h.result.current.notice).not.toMatch(/new_internal_refusal|reported code/)
  h.unmount()
})

it('W1C5 releases a proposal run lock, preserves review, and starts commit as a new run', async () => {
  const h = await setup({ toolName: 'solar-solve-proposal' })
  expect(await h.submit()).toEqual(accepted)
  await h.finish({ tool_name: 'solar-solve-proposal', output_version_id: null,
    result: { schema: 'leaf.solar-solve-proposal.v1', candidate_sha256: 'a'.repeat(64), frame_id: 'frame-1',
      phase: 'initial', candidate_summary: [], request_sha256: 'b'.repeat(64), response_sha256: 'c'.repeat(64),
      execution_provenance: { schema: 'leaf.solar-solve-proposal.v1' } } })
  expect(h.result.current.projectOutcome.outcome).toMatchObject({ state: 'proposal', jobId: J, outputVersionId: null })
  expect(h.result.current.projectPending).toBeNull()
  expect(h.result.current.busy).toBe(false)
  expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
  expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
  const commit = { ...h.prepared, tool: 'solar-commit-solve', proposalJobId: J }
  await h.update({ projectCatalog: { ...h.props.projectCatalog,
    families: [{ capabilities: [h.tool, { name: commit.tool, catalog_digest: digest }] }] } })
  const pendingCommit = deferred()
  h.client.readJob.mockImplementation(() => pendingCommit.promise)
  let answer
  await act(async () => { answer = await h.result.current.onProjectSubmit(commit, h.options); await drain() })
  expect(answer).toEqual(accepted)
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
  expect(h.client.submitRun.mock.calls[1][0]).toBe(commit)
  expect(h.result.current.busy).toBe(true)
  await act(async () => {
    pendingCommit.resolve(response(job({ tool_name: 'solar-commit-solve' })))
    await drain()
  })
  expect(h.result.current.projectOutcome.outcome).toMatchObject({ state: 'succeeded', outputVersionId: W })
  const done = await h.complete()
  expect(h.ws.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(W)
  await h.install()
  expect(await done.promise).toBe(true)
  expect(h.result.current.busy).toBe(false)
  h.unmount()
})

async function setup({ strict = false, initialContext, hostEnabled = true, ready = true, toolName = T } = {}) {
  const ws = store({ orgId: O, openProjectId: P, canonicalVersionId: V, workspace: workspace() })
  ws.rehydrate = vi.fn(async () => { const value = workspace(); ws.change({ workspace: value }); return value })
  ws.selectCanonicalVersion = vi.fn(version => ws.change({ canonicalVersionId: version || null }))
  const session = store({ status: 'active', generation: 1 })
  const client = store({ binding: B, authorityKey: 'session-1' })
  const pendingJob = deferred()
  client.submitRun = vi.fn(async () => accepted)
  client.readJob = vi.fn(() => pendingJob.promise)
  client.readContext = vi.fn(async () => response(context(client.getSnapshot().binding.input_version_id)))
  if (initialContext) client.readContext.mockImplementation(() => initialContext.promise)
  client.readSolar = vi.fn(async () => local('SIP_R7_REQUEST_INVALID'))
  client.downloadArtifact = vi.fn(async () => local('SIP_R7_REQUEST_INVALID'))
  let capture = { live: true }
  function owner(binding = B, key = 'session-1') {
    const token = capture
    return { ready, projectClient: client, scope: { mode: 'project', binding, authorityKey: key },
      isCurrent: () => token.live, checkout: { ownsCheckout: true } }
  }
  const tool = { name: toolName, catalog_digest: digest }
  const otherTool = { name: 'solar-settings', catalog_digest: digest }
  const receipt = (a) => ({ client, binding: a.scope.binding, authorityKey: a.scope.authorityKey,
    isCurrent: a.isCurrent, families: [{ capabilities: [tool, otherTool] }] })
  let a = owner()
  let props = { projectMode: true, projectSolar: a, projectCatalog: receipt(a), workspaceController: ws,
    sessionController: session, completedVersionRef: { current: vi.fn() }, hostEnabled,
    externalBusy: false, writeBlockedReason: null }
  const hook = renderHook(value => useAppProjectSolarRuntime(value), { initialProps: props,
    ...(strict ? { wrapper: ({ children }) => <React.StrictMode>{children}</React.StrictMode> } : {}) })
  await act(drain)
  const prepared = Object.freeze({ binding: B, authorityKey: 'session-1', tool: toolName,
    params: Object.freeze({ sentinel: 'same-object' }), catalogDigest: digest, idempotencyKey: 'intent-1' })
  const options = { binding: B, authorityKey: 'session-1', isCurrent: () => true }
  async function update(patch) { props = { ...props, ...patch }; await act(async () => { hook.rerender(props); await drain() }) }
  async function install(version = W, key = 'session-1') {
    capture.live = false
    capture = { live: true }
    const binding = { ...B, input_version_id: version }
    a = owner(binding, key)
    await act(async () => {
      client.change({ binding, authorityKey: key })
      props = { ...props, projectSolar: a, projectCatalog: receipt(a) }
      hook.rerender(props)
      await drain()
    })
  }
  async function submit() {
    let value
    await act(async () => { value = await hook.result.current.onProjectSubmit(prepared, options); await drain() })
    return value
  }
  async function finish(changes) {
    await act(async () => { pendingJob.resolve(response(job(changes))); await drain() })
    return hook.result.current.projectOutcome
  }
  async function complete() {
    const value = hook.result.current.projectOutcome
    let promise
    await act(async () => { promise = hook.result.current.complete(value.outcome, value.meta); await drain() })
    return { promise }
  }
  return { ...hook, client, ws, session, tool, otherTool, prepared, options, pendingJob, update, install,
    submit, finish, complete, invalidate: () => { capture.live = false }, get props() { return props } }
}

it('R14D-04 test_cloud_admission_and_terminal_sentences', async () => {
  const cases = [
    ['CLOUD_AUTH_MISSING', SOLAR_SIZING_RUN_REASONS.grant_missing],
    ['CLOUD_TENANT_UNAUTHORIZED', SOLAR_SIZING_RUN_REASONS.grant_scope],
    ['CLOUD_UPSTREAM_FAILURE', SOLAR_SIZING_RUN_REASONS.cloud_unavailable],
    ['CLOUD_RESPONSE_INVALID', SOLAR_SIZING_RUN_REASONS.cloud_response_invalid],
    ['CLOUD_REQUEST_INVALID', SOLAR_SIZING_RUN_REASONS.request_invalid],
  ]
  for (const [code, sentence] of cases) {
    for (const spelling of [code, code.toLowerCase()]) {
      for (const status of [401, 403, 503]) {
        const error = { reason_code: spelling, message: 'hostile server prose' }
        const outcome = { ok: false, status, envelope: { error } }
        const admission = { status_code: status, error }
        expect(projectSizingConductorFeedback(outcome)).toMatchObject({ reason: code.toLowerCase(), text: sentence, code: spelling })
        expect(projectSizingConductorFeedback({ ok: true, status }, admission, 'preview'))
          .toMatchObject({ reason: code.toLowerCase(), text: sentence, code: spelling })
      }
      const h = await setup({ toolName: 'solar-size-strings' })
      expect(await h.submit()).toEqual(accepted)
      await h.finish({ tool_name: 'solar-size-strings', status: 'failed', result: null, output_version_id: null,
        error: { error_code: 'GRAPH_JOB_FAILED', reason_code: spelling, retryable: false, message: 'hostile server prose' } })
      expect(h.result.current.projectOutcome.outcome).toMatchObject({ state: 'failed', reasonCode: code.toLowerCase(), message: sentence })
      expect(h.result.current.notice).toBe(sentence)
      expect(h.result.current.notice).not.toContain('hostile server prose')
      expect(h.result.current.busy).toBe(false)
      expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
      h.client.readJob.mockReturnValue(deferred().promise)
      expect(await submitWith(h, fresh(h, 'next-cloud'))).toEqual(accepted)
      expect(h.client.submitRun).toHaveBeenCalledTimes(2)
      h.unmount()
    }
  }
})

it('R14D-06 test_unknown_service_outcome_stops_resubmit', async () => {
  const h = await setup({ toolName: 'solar-size-strings' })
  expect(await h.submit()).toEqual(accepted)
  await h.finish({ tool_name: 'solar-size-strings', status: 'failed', result: null, output_version_id: null,
    error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'SIZING_OUTCOME_UNKNOWN', retryable: false, message: 'hostile server prose' } })
  expect(h.result.current.projectOutcome.outcome).toEqual({ state: 'outcome_unknown', jobId: J,
    outputVersionId: null, reasonCode: null, message: PROJECT_SOLAR_RUN_REASONS.outcome_unknown })
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect(h.result.current.busy).toBe(false)
  expect(h.result.current.publishedVersionId).toBeNull()
  expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
  expect(h.client.submitRun).toHaveBeenCalledOnce()
  expect((await submitWith(h, fresh(h, 'forbidden-cloud'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  await h.install(V)
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect((await submitWith(h, fresh(h, 'still-forbidden'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun).toHaveBeenCalledOnce()
})

it('R13G-02 project submission preserves the prepared object and bridge outcome', async () => {
  const h = await setup()
  const value = await h.submit()
  const bridge = bridges.at(-1)
  expect(h.client.submitRun.mock.calls[0][0]).toBe(h.prepared)
  expect(bridge.onProjectSubmit.mock.calls[0][0]).toBe(h.prepared)
  expect(value).toBe(await bridge.onProjectSubmit.mock.results[0].value)
  expect(value).toEqual(accepted)
  expect(bridge.whenSettled).toHaveBeenCalledExactlyOnceWith(B, 'session-1')
  expect(h.client.readSolar).not.toHaveBeenCalled()
  expect(h.client.downloadArtifact).not.toHaveBeenCalled()
})

it('R13G-03 the runtime submission lock closes before the first await', async () => {
  const h = await setup(), gate = deferred()
  h.client.submitRun.mockReturnValue(gate.promise)
  let first, second
  await act(async () => {
    first = h.result.current.onProjectSubmit(h.prepared, h.options)
    second = h.result.current.onProjectSubmit(h.prepared, h.options)
    await drain()
  })
  expect(await second).toEqual(local('SIP_R13B_RUN_IN_PROGRESS'))
  expect(bridges.at(-1).onProjectSubmit).toHaveBeenCalledOnce()
  await act(async () => { gate.resolve(accepted); await first; await drain() })
})

it('R13G-04 only accepted bridge submission enters submitted presentation', async () => {
  const h = await setup()
  await h.submit()
  expect(h.result.current.projectPending).toEqual({ binding: B, authorityKey: 'session-1', tool: T })
  expect(h.result.current.projectOutcome).toBeNull()
  expect(h.result.current.publishedVersionId).toBeNull()
  expect(h.result.current.projectRunMemory).toBeNull()
  expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
  h.unmount()
  for (const input of [local('SIP_R7_REQUEST_INVALID', 400), { ...accepted, status: 200 }, {},
    local('SIP_R7_TRANSPORT_FAILED')]) {
    const bad = await setup()
    bad.client.submitRun.mockResolvedValue(input)
    const result = await bad.submit()
    expect(result.ok).toBe(false)
    expect(bad.result.current.projectPending).toBeNull()
    expect(bad.result.current.projectOutcome.outcome.state).toBe(input.status === 400 ? 'refused' : 'outcome_unknown')
    expect(bad.result.current.publishedVersionId).toBeNull()
    expect(bad.result.current.projectRunMemory).toBeNull()
    bad.unmount()
  }
})

it('R13G-05 closing or changing the project editor preserves accepted observation', async () => {
  const h = await setup()
  act(() => h.result.current.onOpenTool(h.tool))
  await h.submit()
  const bridge = bridges.at(-1), signal = h.result.current.submissionSignal
  act(() => { h.result.current.onClose(); h.result.current.onOpenTool(h.otherTool) })
  await h.update({ hostEnabled: false, externalBusy: true })
  await h.finish()
  expect(bridges.at(-1)).toBe(bridge)
  expect(bridge.dispose).not.toHaveBeenCalled()
  expect(signal.aborted).toBe(false)
  expect(h.client.readJob).toHaveBeenCalledOnce()
  expect(h.props.completedVersionRef.current).toHaveBeenCalledOnce()
})

it('R13G-06 terminal history records publication separately from electrical readiness', async () => {
  const h = await setup()
  await h.submit()
  expect(h.result.current.projectRunMemory).toBeNull()
  await h.finish()
  expect(h.result.current.projectRunMemory.runs[T]).toEqual({ binding: B, authorityKey: 'session-1', ok: true, code: null })
  expect(h.result.current.contextView.intake.electrical_reasons).toEqual(['EQUIPMENT_VOLTAGE_UNKNOWN'])
  h.unmount()
  const failed = await setup()
  await failed.submit()
  await failed.finish({ status: 'failed', result: null, output_version_id: null,
    error: { error_code: 'EQUIPMENT_VOLTAGE_UNKNOWN', retryable: false } })
  expect(failed.result.current.projectRunMemory.runs[T]).toMatchObject({ ok: false })
  expect(failed.result.current.publishedVersionId).toBeNull()
})

it('R13G-07 an uncertain submission cannot acquire a fresh runtime retry key', async () => {
  const h = await setup()
  h.client.submitRun.mockResolvedValue(local('SIP_R7_TRANSPORT_FAILED'))
  expect((await h.submit()).reason).toBe('SIP_R13B_OUTCOME_UNKNOWN')
  const before = h.client.submitRun.mock.calls.length
  expect(before).toBe(2)
  expect(h.client.submitRun.mock.calls[0][0]).toBe(h.client.submitRun.mock.calls[1][0])
  expect((await h.submit()).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun).toHaveBeenCalledTimes(before)
  expect(h.result.current.projectOutcome.outcome.state).toBe('outcome_unknown')
})

it('R13G-08a runtime success dispatches explicit completion exactly once and failure or refusal never dispatches', async () => {
  const h = await setup()
  await h.submit()
  const value = await h.finish()
  expect(h.props.completedVersionRef.current).toHaveBeenCalledExactlyOnceWith(null, value.outcome,
    { projectSolarCompletion: value.meta })
  await h.update({ hostEnabled: false })
  expect(h.props.completedVersionRef.current).toHaveBeenCalledOnce()
  h.unmount()
  const failed = await setup()
  await failed.submit()
  await failed.finish({ status: 'failed', result: null, output_version_id: null,
    error: { error_code: 'EQUIPMENT_VOLTAGE_UNKNOWN', retryable: false } })
  expect(failed.props.completedVersionRef.current).not.toHaveBeenCalled()
  failed.unmount()
  const refused = await setup()
  refused.client.submitRun.mockResolvedValue(local('SIP_R7_REQUEST_INVALID', 400))
  await refused.submit()
  expect(refused.props.completedVersionRef.current).not.toHaveBeenCalled()
})

it('R13G-09 completion validates the returned workspace output row', async () => {
  for (const rows of [[], [row(), row()], [row({ org_id: X })], [row({ project_id: X })], [row({ drawing_id: X })]]) {
    const h = await setup()
    h.ws.rehydrate.mockImplementation(async () => { const value = workspace(rows); h.ws.change({ workspace: value }); return value })
    await h.submit(); await h.finish()
    const done = await h.complete()
    expect(await Promise.race([done.promise, Promise.resolve('pending')])).toBe(false)
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    expect(h.result.current.publishedVersionId).toBe(W)
    h.unmount()
  }
  for (const detached of [false, true]) {
    const h = await setup()
    h.ws.rehydrate.mockImplementation(async () => {
      const value = { ...workspace(), project: { project_id: detached ? P : X } }
      if (!detached) h.ws.change({ workspace: value })
      return value
    })
    await h.submit(); await h.finish()
    expect(await (await h.complete()).promise).toBe(false)
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    h.unmount()
  }
  const h = await setup()
  await h.submit(); await h.finish(); await h.complete()
  expect(h.ws.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(W)
})

it('R13G-10 a newer head never replaces the completed output selection', async () => {
  const h = await setup()
  await h.submit(); await h.finish()
  const done = await h.complete()
  h.client.readContext.mockImplementation(async () => response(context(W, X)))
  await h.install()
  expect(await done.promise).toBe(true)
  expect(h.ws.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(W)
  expect(h.result.current.contextView.context).toMatchObject({ input_version_id: W, is_head: false, head_version_id: X })
  expect(h.result.current.publishedVersionId).toBe(W)
  expect(h.client.readContext.mock.calls.every(([options]) => Object.keys(options).join() === 'signal')).toBe(true)
})

it('R13G-11 selection or session changes during rehydration retire completion', async () => {
  for (const change of [h => { h.ws.change({ canonicalVersionId: X }); h.ws.change({ canonicalVersionId: V }) },
    h => h.ws.change({ openProjectId: X }), h => h.session.change({ status: 'required' }),
    h => { h.session.change({ generation: 2 }); h.session.change({ generation: 1 }) }]) {
    const h = await setup(), gate = deferred()
    h.ws.rehydrate.mockReturnValue(gate.promise)
    await h.submit(); await h.finish()
    const done = await h.complete()
    await act(async () => { change(h); await drain() })
    expect(await Promise.race([done.promise, Promise.resolve('pending')])).toBe(false)
    await act(async () => { const value = workspace(); h.ws.change({ workspace: value }); gate.resolve(value); await drain() })
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    expect(h.result.current.publishedVersionId).toBeNull()
    expect(h.result.current.projectOutcome).toBeNull()
    h.unmount()
  }
})

it('R13G-12 the owned output transition survives its intentional binding change', async () => {
  const h = await setup()
  await h.submit()
  const value = await h.finish(), done = await h.complete()
  await h.install()
  expect(value.meta.isCurrent()).toBe(false)
  expect(await done.promise).toBe(true)
  expect(h.result.current.contextCurrent()).toBe(true)
  expect(h.result.current.contextView.context.input_version_id).toBe(W)
  expect(h.result.current.busy).toBe(false)
  expect(h.result.current.projectOutcome).toBe(value)
  expect(h.result.current.projectRunMemory.runs[T].ok).toBe(true)
  h.unmount()
  const fast = await setup()
  fast.client.readJob.mockResolvedValue(response(job()))
  fast.props.completedVersionRef.current.mockImplementation((_, outcome, options) =>
    fast.result.current.complete(outcome, options.projectSolarCompletion))
  expect((await fast.submit()).ok).toBe(true)
  const dispatch = fast.props.completedVersionRef.current
  expect(dispatch).toHaveBeenCalledOnce()
  await fast.install()
  expect(await dispatch.mock.results[0].value).toBe(true)
  expect(fast.result.current.projectRunMemory.runs[T].ok).toBe(true)
  expect(fast.result.current.projectOutcome.outcome).toBe(dispatch.mock.calls[0][1])
})

it('a same-owner context reread keeps the selected tool; an owner change clears it', async () => {
  const h = await setup({ toolName: 'solar-solve-proposal' })
  act(() => expect(h.result.current.onOpenTool(h.tool)).toBe(true))
  expect(h.result.current.openTool).toBe(h.tool)
  const reread = deferred()
  h.client.readContext.mockReturnValueOnce(reread.promise)
  // Replacing observation restarts context reading without changing the drawing or authority.
  const workspace = store(h.ws.getSnapshot())
  await h.update({ workspaceController: workspace })
  expect(h.result.current.contextView).toBeNull()
  expect(h.result.current.openTool).toBe(h.tool)
  await h.update({ projectCatalog: { ...h.props.projectCatalog, families: [] } })
  expect(h.result.current.openTool.name).toBe(h.tool.name)
  const fresh = { ...h.tool, catalog_digest: 'sha256:' + 'd'.repeat(64) }
  await h.update({ projectCatalog: { ...h.props.projectCatalog, families: [{ capabilities: [fresh] }] } })
  expect(h.result.current.openTool).toBe(fresh)
  await act(async () => { reread.resolve(response(context())); await drain() })
  expect(h.result.current.contextCurrent()).toBe(true)
  expect(h.result.current.openTool).toBe(fresh)
  act(() => h.result.current.onClose())
  expect(h.result.current.openTool).toBeNull()
  act(() => expect(h.result.current.onOpenTool(fresh)).toBe(true))
  await h.install(V, 'foreign')
  expect(h.result.current.openTool).toBeNull()
  act(() => expect(h.result.current.onOpenTool(h.tool)).toBe(true))
  await act(async () => { workspace.change({ canonicalVersionId: X }); await drain() })
  await h.install(X, 'foreign')
  expect(h.result.current.openTool).toBeNull()
  act(() => expect(h.result.current.onOpenTool(h.tool)).toBe(true))
  await h.update({ projectMode: false })
  expect(h.result.current.openTool).toBeNull()
})

it('an uncertain run on a capture rotation still closes the selected tool', async () => {
  const h = await setup()
  act(() => expect(h.result.current.onOpenTool(h.tool)).toBe(true))
  expect(h.result.current.openTool).toBe(h.tool)
  expect(await h.submit()).toEqual(accepted)
  expect(h.result.current.projectPending).not.toBeNull()
  expect(h.result.current.projectOutcome).toBeNull()
  expect(h.result.current.busy).toBe(true)
  await h.install(V)
  expect(h.result.current.openTool).toBeNull()
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  h.unmount()
})

it('W1C8 retains an in-flight read across equal bindings and capture rotations', async () => {
  const read = deferred(), h = await setup({ initialContext: read })
  const reads = h.client.readContext.mock.calls.length
  const signal = h.client.readContext.mock.calls.at(-1)[0].signal
  const bridge = bridges.at(-1)
  await h.install(V)
  await h.install(V)
  expect(signal.aborted).toBe(false)
  expect(h.client.readContext).toHaveBeenCalledTimes(reads)
  expect(bridges.at(-1)).toBe(bridge)
  await act(async () => { read.resolve(response(context())); await drain() })
  expect(h.result.current.contextView).toEqual(context())
  expect(h.result.current.contextCurrent()).toBe(true)
})

it.each(['mode', 'readiness', 'client', 'authority', 'binding', 'workspace', 'session', 'unmount'])(
  'W1C8 aborts an obsolete context for a real %s change and rejects its late reply', async change => {
    const read = deferred(), h = await setup({ initialContext: read })
    const signal = h.client.readContext.mock.calls.at(-1)[0].signal
    h.client.readContext.mockImplementation(async () => response(context(h.client.getSnapshot().binding.input_version_id)))
    if (change === 'mode') await h.update({ projectMode: false })
    if (change === 'readiness') await h.update({ projectSolar: { ...h.props.projectSolar, ready: false } })
    if (change === 'client') await h.update({ projectSolar: { ...h.props.projectSolar, projectClient: { ...h.client } } })
    if (change === 'authority') await h.install(V, 'foreign')
    if (change === 'binding') {
      await act(async () => { h.ws.change({ canonicalVersionId: X }); await drain() })
      await h.install(X)
    }
    if (change === 'workspace') await h.update({ workspaceController: store(h.ws.getSnapshot()) })
    if (change === 'session') await h.update({ sessionController: store(h.session.getSnapshot()) })
    if (change === 'unmount') h.unmount()
    expect(signal.aborted).toBe(true)
    const obsolete = { ...context(), intake: { stale: true } }
    await act(async () => { read.resolve(response(obsolete)); await drain() })
    expect(h.result.current.contextView).not.toBe(obsolete)
    if (['mode', 'readiness', 'unmount'].includes(change)) expect(h.result.current.contextView).toBeNull()
    else expect(h.result.current.contextCurrent()).toBe(true)
  },
)

it('R13G-13 exact context reads discard obsolete replies and restart for a new ready owner', async () => {
  const gate = deferred(), h = await setup({ initialContext: gate })
  h.invalidate()
  await act(async () => { gate.resolve(response(context())); await drain() })
  expect(h.result.current.contextView).toBeNull()
  h.client.readContext.mockImplementation(async () => response(context()))
  await h.update({ projectSolar: { ...h.props.projectSolar, ready: false } })
  await h.install(V)
  expect(h.result.current.contextCurrent()).toBe(true)
  act(() => h.result.current.onOpenTool(h.tool))
  await h.submit()
  expect(h.result.current.projectPending).not.toBeNull()
  await h.install(V)
  expect(h.result.current.projectPending).toBeNull()
  expect(h.result.current.projectOutcome).toBeNull()
  expect(h.result.current.busy).toBe(false)
  // An accepted run loses its capture and becomes uncertain, closing the selected tool.
  expect(h.result.current.openTool).toBeNull()
  const stale = deferred()
  h.client.readContext.mockReturnValue(stale.promise)
  await h.update({ projectSolar: { ...h.props.projectSolar, ready: false } })
  await h.install(V)
  const oldSignal = h.client.readContext.mock.calls.at(-1)[0].signal
  await act(async () => { h.ws.change({ canonicalVersionId: X }); h.ws.change({ canonicalVersionId: V }); await drain() })
  await act(async () => { stale.resolve(response(context(X))); await drain() })
  expect(oldSignal.aborted).toBe(true)
  expect(h.result.current.contextView).toBeNull()
})

it('R13G-14 unreadable publication retains its receipt and retries only its output', async () => {
  const h = await setup()
  h.ws.rehydrate.mockResolvedValueOnce(null)
  await h.submit(); await h.finish()
  expect(await (await h.complete()).promise).toBe(false)
  expect(h.result.current.publishedVersionId).toBe(W)
  expect(h.result.current.notice).toBe(projectSolarAppReason('output_unreadable'))
  expect(h.result.current.canRetryReadback).toBe(true)
  expect((await h.submit()).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  let retry
  await act(async () => { retry = h.result.current.retryReadback(); await drain() })
  expect(h.result.current.canRetryReadback).toBe(false)
  expect(h.ws.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(W)
  h.client.readContext.mockResolvedValue(local('SIP_R7_RESPONSE_INVALID'))
  await h.install()
  expect(await retry).toBe(false)
  expect(h.result.current.publishedVersionId).toBe(W)
  expect(h.result.current.notice).toBe(projectSolarAppReason('output_unreadable'))
  expect(h.result.current.canRetryReadback).toBe(true)
  h.client.readContext.mockImplementation(async () => response(context(W, X)))
  await act(async () => { retry = h.result.current.retryReadback(); await drain() })
  expect(await retry).toBe(true)
  expect(h.ws.rehydrate).toHaveBeenCalledTimes(2)
  expect(h.ws.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(W)
  expect(h.result.current.contextView.context.input_version_id).toBe(W)
  expect(h.result.current.notice).toBeNull()
  expect(h.result.current.canRetryReadback).toBe(false)
  expect(getDrawingIntake).not.toHaveBeenCalled()
})

it('completed publication with a later checkout notice does not offer output readback refresh', async () => {
  const h = await setup()
  expect(h.result.current.canRetryReadback).toBe(false)
  await h.submit(); await h.finish()
  const done = await h.complete()
  expect(h.result.current.canRetryReadback).toBe(false)
  await h.install(W)
  expect(await done.promise).toBe(true)
  await h.update({ writeBlockedReason: 'checkout_required' })
  const binding = h.props.projectSolar.scope.binding
  expect((await submitWith(h, { ...fresh(h, 'blocked-after-publication'), binding }, { ...h.options, binding })).reason)
    .toBe('SIP_R7_REQUEST_INVALID')
  expect(h.result.current.publishedVersionId).toBe(W)
  expect(h.result.current.notice).toBe(projectSolarAppReason('checkout_required'))
  expect(h.result.current.busy).toBe(false)
  expect(h.result.current.canRetryReadback).toBe(false)
})

it('R13G-15 retained project callbacks refuse standalone and foreign owners', async () => {
  for (const replace of [h => h.update({ projectMode: false }), h => h.install(V, 'foreign')]) {
    const h = await setup()
    await h.submit(); const value = await h.finish()
    const retained = h.result.current
    const before = h.client.submitRun.mock.calls.length
    await replace(h)
    await act(async () => {
      expect((await retained.onProjectSubmit(h.prepared, h.options)).ok).toBe(false)
      expect(await retained.complete(value.outcome, value.meta)).toBe(false)
      expect(await retained.retryReadback()).toBe(false)
    })
    expect(h.client.submitRun).toHaveBeenCalledTimes(before)
    expect(h.ws.rehydrate).not.toHaveBeenCalled()
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    expect(h.result.current.projectOutcome).toBeNull()
    expect(h.result.current.projectPending).toBeNull()
    expect(h.result.current.publishedVersionId).toBeNull()
    h.unmount()
  }
})

it('R13G-1a01 StrictMode replaces and disposes the bridge owner', async () => {
  const h = await setup({ strict: true })
  expect(bridges).toHaveLength(2)
  expect(bridges[0].dispose).toHaveBeenCalledOnce()
  expect(bridges[1].dispose).not.toHaveBeenCalled()
  expect((await h.submit()).ok).toBe(true)
  const signal = h.result.current.submissionSignal
  h.unmount()
  expect(signal.aborted).toBe(true)
  expect(bridges[1].dispose).toHaveBeenCalledOnce()
  expect(h.ws.listeners.size).toBe(0)
  expect(h.session.listeners.size).toBe(0)
})

it('R13G-1a02 host admission never gates ready context or accepted output readback', async () => {
  const h = await setup({ hostEnabled: false })
  expect(h.result.current.contextCurrent()).toBe(true)
  expect(h.result.current.onOpenTool(h.tool)).toBe(false)
  expect((await h.submit()).ok).toBe(false)
  await h.update({ hostEnabled: true })
  await h.submit(); await h.finish()
  await h.update({ hostEnabled: false })
  const done = await h.complete()
  await h.install()
  expect(await done.promise).toBe(true)
  expect(h.result.current.contextCurrent()).toBe(true)
  h.unmount()
  const unready = await setup({ ready: false })
  expect(unready.client.readContext).not.toHaveBeenCalled()
})

it('R13G-1a03 live write gates and catalog receipts refuse stale entry', async () => {
  const h = await setup(), retained = h.result.current.onProjectSubmit
  for (const patch of [{ externalBusy: true }, { externalBusy: false, writeBlockedReason: 'checkout_required' }]) {
    await h.update(patch)
    await act(async () => { expect((await retained(h.prepared, h.options)).ok).toBe(false) })
  }
  await h.update({ writeBlockedReason: null })
  expect(h.result.current.onOpenTool({ ...h.tool })).toBe(false)
  act(() => { expect(h.result.current.onOpenTool(h.tool)).toBe(true) })
  await h.update({ projectCatalog: { ...h.props.projectCatalog, isCurrent: () => false } })
  expect((await h.submit()).ok).toBe(false)
  expect(h.client.submitRun).not.toHaveBeenCalled()
})

it('R13G-1a04 retirement settles pending context retry and clears scoped layers', async () => {
  const h = await setup()
  const oldSetLayers = h.result.current.setVisibleLayers
  act(() => h.result.current.setVisibleLayers({ panels: false }))
  expect(h.result.current.visibleLayers).toEqual({ panels: false })
  await h.submit(); await h.finish()
  const done = await h.complete()
  const gate = deferred()
  h.client.readContext.mockReturnValue(gate.promise)
  await h.install()
  act(() => oldSetLayers({ obsolete: false }))
  expect(h.result.current.visibleLayers).toEqual({})
  const signal = h.client.readContext.mock.calls.at(-1)[0].signal
  let retry
  act(() => { retry = h.result.current.retryReadback() })
  h.unmount()
  await act(drain)
  expect(await Promise.race([done.promise, Promise.resolve('pending')])).toBe(false)
  expect(await retry).toBe(false)
  expect(signal.aborted).toBe(true)
  await act(async () => { gate.resolve(response(context(W))); await drain() })
})

it('R13G-1a05 output readback has a bounded deadline even without owner retirement', async () => {
  vi.useFakeTimers()
  try {
    const h = await setup(), gate = deferred()
    h.ws.rehydrate.mockReturnValueOnce(gate.promise)
    await h.submit(); await h.finish()
    const done = await h.complete()
    await act(async () => { await vi.advanceTimersByTimeAsync(120000); await drain() })
    expect(await done.promise).toBe(false)
    expect(h.result.current.publishedVersionId).toBe(W)
    expect(h.result.current.notice).toBe(projectSolarAppReason('output_unreadable'))
    expect(h.result.current.busy).toBe(false)
    let retry
    await act(async () => { retry = h.result.current.retryReadback(); await drain() })
    expect(h.ws.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(W)
    await act(async () => { await vi.advanceTimersByTimeAsync(120000); await drain() })
    expect(await retry).toBe(false)
    expect(h.result.current.busy).toBe(false)
    h.unmount()
  } finally { cleanup(); vi.useRealTimers() }
})

async function submitWith(h, prepared, options = h.options) {
  let value
  await act(async () => { value = await h.result.current.onProjectSubmit(prepared, options); await drain() })
  return value
}
const fresh = (h, key, authorityKey = 'session-1') => Object.freeze({ ...h.prepared, authorityKey, idempotencyKey: key })

it('R13G-16 an uncertain submission keeps its lock across a capture rotation and releases it only for a new owner', async () => {
  // A: the submission itself is lost twice, so its outcome is unknown and the bridge reports it.
  const h = await setup()
  h.client.submitRun.mockResolvedValue(local('SIP_R7_TRANSPORT_FAILED'))
  expect((await h.submit()).reason).toBe('SIP_R13B_OUTCOME_UNKNOWN')
  await h.install(V)
  expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1', 'intent-1'])
  // Positive control: a new owner (another authority) releases the lock.
  await h.install(V, 'foreign')
  h.client.submitRun.mockResolvedValue(accepted)
  const released = await submitWith(h, fresh(h, 'fresh-3', 'foreign'), { ...h.options, authorityKey: 'foreign' })
  expect(released).toEqual(accepted)
  expect(h.client.submitRun).toHaveBeenCalledTimes(3)
  expect(h.client.submitRun.mock.calls[2][0].idempotencyKey).toBe('fresh-3')
  h.unmount()
  // B: the job was accepted, then its outcome became unknown; only the bridge notification marks it.
  const b = await setup()
  b.client.readJob.mockResolvedValue(local('SIP_R7_TRANSPORT_FAILED'))
  expect(await b.submit()).toEqual(accepted)
  expect(b.result.current.projectOutcome.outcome.state).toBe('outcome_unknown')
  await b.install(V)
  expect((await submitWith(b, fresh(b, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(b.client.submitRun).toHaveBeenCalledOnce()
  b.unmount()
  // C: the budget elapses on the same tick the submission leaves scope, so no notification is
  // delivered and only the submission's own answer marks the run uncertain.
  vi.useFakeTimers()
  try {
    const c = await setup(), gate = deferred()
    c.client.submitRun.mockReturnValue(gate.promise)
    const start = Date.now()
    const racing = { ...c.options, isCurrent: () => Date.now() < start + bridgeModule.PROJECT_SOLAR_RUN_BUDGET_MS }
    let first
    await act(async () => { first = c.result.current.onProjectSubmit(c.prepared, racing); await drain() })
    await act(async () => { await vi.advanceTimersByTimeAsync(bridgeModule.PROJECT_SOLAR_RUN_BUDGET_MS); await drain() })
    expect(await first).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
    expect(c.result.current.projectOutcome).toBeNull()
    // A resolving mock, so a lost lock fails on the refusal below rather than on a hung submission.
    c.client.submitRun.mockResolvedValue(accepted)
    await c.install(V)
    expect((await submitWith(c, fresh(c, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
    expect(c.client.submitRun).toHaveBeenCalledOnce()
    c.unmount()
  } finally { cleanup(); vi.useRealTimers() }
})

it('R13G-17 an authority round trip before selection loses the completion and frees the runtime', async () => {
  for (const ending of ['resolve', 'reject']) {
    const h = await setup(), gate = deferred()
    h.ws.rehydrate.mockReturnValue(gate.promise)
    await h.submit(); await h.finish()
    const done = await h.complete()
    expect(h.result.current.publishedVersionId).toBe(W)
    await act(async () => { h.client.change({ authorityKey: 'foreign' }); h.client.change({ authorityKey: 'session-1' }); await drain() })
    await act(async () => {
      if (ending === 'resolve') { const value = workspace(); h.ws.change({ workspace: value }); gate.resolve(value) }
      else gate.reject(new Error('workspace read failed'))
      await drain()
    })
    expect(await done.promise).toBe(false)
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    expect(h.result.current.publishedVersionId).toBeNull()
    expect(h.result.current.busy).toBe(false)
    expect(h.result.current.notice).toBeNull()
    expect(h.result.current.projectOutcome).toBeNull()
    expect(h.result.current.projectRunMemory.runs[T].ok).toBe(true)
    h.client.readJob.mockReturnValue(new Promise(() => {}))
    expect(await submitWith(h, fresh(h, 'fresh-2'))).toEqual(accepted)
    expect(h.client.submitRun).toHaveBeenCalledTimes(2)
    h.unmount()
  }
})

it('R13G-18 a completion whose scope left during a hung readback settles as lost at its deadline', async () => {
  vi.useFakeTimers()
  try {
    const h = await setup()
    h.ws.rehydrate.mockReturnValueOnce(new Promise(() => {}))
    await h.submit(); await h.finish()
    const done = await h.complete()
    await act(async () => { h.client.change({ authorityKey: 'foreign' }); h.client.change({ authorityKey: 'session-1' }); await drain() })
    expect(h.result.current.publishedVersionId).toBe(W)
    expect(h.result.current.busy).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(bridgeModule.PROJECT_SOLAR_RUN_BUDGET_MS); await drain() })
    expect(await done.promise).toBe(false)
    expect(h.result.current.busy).toBe(false)
    expect(h.result.current.publishedVersionId).toBeNull()
    expect(h.result.current.notice).toBeNull()
    expect(h.result.current.projectOutcome).toBeNull()
    expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
    h.unmount()
  } finally { cleanup(); vi.useRealTimers() }
})

it('R13G-19 an in-flight replay keeps the lock across capture rotation and an unknown answer', async () => {
  const h = await setup(), replay = deferred()
  h.client.submitRun.mockResolvedValueOnce(local('SIP_R7_TRANSPORT_FAILED')).mockReturnValueOnce(replay.promise)
  let first
  await act(async () => { first = h.result.current.onProjectSubmit(h.prepared, h.options); await drain() })
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
  const bridge = bridges.at(-1)
  // Flush the rotated capture's effect while the submission continuation is still pending.
  act(() => {
    h.invalidate()
    const a = { ...h.props.projectSolar, isCurrent: () => true }
    h.rerender({ ...h.props, projectSolar: a, projectCatalog: { ...h.props.projectCatalog, isCurrent: a.isCurrent } })
    h.client.change({ binding: B })
  })
  await act(drain)
  expect(bridges.at(-1)).toBe(bridge)
  expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  await act(async () => { replay.resolve(local('SIP_R7_TRANSPORT_FAILED')); await drain() })
  expect(await first).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
  expect((await submitWith(h, fresh(h, 'fresh-3'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  await h.install(V)
  expect((await submitWith(h, fresh(h, 'fresh-4'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1', 'intent-1'])
})

it('R13G-20 a never-sent invalid request releases its lock when the capture rotates before settlement', async () => {
  const h = await setup()
  let first
  // B rejects the empty key before transport; rotate before the runtime awaits that answer.
  act(() => {
    first = h.result.current.onProjectSubmit(fresh(h, ''), h.options)
    h.invalidate()
    const a = { ...h.props.projectSolar, isCurrent: () => true }
    h.rerender({ ...h.props, projectSolar: a, projectCatalog: { ...h.props.projectCatalog, isCurrent: a.isCurrent } })
  })
  await act(drain)
  expect(await first).toEqual(local('SIP_R7_REQUEST_INVALID'))
  expect(h.client.submitRun).not.toHaveBeenCalled()
  expect(await submitWith(h, fresh(h, 'fresh-2'))).toEqual(accepted)
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['fresh-2'])
})

it('R13G-21 deadline loss invalidates context and releases the lock before retry settlement callbacks', async () => {
  vi.useFakeTimers()
  try {
    const h = await setup()
    h.ws.rehydrate.mockReturnValueOnce(new Promise(() => {}))
    await h.submit(); await h.finish()
    const done = await h.complete()
    let observed
    const retry = h.result.current.retryReadback().then(async value => {
      const contextCurrent = h.result.current.contextCurrent()
      const submission = await h.result.current.onProjectSubmit(fresh(h, 'fresh-2'), h.options)
      observed = { value, contextCurrent, reason: submission.reason }
    })
    await act(async () => { h.client.change({ authorityKey: 'foreign' }); h.client.change({ authorityKey: 'session-1' }); await drain() })
    await act(async () => { await vi.advanceTimersByTimeAsync(bridgeModule.PROJECT_SOLAR_RUN_BUDGET_MS); await drain() })
    await retry
    expect(observed).toEqual({ value: false, contextCurrent: false, reason: 'SIP_R7_REQUEST_INVALID' })
    expect(await done.promise).toBe(false)
    expect(h.client.submitRun).toHaveBeenCalledOnce()
    h.unmount()
  } finally { cleanup(); vi.useRealTimers() }
}, 15000)

it('R13G-22 loss before selection clears visible layers after an authority round trip', async () => {
  const h = await setup(), gate = deferred()
  act(() => h.result.current.setVisibleLayers({ panels: false }))
  expect(h.result.current.visibleLayers).toEqual({ panels: false })
  h.ws.rehydrate.mockReturnValueOnce(gate.promise)
  await h.submit(); await h.finish()
  const done = await h.complete()
  await act(async () => { h.client.change({ authorityKey: 'foreign' }); h.client.change({ authorityKey: 'session-1' }); await drain() })
  await act(async () => { const value = workspace(); h.ws.change({ workspace: value }); gate.resolve(value); await drain() })
  expect(await done.promise).toBe(false)
  expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
  expect(h.result.current.visibleLayers).toEqual({})
})

it('R13G-23 a never-sent stale submission releases its lock across same-owner rotations', async () => {
  vi.useFakeTimers()
  try {
    const h = await setup(), bridge = bridges.at(-1)
    let first
    act(() => {
      first = h.result.current.onProjectSubmit(h.prepared, h.options)
      h.invalidate()
      const a = { ...h.props.projectSolar, isCurrent: () => true }
      h.rerender({ ...h.props, projectSolar: a, projectCatalog: { ...h.props.projectCatalog, isCurrent: a.isCurrent } })
    })
    await act(drain)
    expect(await first).toEqual(local('SIP_R7_SCOPE_STALE'))
    expect(await bridge.onProjectSubmit.mock.results[0].value).toEqual(local('SIP_R7_SCOPE_STALE'))
    expect(h.client.submitRun).not.toHaveBeenCalled()
    await h.install(V)
    await h.install(V)
    await act(async () => { await vi.advanceTimersByTimeAsync(120001); await drain() })
    expect(bridges.at(-1)).toBe(bridge)
    expect(await submitWith(h, fresh(h, 'fresh-2'))).toEqual(accepted)
    expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['fresh-2'])
  } finally { cleanup(); vi.useRealTimers() }
})

it('R13G-24 a never-sent aborted submission releases its lock across same-owner rotations', async () => {
  vi.useFakeTimers()
  try {
    // The runtime passes its owner signal to B; caller abort is a stale predicate, while owner abort
    // retires the owner. Stub the existing controller spy for the same-owner never-sent ABORTED case.
    vi.mocked(bridgeModule.createProjectSolarRunController).mockImplementationOnce(options => {
      const real = realCreate(options)
      const bridge = { ...real, onProjectSubmit: vi.fn(real.onProjectSubmit)
        .mockResolvedValueOnce(local('SIP_R7_ABORTED')) }
      bridges.push(bridge)
      return bridge
    })
    const h = await setup(), bridge = bridges.at(-1)
    let first
    act(() => {
      first = h.result.current.onProjectSubmit(h.prepared, h.options)
      h.invalidate()
      const a = { ...h.props.projectSolar, isCurrent: () => true }
      h.rerender({ ...h.props, projectSolar: a, projectCatalog: { ...h.props.projectCatalog, isCurrent: a.isCurrent } })
    })
    await act(drain)
    expect(await first).toEqual(local('SIP_R7_ABORTED'))
    expect(await bridge.onProjectSubmit.mock.results[0].value).toEqual(local('SIP_R7_ABORTED'))
    expect(h.client.submitRun).not.toHaveBeenCalled()
    await h.install(V)
    await h.install(V)
    await act(async () => { await vi.advanceTimersByTimeAsync(120001); await drain() })
    expect(bridges.at(-1)).toBe(bridge)
    expect(await submitWith(h, fresh(h, 'fresh-2'))).toEqual(accepted)
    expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['fresh-2'])
  } finally { cleanup(); vi.useRealTimers() }
})

it('R13G-25 an accepted non-terminal run keeps its lock and explains lost observation after rotation', async () => {
  vi.useFakeTimers()
  try {
    const h = await setup(), bridge = bridges.at(-1)
    expect(await h.submit()).toEqual(accepted)
    expect(h.client.readJob).toHaveBeenCalledOnce()
    await h.install(V)
    expect(bridges.at(-1)).toBe(bridge)
    expect(h.result.current.contextCurrent()).toBe(true)
    expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
    expect(h.result.current.busy).toBe(false)
    expect(h.result.current.projectPending).toBeNull()
    expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
    // Even a late success cannot dispatch completion from the obsolete capture.
    await h.finish()
    expect(h.props.completedVersionRef.current).not.toHaveBeenCalled()
    expect(h.result.current.projectOutcome).toBeNull()
    await h.install(V)
    await act(async () => { await vi.advanceTimersByTimeAsync(120001); await drain() })
    expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
    expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
    expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1'])
  } finally { cleanup(); vi.useRealTimers() }
})

it('R13G-26 an authority change releases an accepted run held after same-owner rotation', async () => {
  const h = await setup(), bridge = bridges.at(-1)
  expect(await h.submit()).toEqual(accepted)
  await h.install(V)
  expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1'])
  await h.install(V, 'foreign')
  expect(bridges.at(-1)).not.toBe(bridge)
  expect(h.result.current.notice).toBeNull()
  expect(await submitWith(h, fresh(h, 'fresh-3', 'foreign'), { ...h.options, authorityKey: 'foreign' })).toEqual(accepted)
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1', 'fresh-3'])
})

it('R13G-27 a transport ABORTED answer becomes unknown in B2 and keeps the runtime lock after rotation', async () => {
  const h = await setup()
  h.client.submitRun.mockResolvedValueOnce(local('SIP_R7_ABORTED'))
  expect(await h.submit()).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
  expect(await bridges.at(-1).onProjectSubmit.mock.results[0].value).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
  expect(h.result.current.projectOutcome).toBeNull()
  await h.install(V)
  expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1'])
})

it('R13G-28 a same-owner rotation retains context, the unknown notice and the lock without another read', async () => {
  const h = await setup()
  expect(await h.submit()).toEqual(accepted)
  const reads = h.client.readContext.mock.calls.length
  const retained = h.result.current.contextView
  h.client.readContext.mockResolvedValueOnce(local('SIP_R7_TRANSPORT_FAILED'))
  await h.install(V)
  expect(h.client.readContext).toHaveBeenCalledTimes(reads)
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect(h.result.current.contextView).toBe(retained)
  expect(h.result.current.contextCurrent()).toBe(true)
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect(h.result.current.busy).toBe(false)
  expect(h.result.current.projectPending).toBeNull()
  expect((await submitWith(h, fresh(h, 'fresh-2'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1'])
})

it('R13G-29 an equal binding copy while the capture stays current keeps the accepted run followable', async () => {
  const h = await setup(), bridge = bridges.at(-1)
  expect(await h.submit()).toEqual(accepted)
  const reads = h.client.readContext.mock.calls.length
  const copy = { ...B }
  const a = { ...h.props.projectSolar, scope: { ...h.props.projectSolar.scope, binding: copy } }
  await h.update({ projectSolar: a, projectCatalog: { ...h.props.projectCatalog, binding: copy } })
  expect(bridges.at(-1)).toBe(bridge)
  expect(h.client.readContext.mock.calls.length).toBe(reads)
  expect(h.result.current.notice).toBeNull()
  expect(h.result.current.busy).toBe(true)
  expect(h.result.current.projectPending).not.toBeNull()
  expect(h.result.current.contextCurrent()).toBe(true)
  await h.finish()
  expect(h.result.current.projectOutcome?.outcome.state).toBe('succeeded')
  expect(h.props.completedVersionRef.current).toHaveBeenCalledOnce()
})


it('R13G-30 a done transition cannot replace a later unknown run notice or refetch on capture rotation', async () => {
  const h = await setup()
  await h.submit(); await h.finish()
  const done = await h.complete()
  await h.install(W)
  expect(await done.promise).toBe(true)
  const binding = h.props.projectSolar.scope.binding
  const prepared = { ...fresh(h, 'run-two'), binding }
  const options = { ...h.options, binding }
  h.client.readJob.mockReturnValue(deferred().promise)
  expect(await submitWith(h, prepared, options)).toEqual(accepted)
  const reads = h.client.readContext.mock.calls.length
  h.client.readContext.mockResolvedValueOnce(local('SIP_R7_TRANSPORT_FAILED'))
  await h.install(W)
  expect(h.client.readContext).toHaveBeenCalledTimes(reads)
  expect(h.result.current.contextCurrent()).toBe(true)
  expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect.soft(h.result.current.busy).toBe(false)
  expect.soft(h.result.current.projectPending).toBeNull()
  expect.soft((await submitWith(h, { ...prepared, idempotencyKey: 'run-three' }, options)).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect.soft(h.client.submitRun).toHaveBeenCalledTimes(2)
})

it('R13G-31 a done transition with retained context cannot release a later run lock', async () => {
  const h = await setup()
  await h.submit(); await h.finish()
  const done = await h.complete()
  await h.install(W)
  expect(await done.promise).toBe(true)
  const binding = h.props.projectSolar.scope.binding
  const prepared = { ...fresh(h, 'run-two'), binding }
  const options = { ...h.options, binding }
  h.client.readJob.mockReturnValue(deferred().promise)
  expect(await submitWith(h, prepared, options)).toEqual(accepted)
  const reads = h.client.readContext.mock.calls.length
  const retained = h.result.current.contextView
  await h.install(W)
  expect(h.client.readContext).toHaveBeenCalledTimes(reads)
  expect(h.result.current.contextView).toBe(retained)
  expect.soft(h.result.current.busy).toBe(false)
  expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect.soft((await submitWith(h, { ...prepared, idempotencyKey: 'run-three' }, options)).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect.soft(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1', 'run-two'])
  expect.soft(h.result.current.busy).toBe(false)
  expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
})

it('R13G-32 a bridge-aborted follow retains an explained lock until authority changes', async () => {
  const h = await setup(), bridge = bridges.at(-1)
  expect(await h.submit()).toEqual(accepted)
  const settlement = bridge.whenSettled(B, 'session-1')
  await act(async () => { h.pendingJob.resolve(local('SIP_R7_ABORTED')); await drain() })
  expect((await settlement).state).toBe('aborted')
  const copy = { ...B }
  const a = { ...h.props.projectSolar, scope: { ...h.props.projectSolar.scope, binding: copy } }
  await h.update({ projectSolar: a, projectCatalog: { ...h.props.projectCatalog, binding: copy } })
  expect(bridges.at(-1)).toBe(bridge)
  expect(a.isCurrent()).toBe(true)
  expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect.soft(h.result.current.busy).toBe(false)
  expect.soft(h.result.current.projectPending).toBeNull()
  expect.soft((await submitWith(h, fresh(h, 'run-two'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect.soft(h.client.submitRun).toHaveBeenCalledOnce()
  await h.install(V, 'foreign')
  h.client.readJob.mockReturnValue(deferred().promise)
  expect(await submitWith(h, fresh(h, 'fresh-owner', 'foreign'), { ...h.options, authorityKey: 'foreign' })).toEqual(accepted)
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
})

it('R13G-33 an initial failed context read explains scope pending without a run', async () => {
  const read = deferred(), h = await setup({ initialContext: read })
  await act(async () => { read.resolve(local('SIP_R7_TRANSPORT_FAILED')); await drain() })
  expect(h.result.current.notice).toBe(projectSolarAppReason('scope_pending'))
  expect(h.result.current.notice).toBe('Project Solar is preparing this drawing version.')
  expect(h.result.current.busy).toBe(false)
  expect(h.client.submitRun).not.toHaveBeenCalled()
})

it('R13G-34 a done transition retains its readable context through a same-owner capture rotation', async () => {
  const h = await setup()
  await h.submit(); await h.finish()
  const done = await h.complete()
  await h.install(W)
  expect(await done.promise).toBe(true)
  const reads = h.client.readContext.mock.calls.length
  const retained = h.result.current.contextView
  h.client.readContext.mockResolvedValueOnce(local('SIP_R7_TRANSPORT_FAILED'))
  await h.install(W)
  expect(h.client.readContext).toHaveBeenCalledTimes(reads)
  expect(h.result.current.contextView).toBe(retained)
  const binding = h.props.projectSolar.scope.binding
  const prepared = { ...fresh(h, 'run-two'), binding }
  const options = { ...h.options, binding }
  expect(h.result.current.notice).toBeNull()
  h.client.readJob.mockReturnValue(deferred().promise)
  expect(await submitWith(h, prepared, options)).toEqual(accepted)
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
})


it('R13G-35 an old settlement cannot overwrite a new owner run', async () => {
  const h = await setup(), bridge = bridges.at(-1), delivery = deferred()
  const observeSettlement = bridge.whenSettled.getMockImplementation()
  let settlement
  // Keep the real bridge settlement, but deliver it after the replacement run starts.
  bridge.whenSettled.mockImplementationOnce((...args) => {
    settlement = observeSettlement(...args)
    return delivery.promise
  })
  expect(await h.submit()).toEqual(accepted)
  await h.install(V, 'foreign')
  const stopped = await settlement
  expect(stopped.state).toBe('stale')
  expect(await submitWith(h, fresh(h, 'new-owner', 'foreign'), { ...h.options, authorityKey: 'foreign' })).toEqual(accepted)
  expect(h.result.current.busy).toBe(true)
  await act(async () => { delivery.resolve(stopped); await drain() })
  expect.soft(h.result.current.busy).toBe(true)
  expect.soft(h.result.current.notice).toBeNull()
  expect.soft(h.result.current.projectPending).toMatchObject({ authorityKey: 'foreign' })
  expect((await submitWith(h, fresh(h, 'blocked', 'foreign'), { ...h.options, authorityKey: 'foreign' })).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1', 'new-owner'])
})

it('R13G-36 settlement is captured before an authority round trip in the next microtask', async () => {
  const h = await setup(), bridge = bridges.at(-1)
  const submit = bridge.onProjectSubmit.getMockImplementation()
  bridge.onProjectSubmit.mockImplementationOnce((...args) => submit(...args).then(answer => {
    // The runtime acceptance continuation runs between these two microtasks. An extra await
    // lets the real bridge discard its association before the runtime can capture it.
    queueMicrotask(() => queueMicrotask(() => {
      h.client.change({ authorityKey: 'foreign' })
      h.client.change({ authorityKey: 'session-1' })
    }))
    return answer
  }))
  expect(await h.submit()).toEqual(accepted)
  expect(h.props.projectSolar.isCurrent()).toBe(true)
  expect(h.client.getSnapshot().authorityKey).toBe('session-1')
  expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect.soft(h.result.current.busy).toBe(false)
  expect.soft(h.result.current.projectPending).toBeNull()
  expect((await submitWith(h, fresh(h, 'blocked'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect(h.client.submitRun).toHaveBeenCalledOnce()
})

it("R13G-37 a late rejection of an earlier run's completion callback never reopens its done transition or releases a later uncertain run", async () => {
  const h = await setup(), tail = deferred()
  let completion
  h.props.completedVersionRef.current = (_, outcome, options) => {
    completion = h.result.current.complete(outcome, options.projectSolarCompletion)
    return completion.then(() => tail.promise)
  }
  await h.submit(); await h.finish()
  await h.install(W)
  expect(await completion).toBe(true)
  const binding = h.props.projectSolar.scope.binding
  const prepared = { ...fresh(h, 'run-two'), binding }
  const options = { ...h.options, binding }
  h.client.readJob.mockResolvedValue(local('SIP_R7_TRANSPORT_FAILED'))
  expect(await submitWith(h, prepared, options)).toEqual(accepted)
  expect(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
  await act(async () => { tail.reject(new Error('late completion failure')); await drain() })
  expect.soft(h.result.current.notice).not.toBe(projectSolarAppReason('output_unreadable'))
  expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
  await h.install(W)
  expect(h.result.current.contextCurrent()).toBe(true)
  expect.soft((await submitWith(h, { ...prepared, idempotencyKey: 'run-three' }, options)).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
  expect.soft(h.client.submitRun).toHaveBeenCalledTimes(2)
  expect.soft(h.client.submitRun.mock.calls.map(([intent]) => intent.idempotencyKey)).toEqual(['intent-1', 'run-two'])
})

it("R13G-38 a completion callback failure for one outcome never touches another outcome's live transition", async () => {
  const h = await setup(), tail = deferred(), rehydrate = deferred()
  let completion
  h.props.completedVersionRef.current = (_, outcome, options) => {
    completion = h.result.current.complete(outcome, options.projectSolarCompletion)
    return completion.then(() => tail.promise)
  }
  await h.submit(); await h.finish()
  await h.install(W)
  expect(await completion).toBe(true)
  h.props.completedVersionRef.current = (_, outcome, options) => {
    completion = h.result.current.complete(outcome, options.projectSolarCompletion)
    return completion
  }
  h.ws.rehydrate.mockReturnValueOnce(rehydrate.promise)
  h.client.readJob.mockResolvedValue(response(job({ input_version_id: W, output_version_id: X,
    result: { output_version_id: X, graph_commit: { output_version_id: X } } })))
  const binding = h.props.projectSolar.scope.binding
  expect(await submitWith(h, { ...fresh(h, 'run-two'), binding }, { ...h.options, binding })).toEqual(accepted)
  expect(h.result.current.projectOutcome?.outcome.state).toBe('succeeded')
  expect(h.result.current.publishedVersionId).toBe(X)
  expect(h.ws.rehydrate).toHaveBeenCalledTimes(2)
  expect(h.ws.getSnapshot().canonicalVersionId).toBe(W)
  expect(h.result.current.busy).toBe(true)
  await act(async () => { tail.reject(new Error('earlier completion failure')); await drain() })
  expect.soft(h.result.current.notice).not.toBe(projectSolarAppReason('output_unreadable'))
  expect.soft(h.result.current.notice).toBeNull()
  expect.soft(h.result.current.busy).toBe(true)
  await act(async () => {
    const value = workspace([row({ version_id: X })])
    h.ws.change({ workspace: value }); rehydrate.resolve(value); await drain()
  })
  await h.install(X)
  expect.soft(await completion).toBe(true)
  expect.soft(h.ws.getSnapshot().canonicalVersionId).toBe(X)
  expect.soft(h.result.current.contextCurrent()).toBe(true)
  expect.soft(h.result.current.notice).toBeNull()
  expect.soft(h.result.current.busy).toBe(false)
  expect(h.client.submitRun).toHaveBeenCalledTimes(2)
})

it('R13G-39 a settlement observer that resolves null leaves an accepted run following', async () => {
  for (const result of [null, undefined]) {
    const h = await setup(), bridge = bridges.at(-1)
    bridge.whenSettled.mockReturnValueOnce(Promise.resolve(result))
    expect(await h.submit()).toEqual(accepted)
    await act(drain)
    expect(bridge.whenSettled).toHaveBeenCalledWith(B, 'session-1')
    expect.soft(h.result.current.busy).toBe(true)
    expect.soft(h.result.current.projectPending).toMatchObject({ binding: B, authorityKey: 'session-1', tool: T })
    expect.soft(h.result.current.notice).toBeNull()
    expect.soft((await submitWith(h, fresh(h, 'blocked'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
    expect.soft(h.client.submitRun).toHaveBeenCalledOnce()
    h.unmount()
  }
})

it('R13G-40 a completion callback failure for its own live transition shows the unreadable sentence', async () => {
  for (const fail of ['reject', 'throw']) {
    const h = await setup(), rehydrate = deferred()
    let completion
    h.ws.rehydrate.mockReturnValueOnce(rehydrate.promise)
    h.props.completedVersionRef.current = (_, outcome, options) => {
      completion = h.result.current.complete(outcome, options.projectSolarCompletion)
      if (fail === 'throw') throw new Error('callback failure')
      return Promise.reject(new Error('callback failure'))
    }
    await h.submit(); await h.finish()
    await act(drain)
    expect.soft(await completion).toBe(false)
    expect.soft(h.result.current.notice).toBe(projectSolarAppReason('output_unreadable'))
    expect.soft(h.result.current.busy).toBe(false)
    expect.soft(h.result.current.publishedVersionId).toBe(W)
    h.unmount()
  }
})

it('R13G-41 a settlement observer that resolves anything but null or undefined marks an accepted run uncertain', async () => {
  for (const result of [false, 0, '']) {
    const h = await setup(), bridge = bridges.at(-1)
    bridge.whenSettled.mockReturnValueOnce(Promise.resolve(result))
    expect(await h.submit()).toEqual(accepted)
    await act(drain)
    expect(bridge.whenSettled).toHaveBeenCalledWith(B, 'session-1')
    expect.soft(h.result.current.busy).toBe(false)
    expect.soft(h.result.current.projectPending).toBeNull()
    expect.soft(h.result.current.notice).toBe(PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
    expect.soft((await submitWith(h, fresh(h, 'blocked'))).reason).toBe('SIP_R13B_RUN_IN_PROGRESS')
    expect.soft(h.client.submitRun).toHaveBeenCalledOnce()
    h.unmount()
  }
})

it('R13G-42 a completion callback failure after the output is selected and before its context read settles shows the unreadable sentence', async () => {
  const h = await setup(), tail = deferred(), held = deferred()
  let completion
  h.props.completedVersionRef.current = (_, outcome, options) => {
    completion = h.result.current.complete(outcome, options.projectSolarCompletion)
    return tail.promise
  }
  await h.submit(); await h.finish()
  expect(h.ws.getSnapshot().canonicalVersionId).toBe(W)
  h.client.readContext.mockImplementation(() => held.promise)
  await h.install(W)
  expect(h.result.current.busy).toBe(true)
  expect(h.result.current.notice).toBeNull()
  await act(async () => { tail.reject(new Error('current completion failure')); await drain() })
  expect.soft(await completion).toBe(false)
  expect.soft(h.result.current.notice).toBe(projectSolarAppReason('output_unreadable'))
  expect.soft(h.result.current.busy).toBe(false)
  expect.soft(h.result.current.publishedVersionId).toBe(W)
  expect(h.client.submitRun).toHaveBeenCalledOnce()
})
