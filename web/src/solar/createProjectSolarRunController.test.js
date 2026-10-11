import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { setFlagsFromString } from 'node:v8'
import { runInNewContext } from 'node:vm'
import { createSolarProjectClient } from './solarProjectClient.js'
import { PROJECT_SOLAR_RUN_REASONS } from './solarProjectCompletion.js'
import { createProjectSolarRunController, PROJECT_SOLAR_RUN_POLL_MS, PROJECT_SOLAR_RUN_BUDGET_MS } from './createProjectSolarRunController.js'

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const J = '55555555-5555-5555-5555-555555555555'
const W = '66666666-6666-6666-6666-666666666666'
const X = '77777777-7777-7777-7777-777777777777'
const T = 'solar-settings'
const B = Object.freeze({ kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V })
const authorityKey = 'session-1'
const digest = 'sha256:' + 'a'.repeat(64)
const accepted = { ok: true, status: 202, data: { job_id: J, status: 'submitted' } }
const local = (reason, status = null) => ({ ok: false, status, reason, envelope: null })
const loss = local('SIP_R7_TRANSPORT_FAILED')
const success = { state: 'succeeded', jobId: J, outputVersionId: W, reasonCode: null, message: null }
const interrupted = (state, jobId = J) => ({ state, jobId, outputVersionId: null, reasonCode: null, message: null })
const unknown = (jobId = J) => ({ ...interrupted('outcome_unknown', jobId), message: PROJECT_SOLAR_RUN_REASONS.outcome_unknown })
const unreadable = { ...interrupted('unreadable'), message: PROJECT_SOLAR_RUN_REASONS.unreadable_output }
const job = (changes = {}) => ({ job_id: J, org_id: O, project_id: P, input_version_id: V,
  tool_name: T, execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: D },
  status: 'complete', output_version_id: W, error: null, result: { output_version_id: W,
    graph_commit: { output_version_id: W } }, ...changes })
const readResponse = (data = job(), status = 200) => ({ ok: true, status, data })
function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
async function drain() { for (let i = 0; i < 40; i += 1) await Promise.resolve() }
const cleanups = []
function setup({ isCurrent = () => true, signal, onOutcome = vi.fn() } = {}) {
  let snapshot = { binding: B, authorityKey }
  const listeners = new Set()
  const projectClient = {
    submitRun: vi.fn(async () => accepted),
    readJob: vi.fn(async () => readResponse()),
    getSnapshot: vi.fn(() => snapshot),
    subscribe: vi.fn((listener) => { listeners.add(listener); return () => listeners.delete(listener) }),
  }
  const controller = createProjectSolarRunController({ projectClient, onOutcome })
  cleanups.push(() => controller.dispose())
  const preparedIntent = Object.freeze({ binding: B, authorityKey, tool: T,
    params: Object.freeze({ drawing_id: D, expected_rev: 0 }), catalogDigest: digest, idempotencyKey: 'request-1' })
  const options = { binding: B, authorityKey, signal, isCurrent }
  const change = (binding, key = authorityKey) => {
    snapshot = { binding, authorityKey: key }
    for (const listener of [...listeners]) listener()
  }
  return { controller, projectClient, onOutcome, preparedIntent, options, listeners, change,
    start: () => controller.onProjectSubmit(preparedIntent, options),
    settled: () => controller.whenSettled(B, authorityKey) }
}
function realClient(fetchImpl) {
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(B, authorityKey)
  cleanups.push(() => client.dispose())
  return client
}
const http = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data })
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(0) })
afterEach(async () => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  await drain()
  vi.useRealTimers()
})

describe('bounded project Solar run controller', () => {
  it('R13B-17 forwards the identical prepared intent', async () => {
    const run = setup()
    run.projectClient.submitRun.mockResolvedValueOnce({ ...accepted,
      data: { ...accepted.data, extra: 'private-submit-sentinel' }, envelope: { extra: 'private-submit-sentinel' } })
    expect(await run.start()).toEqual(accepted)
    expect(run.projectClient.submitRun.mock.calls[0][0]).toBe(run.preparedIntent)
    expect(Object.keys(await run.start()).sort()).toEqual(['envelope', 'ok', 'reason', 'status'])
    await drain()
    expect(await run.settled()).toEqual(success)
    const invalid = setup()
    expect(await invalid.controller.onProjectSubmit({ ...invalid.preparedIntent, authorityKey: 'foreign' }, invalid.options))
      .toEqual(local('SIP_R7_REQUEST_INVALID'))
    expect(invalid.projectClient.submitRun).not.toHaveBeenCalled()
    expect(await invalid.settled()).toBeNull()
    expect(() => createProjectSolarRunController({ projectClient: {} })).toThrow(TypeError)
  })
  it('R13B-18 follows acceptance to one completion', async () => {
    const run = setup()
    run.projectClient.readJob.mockResolvedValueOnce(readResponse(job({ status: 'submitted' })))
      .mockResolvedValueOnce(readResponse(job({ status: 'running' }))).mockResolvedValueOnce(readResponse())
    const submitted = run.start()
    const settled = run.settled()
    expect(await submitted).toEqual(accepted)
    await drain()
    expect(run.onOutcome).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    expect(await settled).toEqual(success)
    expect(run.onOutcome).toHaveBeenCalledOnce()
    expect(run.onOutcome.mock.calls[0][0]).toEqual(success)
    const scope = run.onOutcome.mock.calls[0][1]
    expect(scope.binding).toEqual(B)
    expect(scope.authorityKey).toBe(authorityKey)
    expect(scope.tool).toBe(T)
    expect(scope.isCurrent()).toBe(true)
    for (const [id, options] of run.projectClient.readJob.mock.calls) {
      expect(id).toBe(J)
      expect(options.tool).toBe(T)
      expect(options.signal).toBe(scope.signal)
    }
    expect(run.projectClient.readJob).toHaveBeenCalledTimes(3)
  })
  it('R13B-19 treats acceptance as pending', async () => {
    const run = setup()
    const read = deferred()
    run.projectClient.readJob.mockReturnValueOnce(read.promise)
    expect(await run.start()).toEqual(accepted)
    await drain()
    expect(run.onOutcome).not.toHaveBeenCalled()
    expect(await run.start()).toEqual(local('SIP_R13B_RUN_IN_PROGRESS'))
    read.resolve(readResponse())
    expect(await run.settled()).toEqual(success)
  })
  it('R13B-20 suppresses a second in-flight submit', async () => {
    const run = setup()
    const submit = deferred()
    run.projectClient.submitRun.mockReturnValueOnce(submit.promise)
    const first = run.start()
    const settled = run.settled()
    const second = run.start()
    expect(run.settled()).toBe(settled)
    expect(await second).toEqual(local('SIP_R13B_RUN_IN_PROGRESS'))
    expect(run.projectClient.submitRun).toHaveBeenCalledOnce()
    expect(await run.controller.whenSettled(B, 'foreign')).toBeNull()
    expect(await run.controller.onProjectSubmit(null, run.options)).toEqual(local('SIP_R7_REQUEST_INVALID'))
    expect(run.settled()).toBe(settled)
    submit.resolve(accepted)
    expect(await first).toEqual(accepted)
    expect(await settled).toEqual(success)
  })
  it('R13B-21 aborts before dispatch', async () => {
    const abort = new AbortController()
    abort.abort()
    const run = setup({ signal: abort.signal })
    expect(await run.start()).toEqual(local('SIP_R7_ABORTED'))
    expect(run.projectClient.submitRun).not.toHaveBeenCalled()
    expect(run.projectClient.readJob).not.toHaveBeenCalled()
    expect(run.onOutcome).not.toHaveBeenCalled()
    expect(await run.settled()).toBeNull()
  })
  it('R13B-22 aborts mid-follow', async () => {
    const abort = new AbortController()
    const run = setup({ signal: abort.signal })
    const read = deferred()
    run.projectClient.readJob.mockReturnValueOnce(read.promise)
    await run.start()
    const settled = run.settled()
    await drain()
    abort.abort()
    expect(await settled).toEqual(interrupted('aborted'))
    read.resolve(readResponse())
    await drain()
    expect(run.onOutcome).not.toHaveBeenCalled()
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('R13B-23 rejects a binding change mid-follow', async () => {
    const run = setup()
    const read = deferred()
    run.projectClient.readJob.mockReturnValueOnce(read.promise)
    await run.start()
    const settled = run.settled()
    await drain()
    run.change({ ...B, input_version_id: X })
    expect(await settled).toEqual(interrupted('stale'))
    read.resolve(readResponse())
    await drain()
    expect(run.onOutcome).not.toHaveBeenCalled()
    expect(await run.settled()).toBeNull()
  })
  it('R13B-24 rejects authority change and ABA', async () => {
    for (const mode of ['authority', 'binding']) {
      const run = setup()
      const read = deferred()
      run.projectClient.readJob.mockReturnValueOnce(read.promise)
      await run.start()
      const settled = run.settled()
      await drain()
      if (mode === 'authority') run.change(B, 'session-2')
      else run.change({ ...B, input_version_id: X })
      run.change(B)
      expect(await settled).toEqual(interrupted('stale'))
      expect(await run.settled()).toBeNull()
      read.resolve(readResponse())
      await drain()
      expect(run.onOutcome).not.toHaveBeenCalled()
      expect(await run.start()).toEqual(accepted)
      expect(await run.settled()).toEqual(success)
      expect(run.onOutcome).toHaveBeenCalledOnce()
      run.controller.dispose()
    }
  })
  it('R13B-25 checks currentness around each await', async () => {
    for (const stage of ['submit', 'read', 'recovery', 'delay']) {
      let current = true
      const run = setup({ isCurrent: () => current })
      const waiting = deferred()
      if (stage === 'submit') run.projectClient.submitRun.mockReturnValueOnce(waiting.promise)
      if (stage === 'read') run.projectClient.readJob.mockReturnValueOnce(waiting.promise)
      if (stage === 'recovery') run.projectClient.submitRun.mockResolvedValueOnce(loss).mockReturnValueOnce(waiting.promise)
      if (stage === 'delay') run.projectClient.readJob.mockResolvedValueOnce(readResponse(job({ status: 'running' })))
      const submitted = run.start()
      const settled = run.settled()
      await drain()
      const calls = run.projectClient.submitRun.mock.calls.length + run.projectClient.readJob.mock.calls.length
      current = false
      if (stage !== 'delay') waiting.resolve(stage === 'read' ? readResponse() : accepted)
      await vi.advanceTimersByTimeAsync(1000)
      expect(await settled).toEqual(interrupted('stale', stage === 'submit' || stage === 'recovery' ? null : J))
      waiting.resolve(stage === 'read' ? readResponse() : accepted)
      await submitted
      await drain()
      expect(run.projectClient.submitRun.mock.calls.length + run.projectClient.readJob.mock.calls.length).toBe(calls)
      expect(run.onOutcome).not.toHaveBeenCalled()
      run.controller.dispose()
    }
    const throwing = setup({ isCurrent: () => { throw new Error('private callback exception') } })
    expect(await throwing.start()).toEqual(local('SIP_R7_SCOPE_STALE'))
    expect(throwing.projectClient.submitRun).not.toHaveBeenCalled()
  })
  it('R13B-26 recovers a lost submit response once', async () => {
    for (const kind of ['lost', 'rejected']) {
      const run = setup()
      if (kind === 'lost') run.projectClient.submitRun.mockResolvedValueOnce(loss)
      else run.projectClient.submitRun.mockRejectedValueOnce(new Error('private transport exception'))
      expect(await run.start()).toEqual(accepted)
      expect(await run.settled()).toEqual(success)
      expect(run.projectClient.submitRun).toHaveBeenCalledTimes(2)
      for (const [intent] of run.projectClient.submitRun.mock.calls) {
        expect(intent).toBe(run.preparedIntent)
        expect(intent.idempotencyKey).toBe('request-1')
      }
      expect(run.projectClient.readJob).toHaveBeenCalledOnce()
      expect(run.onOutcome).toHaveBeenCalledOnce()
      run.controller.dispose()
    }
  })
  it('R13B-27 reports unknown when submit recovery cannot identify a job', async () => {
    for (const recovery of [loss, local('EQUIPMENT_ASSIGNMENT_REQUIRED', 409)]) {
      const run = setup()
      run.projectClient.submitRun.mockResolvedValueOnce(loss).mockResolvedValueOnce(recovery)
      expect(await run.start()).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
      expect(await run.settled()).toEqual(unknown(null))
      expect(run.projectClient.submitRun).toHaveBeenCalledTimes(2)
      expect(run.projectClient.readJob).not.toHaveBeenCalled()
      expect(run.onOutcome.mock.calls[0][0]).toEqual(unknown(null))
      run.controller.dispose()
    }
  })
  it('R13B-28 reattaches once using the known job ID', async () => {
    const run = setup()
    run.projectClient.readJob.mockResolvedValueOnce(loss).mockResolvedValueOnce(readResponse())
    expect(await run.start()).toEqual(accepted)
    expect(await run.settled()).toEqual(success)
    expect(run.projectClient.submitRun).toHaveBeenCalledOnce()
    expect(run.projectClient.readJob).toHaveBeenCalledTimes(2)
    expect(run.projectClient.readJob.mock.calls.map(([id]) => id)).toEqual([J, J])
    expect(run.onOutcome).toHaveBeenCalledOnce()
  })
  it('R13B-29 retains the ID when reattachment fails', async () => {
    const run = setup()
    run.projectClient.readJob.mockResolvedValueOnce(loss).mockResolvedValueOnce(local('HTTP_ERROR', 404))
    await run.start()
    expect(await run.settled()).toEqual(unknown())
    expect(run.projectClient.readJob).toHaveBeenCalledTimes(2)
    expect(run.projectClient.submitRun).toHaveBeenCalledOnce()
    expect(run.onOutcome.mock.calls[0][0]).toEqual(unknown())
  })
  it('R13B-30 shares one recovery allowance across the run', async () => {
    const run = setup()
    run.projectClient.submitRun.mockResolvedValueOnce(loss).mockResolvedValueOnce(accepted)
    run.projectClient.readJob.mockResolvedValueOnce(loss)
    await run.start()
    expect(await run.settled()).toEqual(unknown())
    expect(run.projectClient.submitRun).toHaveBeenCalledTimes(2)
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
  })
  it('R13B-31 uses the exact poll interval without overlap', async () => {
    expect(PROJECT_SOLAR_RUN_POLL_MS).toBe(1000)
    const run = setup()
    const second = deferred()
    run.projectClient.readJob.mockResolvedValueOnce(readResponse(job({ status: 'submitted' })))
      .mockReturnValueOnce(second.promise)
    await run.start()
    await drain()
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(999)
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(run.projectClient.readJob).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(3000)
    expect(run.projectClient.readJob).toHaveBeenCalledTimes(2)
    second.resolve(readResponse())
    expect(await run.settled()).toEqual(success)
  })
  it('R13B-32 enforces the total budget', async () => {
    expect(PROJECT_SOLAR_RUN_BUDGET_MS).toBe(120000)
    for (const stage of ['follow', 'submit-recovery']) {
      const run = setup()
      const waiting = deferred()
      if (stage === 'follow') run.projectClient.readJob.mockReturnValueOnce(waiting.promise)
      else run.projectClient.submitRun.mockResolvedValueOnce(loss).mockReturnValueOnce(waiting.promise)
      const submitted = run.start()
      const settled = run.settled()
      const observed = vi.fn()
      settled.then(observed)
      await drain()
      await vi.advanceTimersByTimeAsync(119999)
      expect(observed).not.toHaveBeenCalled()
      expect(run.onOutcome).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1)
      expect(await settled).toEqual(unknown(stage === 'follow' ? J : null))
      expect(await submitted).toEqual(stage === 'follow' ? accepted : local('SIP_R13B_OUTCOME_UNKNOWN'))
      expect(run.onOutcome).toHaveBeenCalledOnce()
      waiting.resolve(stage === 'follow' ? readResponse() : accepted)
      await drain()
      expect(run.onOutcome).toHaveBeenCalledOnce()
      expect(vi.getTimerCount()).toBe(0)
      run.controller.dispose()
    }
    const boundary = setup()
    boundary.projectClient.readJob.mockImplementationOnce(() => new Promise((done) => setTimeout(() => done(readResponse()), 120000)))
    await boundary.start()
    const settled = boundary.settled()
    await drain()
    await vi.advanceTimersByTimeAsync(120000)
    expect(await settled).toEqual(unknown())
  })
  it('R13B-33 distinguishes rejection from uncertainty', async () => {
    const refusal = setup()
    refusal.projectClient.submitRun.mockResolvedValueOnce(local('EQUIPMENT_ASSIGNMENT_REQUIRED', 409))
    expect(await refusal.start()).toEqual(local('equipment_assignment_required', 409))
    expect(await refusal.settled()).toEqual({ ...interrupted('refused', null),
      reasonCode: 'equipment_assignment_required', message: 'Assign inverter equipment first' })
    expect(refusal.projectClient.readJob).not.toHaveBeenCalled()
    for (const response of [{ ...accepted, status: 200 }, { ...accepted, data: { job_id: J } },
      local('HTTP_ERROR', 500), { ...accepted, ok: 'true' }, { ...accepted, data: { job_id: J.toUpperCase() + 'A', status: 'submitted' } }]) {
      const run = setup()
      run.projectClient.submitRun.mockResolvedValueOnce(response)
      expect(await run.start()).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
      expect(await run.settled()).toEqual(unknown(null))
      expect(run.projectClient.readJob).not.toHaveBeenCalled()
      run.controller.dispose()
    }
    const invalid = setup()
    invalid.projectClient.submitRun.mockResolvedValueOnce(local('SIP_R7_REQUEST_INVALID'))
    expect(await invalid.start()).toEqual(local('SIP_R7_REQUEST_INVALID'))
    expect((await invalid.settled()).state).toBe('refused')
  })
  it('R13B-34 handles malformed job reads safely', async () => {
    let getterCalls = 0
    const accessor = { status: 200, data: job() }
    Object.defineProperty(accessor, 'ok', { get() { getterCalls += 1; return true } })
    const proxy = new Proxy({}, { getPrototypeOf() { throw new Error('private proxy exception') } })
    for (const response of [readResponse(null), readResponse(job({ result: {} })),
      local('SIP_R7_RESPONSE_INVALID'), accessor, proxy, readResponse(job(), 201)]) {
      const run = setup()
      run.projectClient.readJob.mockResolvedValueOnce(response)
      await run.start()
      expect(await run.settled()).toEqual(unreadable)
      expect(run.projectClient.readJob).toHaveBeenCalledOnce()
      expect(run.onOutcome.mock.calls[0][0]).toEqual(unreadable)
      run.controller.dispose()
    }
    expect(getterCalls).toBe(0)
  })
  it('R13B-35 handles flat and wrapped records without ambiguity', async () => {
    for (const data of [job(), { job: job() }, { job: job(), job_id: J }, { job: null },
      { job: [] }, { job: job(), job_id: X }, { job: job(), input_version_id: X }]) {
      const run = setup()
      run.projectClient.readJob.mockResolvedValueOnce(readResponse(data))
      await run.start()
      expect(await run.settled()).toEqual(data.job === null || Array.isArray(data.job) ||
        data.job_id === X || data.input_version_id === X ? unreadable : success)
      run.controller.dispose()
    }
    let calls = 0
    const data = {}
    Object.defineProperty(data, 'job', { get() { calls += 1; return job() } })
    const run = setup()
    run.projectClient.readJob.mockResolvedValueOnce(readResponse(data))
    await run.start()
    expect(await run.settled()).toEqual(unreadable)
    expect(calls).toBe(0)
  })
  it('R13B-36 preserves the terminal failure sentence', async () => {
    const run = setup()
    const sentinel = 'private-server-sentinel'
    run.projectClient.readJob.mockResolvedValueOnce(readResponse(job({ status: 'failed', result: null,
      output_version_id: null, progress: sentinel, error: { error_code: 'GRAPH_JOB_FAILED',
        reason_code: 'EQUIPMENT_ASSIGNMENT_REQUIRED', retryable: true, message: sentinel } })))
    await run.start()
    const result = await run.settled()
    expect(result).toEqual({ ...interrupted('failed'), reasonCode: 'equipment_assignment_required',
      message: 'Assign inverter equipment first' })
    expect(run.onOutcome).toHaveBeenCalledOnce()
    expect(run.onOutcome.mock.calls[0][0]).toEqual(result)
    expect(JSON.stringify(result)).not.toContain(sentinel)
    expect(Object.keys(result).sort()).toEqual(['jobId', 'message', 'outputVersionId', 'reasonCode', 'state'])
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
  })
  it('R13B-37 cleans up and isolates observer failure', async () => {
    const abort = new AbortController()
    const add = vi.spyOn(abort.signal, 'addEventListener')
    const remove = vi.spyOn(abort.signal, 'removeEventListener')
    const onOutcome = vi.fn(() => { throw new Error('private observer exception') })
    const run = setup({ onOutcome, signal: abort.signal })
    await run.start()
    expect(await run.settled()).toEqual(success)
    expect(run.projectClient.submitRun).toHaveBeenCalledOnce()
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
    expect(onOutcome).toHaveBeenCalledOnce()
    expect(run.listeners.size).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
    expect(remove.mock.calls[0][1]).toBe(add.mock.calls[0][1])
    expect(await run.start()).toEqual(accepted)
    expect(await run.settled()).toEqual(success)
    expect(onOutcome).toHaveBeenCalledTimes(2)
    expect(run.listeners.size).toBe(1)
    run.controller.dispose()
    expect(run.listeners.size).toBe(0)
  })
  it('R13B-38 disposes without late publication', async () => {
    for (const lateKind of ['resolve', 'reject']) {
      const run = setup()
      const read = deferred()
      run.projectClient.readJob.mockReturnValueOnce(read.promise)
      await run.start()
      const settled = run.settled()
      await drain()
      run.controller.dispose()
      run.controller.dispose()
      expect(await settled).toEqual(interrupted('aborted'))
      if (lateKind === 'resolve') read.resolve(readResponse())
      else read.reject(new Error('private late exception'))
      await drain()
      expect(run.onOutcome).not.toHaveBeenCalled()
      expect(await run.start()).toEqual(local('SIP_R7_ABORTED'))
      expect(await run.settled()).toBeNull()
      expect(run.listeners.size).toBe(0)
      expect(vi.getTimerCount()).toBe(0)
    }
  })
  it('R13B-39 keeps the modules unmounted and independent', async () => {
    for (const name of ['solarProjectCompletion.js', 'createProjectSolarRunController.js']) {
      const source = readFileSync(resolve(process.cwd(), 'src/solar', name), 'utf8')
      for (const forbidden of ['solarProjectAppModel', 'useProjectSolarScope', 'createProjectSolarCatalogController',
        'App.jsx', 'fetch(', 'readSolar(', 'downloadArtifact(', 'localStorage', 'sessionStorage',
        'error.message', 'readContext(', 'headVersion', 'selectVersion(', 'rehydrate']) {
        expect(source).not.toContain(forbidden)
      }
    }
    const run = setup()
    await run.start()
    expect(await run.settled()).toEqual(success)
    expect(run.projectClient.submitRun).toHaveBeenCalledOnce()
    expect(run.projectClient.readJob).toHaveBeenCalledOnce()
  })
  it('R13B-46 keeps the original deadline through submit recovery', async () => {
    const run = setup()
    const recovery = deferred()
    run.projectClient.submitRun
      .mockImplementationOnce(() => new Promise((done) => setTimeout(() => done(loss), 60000)))
      .mockReturnValueOnce(recovery.promise)
    const submitted = run.start()
    const observed = vi.fn()
    run.settled().then(observed)
    await drain()
    await vi.advanceTimersByTimeAsync(60000)
    expect(run.projectClient.submitRun).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(59999)
    expect(observed).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(observed).toHaveBeenCalledOnce()
    expect(observed.mock.calls[0][0]).toEqual(unknown(null))
    expect(await submitted).toEqual(local('SIP_R13B_OUTCOME_UNKNOWN'))
    recovery.resolve(accepted)
    await drain()
    expect(run.projectClient.readJob).not.toHaveBeenCalled()
    expect(run.onOutcome).toHaveBeenCalledOnce()
    expect(run.onOutcome.mock.calls[0][0]).toEqual(unknown(null))
    expect(vi.getTimerCount()).toBe(0)
  })
  it('R13B-47 removes only the settled run from the active set', async () => {
    const run = setup()
    const oldRead = deferred()
    const newRead = deferred()
    run.projectClient.readJob.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(newRead.promise)
    await run.start()
    const oldSettled = run.settled()
    await drain()
    run.change({ ...B, input_version_id: X })
    run.change(B)
    const replacement = run.start()
    await drain()
    expect(await oldSettled).toEqual(interrupted('stale'))
    expect(await replacement).toEqual(accepted)
    expect(run.projectClient.readJob).toHaveBeenCalledTimes(2)
    expect(await run.start()).toEqual(local('SIP_R13B_RUN_IN_PROGRESS'))
    expect(run.projectClient.submitRun).toHaveBeenCalledTimes(2)
    const replacementSettled = run.settled()
    newRead.resolve(readResponse())
    expect(await replacementSettled).toEqual(success)
    oldRead.resolve(readResponse())
    await drain()
    expect(run.onOutcome).toHaveBeenCalledOnce()
  })
  it('R13B-48 keeps a published guard false once its scope changed', async () => {
    for (const mode of ['binding', 'authority']) {
      const run = setup()
      await run.start()
      expect(await run.settled()).toEqual(success)
      const scope = run.onOutcome.mock.calls[0][1]
      run.change(B)
      expect(scope.isCurrent()).toBe(true)
      if (mode === 'authority') run.change(B, 'session-2')
      else run.change({ ...B, input_version_id: X })
      expect(scope.isCurrent()).toBe(false)
      run.change(B)
      expect(scope.isCurrent()).toBe(false)
      expect(await run.start()).toEqual(accepted)
      expect(await run.settled()).toEqual(success)
      expect(run.onOutcome.mock.calls[1][1].isCurrent()).toBe(true)
      expect(scope.isCurrent()).toBe(false)
      run.controller.dispose()
      expect(run.onOutcome.mock.calls[1][1].isCurrent()).toBe(false)
    }
  })
  it('R13B-51 keeps a published guard false after a binding changed in place', async () => {
    const run = setup()
    const mutable = { ...B }
    run.change(mutable)
    await run.start()
    expect(await run.settled()).toEqual(success)
    const scope = run.onOutcome.mock.calls[0][1]
    expect(scope.isCurrent()).toBe(true)
    mutable.input_version_id = X
    run.change(mutable)
    expect(scope.isCurrent()).toBe(false)
    mutable.input_version_id = V
    run.change(mutable)
    expect(scope.isCurrent()).toBe(false)
  })
  it('R13B-49 holds one scope subscription for the controller life', async () => {
    const run = setup()
    expect(run.projectClient.subscribe).not.toHaveBeenCalled()
    for (let index = 0; index < 3; index += 1) {
      await run.start()
      expect(await run.settled()).toEqual(success)
    }
    expect(run.projectClient.subscribe).toHaveBeenCalledOnce()
    expect(run.listeners.size).toBe(1)
    run.controller.dispose()
    run.controller.dispose()
    expect(run.listeners.size).toBe(0)
    const broken = setup()
    broken.projectClient.subscribe.mockReturnValueOnce(undefined)
    expect(() => broken.start()).toThrow(TypeError)
    expect(broken.projectClient.submitRun).not.toHaveBeenCalled()
    expect(await broken.settled()).toBeNull()
    expect(await broken.start()).toEqual(accepted)
    expect(await broken.settled()).toEqual(success)
  })
  it('R13B-50 keeps no raw envelope after settlement', async () => {
    vi.useRealTimers()
    setFlagsFromString('--expose-gc')
    const collect = runInNewContext('gc')
    expect(typeof collect).toBe('function')
    const refs = []
    const projectClient = {
      submitRun: async () => {
        const envelope = { ok: true, status: 202, data: { job_id: J, status: 'submitted' }, sentinel: { bytes: 'x'.repeat(4096) } }
        refs.push(new WeakRef(envelope))
        return envelope
      },
      readJob: async () => {
        const envelope = { ok: true, status: 200, data: job(), sentinel: { bytes: 'y'.repeat(4096) } }
        refs.push(new WeakRef(envelope))
        return envelope
      },
      getSnapshot: () => ({ binding: B, authorityKey }),
      subscribe: () => () => {},
    }
    const controller = createProjectSolarRunController({ projectClient })
    cleanups.push(() => controller.dispose())
    const intent = Object.freeze({ binding: B, authorityKey, tool: T,
      params: Object.freeze({ drawing_id: D, expected_rev: 0 }), catalogDigest: digest, idempotencyKey: 'request-1' })
    expect(await controller.onProjectSubmit(intent, { binding: B, authorityKey, isCurrent: () => true })).toEqual(accepted)
    expect(await controller.whenSettled(B, authorityKey)).toEqual(success)
    expect(refs).toHaveLength(2)
    for (let cycle = 0; cycle < 20 && refs.some((ref) => ref.deref() !== undefined); cycle += 1) {
      await new Promise((done) => setTimeout(done, 0))
      collect()
    }
    expect(refs.map((ref) => ref.deref() === undefined)).toEqual([true, true])
  })
  it('R13B-41 proves R7 needs the explicit 202 guard', async () => {
    const body = { job_id: J, status: 'submitted' }
    const client = realClient(vi.fn(async () => http(body, 200)))
    const intent = client.prepareRun({ tool: T, params: {}, catalogDigest: digest, idempotencyKey: 'request-1' })
    expect(await client.submitRun(intent)).toEqual({ ok: true, status: 200, data: body })
  })
  it('R13B-42 proves R7 leaves output validation to B', async () => {
    const fetchImpl = vi.fn()
    const client = realClient(fetchImpl)
    const missing = job(); delete missing.output_version_id; delete missing.result
    for (const data of [job(), missing, job({ output_version_id: 'head', result: { output_version_id: 'head' } })]) {
      fetchImpl.mockResolvedValueOnce(http(data))
      expect(await client.readJob(J, { tool: T })).toEqual({ ok: true, status: 200, data })
    }
  })
  it('R13B-43 proves a lost submit has no job ID', async () => {
    const client = realClient(vi.fn(async () => { throw new Error('private transport exception') }))
    const intent = client.prepareRun({ tool: T, params: {}, catalogDigest: digest, idempotencyKey: 'request-1' })
    expect(await client.submitRun(intent)).toEqual(loss)
  })
  it('R13B-44 proves R7 rejects a foreign input version', async () => {
    const client = realClient(vi.fn(async () => http(job({ input_version_id: X }))))
    expect(await client.readJob(J, { tool: T })).toEqual(local('SIP_R7_RESPONSE_INVALID'))
  })
})
