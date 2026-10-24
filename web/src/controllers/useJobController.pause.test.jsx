// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController, { readInflightJob, readPendingRun } from './useJobController.js'

afterEach(cleanup)

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: (key) => { values.delete(key) },
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function renderController({ services, storage, readsPaused = false, onAuthRequired }) {
  return renderHook(({ readsPaused }) => useJobController({
    readsPaused, services, storage, onAuthRequired, pollIntervalMs: 60000,
  }), { initialProps: { readsPaused } })
}

it('masks signed-in job data on the first paused render', async () => {
  const storage = memoryStorage()
  const services = { listJobs: vi.fn(async () => [{ id: 'job-1', status: 'complete' }]) }
  const hook = renderController({ services, storage })
  await waitFor(() => expect(hook.result.current.jobs).toHaveLength(1))
  act(() => {
    hook.result.current.adoptEnvelope({ ok: false, tool: 'count', error: { message: 'x' } }, {
      jobId: 'job-2', toolName: 'count',
    })
    hook.result.current.reportError('boom')
  })
  act(() => { hook.rerender({ readsPaused: true }) })
  expect(hook.result.current.jobs).toEqual([])
  expect(hook.result.current.staleResults).toEqual([])
  for (const field of ['currentJob', 'currentJobId', 'result', 'error', 'inflight']) {
    expect(hook.result.current[field]).toBeNull()
  }
  expect(hook.result.current.running).toBe(false)
})

it('refuses attach, adopt and error publication while paused', async () => {
  const storage = memoryStorage()
  const services = { listJobs: vi.fn(async () => []), attachToJob: vi.fn() }
  const hook = renderController({ services, storage, readsPaused: true })
  await act(async () => {
    expect(await hook.result.current.attachJob('job-3', { persist: true })).toBeNull()
    expect(hook.result.current.adoptEnvelope({ ok: true, tool: 'count' })).toBeNull()
    hook.result.current.reportError('x')
  })
  expect(services.attachToJob).not.toHaveBeenCalled()
  expect(readInflightJob(storage)).toBeNull()
  expect(hook.result.current.error).toBeNull()
})

it('cancels a running attach on pause and keeps its pointer', async () => {
  const storage = memoryStorage()
  let signal
  const services = {
    listJobs: vi.fn(async () => []),
    attachToJob: vi.fn((jobId, options) => {
      signal = options.signal
      return new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      })
    }),
  }
  const hook = renderController({ services, storage })
  let attach
  act(() => { attach = hook.result.current.attachJob('job-4', { toolName: 'count', persist: true }) })
  await act(async () => { hook.rerender({ readsPaused: true }) })
  expect(signal.aborted).toBe(true)
  expect(await attach).toBeNull()
  expect(readInflightJob(storage)?.job_id).toBe('job-4')
})

it('keeps the pointer when an attach fails after a pause', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const services = { listJobs: vi.fn(async () => []), attachToJob: vi.fn(() => pending.promise) }
  const hook = renderController({ services, storage })
  let attach
  act(() => { attach = hook.result.current.attachJob('job-4', { toolName: 'count', persist: true }) })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => {
    pending.reject(new Error('network'))
    expect(await attach).toBeNull()
  })
  expect(readInflightJob(storage)?.job_id).toBe('job-4')
})

it('clears the pointer when the job finishes after a pause', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const services = { listJobs: vi.fn(async () => []), attachToJob: vi.fn(() => pending.promise) }
  const hook = renderController({ services, storage })
  let attach
  act(() => { attach = hook.result.current.attachJob('job-4', { toolName: 'count', persist: true }) })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => {
    pending.resolve({ ok: true, tool: 'count' })
    expect(await attach).toBeNull()
  })
  expect(readInflightJob(storage)).toBeNull()
  expect(hook.result.current.result).toBeNull()
})

it('stores a submit receipt that arrives during a pause', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const services = { listJobs: vi.fn(async () => []) }
  let onSubmit
  const execute = vi.fn((options) => { onSubmit = options.onSubmit; return pending.promise })
  const hook = renderController({ services, storage })
  let run
  act(() => { run = hook.result.current.runJob({ toolName: 'count', execute }) })
  act(() => { hook.rerender({ readsPaused: true }) })
  act(() => { onSubmit('job-7') })
  expect(readInflightJob(storage)?.job_id).toBe('job-7')
  expect(hook.result.current.currentJob).toBeNull()
  await act(async () => { pending.resolve({ ok: true, tool: 'count' }); await run })
})

it("keeps a newer run's pointer when a late receipt lands in the same millisecond", async () => {
  vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
  const storage = memoryStorage()
  const pendingA = deferred()
  const pendingB = deferred()
  const services = {
    listJobs: vi.fn(async () => []),
    getJob: vi.fn(async () => ({ job_id: 'job-B', status: 'running', tool: 'count' })),
    attachToJob: vi.fn(() => pendingB.promise),
  }
  let onSubmitA
  const executeA = vi.fn((options) => { onSubmitA = options.onSubmit; return pendingA.promise })
  const executeB = vi.fn((options) => { options.onSubmit('job-B'); return pendingB.promise })
  const hook = renderController({ services, storage })
  let runA
  let runB
  act(() => { runA = hook.result.current.runJob({ toolName: 'count', execute: executeA }) })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => { hook.rerender({ readsPaused: false }) })
  act(() => { runB = hook.result.current.runJob({ toolName: 'count', execute: executeB }) })
  expect(readInflightJob(storage).job_id).toBe('job-B')
  act(() => { onSubmitA('job-A') })
  expect(readInflightJob(storage).job_id).toBe('job-B')
  await act(async () => {
    pendingA.resolve({ ok: true, tool: 'count' })
    pendingB.resolve({ ok: true, tool: 'count' })
    await Promise.all([runA, runB])
  })
})

it('reattaches a stored receipt when reads resume', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const attached = deferred()
  const services = {
    listJobs: vi.fn(async () => []),
    getJob: vi.fn(async () => ({ job_id: 'job-7', status: 'running', tool: 'count' })),
    attachToJob: vi.fn(() => attached.promise),
  }
  let onSubmit
  const execute = vi.fn((options) => { onSubmit = options.onSubmit; return pending.promise })
  const hook = renderController({ services, storage })
  let run
  act(() => { run = hook.result.current.runJob({ toolName: 'count', execute }) })
  act(() => { hook.rerender({ readsPaused: true }) })
  act(() => { onSubmit('job-7') })
  await act(async () => { hook.rerender({ readsPaused: false }) })
  expect(services.getJob).toHaveBeenCalledWith('job-7')
  await act(async () => {
    pending.resolve({ ok: true, tool: 'count' })
    attached.resolve({ ok: true, tool: 'count' })
    await run
  })
})

it('drops a jobs response that settles across a pause and resume', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const services = { listJobs: vi.fn(async () => []) }
  const hook = renderController({ services, storage })
  await act(async () => {})
  services.listJobs.mockImplementationOnce(() => pending.promise)
  let refresh
  act(() => { refresh = hook.result.current.refreshJobs() })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => { hook.rerender({ readsPaused: false }) })
  await act(async () => {
    pending.resolve([{ id: 'old', status: 'complete' }])
    await refresh
  })
  expect(hook.result.current.jobs.some((row) => row.id === 'old')).toBe(false)
})

it('asks for sign-in instead of running or preparing while paused', async () => {
  const storage = memoryStorage()
  const services = { listJobs: vi.fn(async () => []) }
  const execute = vi.fn()
  const loadTools = vi.fn()
  const onAuthRequired = vi.fn()
  const hook = renderController({ services, storage, readsPaused: true, onAuthRequired })
  await act(async () => {
    expect(await hook.result.current.runJob({
      toolName: 'count', execute, submission: { params: { a: 1 } },
    })).toBeNull()
  })
  expect(execute).not.toHaveBeenCalled()
  expect(onAuthRequired).toHaveBeenCalledWith(true)
  expect(readPendingRun(storage)).toEqual({ tool: 'count', params: { a: 1 }, ts: expect.any(Number) })
  await act(async () => { expect(await hook.result.current.preparePendingRun(loadTools)).toBeNull() })
  expect(loadTools).not.toHaveBeenCalled()
  expect(readPendingRun(storage)).toEqual({ tool: 'count', params: { a: 1 }, ts: expect.any(Number) })
})
