// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController, { PENDING_RUN_KEY, PENDING_RUN_LOGOUT_KEY, readInflightJob, readPendingRun, savePendingRun } from './useJobController.js'
import controllerSource from './useJobController.js?raw'
import * as api from '../api.js'
import * as auth from '../auth.js'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

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

function accountToken(sub) {
  return `header.${btoa(JSON.stringify({ iss: 'studio', sub }))}.signature`
}

it('keeps saved inputs across a cross-tab token expiry and restores them for the same account', async () => {
  const storage = memoryStorage()
  const token = accountToken('designer-a')
  storage.setItem('leaf.jwt', token)
  savePendingRun('count', { panels: 8 }, storage)
  const saved = storage.getItem(PENDING_RUN_KEY)
  const hook = renderController({ storage, readsPaused: true, services: {} })
  expect(hook.result.current.pendingRun.params).toEqual({ panels: 8 })
  act(() => {
    storage.removeItem('leaf.jwt')
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', oldValue: token, newValue: null }))
  })
  expect(hook.result.current.pendingRun).toBeNull()
  expect(storage.getItem(PENDING_RUN_KEY)).toBe(saved)
  act(() => {
    storage.setItem('leaf.jwt', token)
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: token }))
  })
  expect(hook.result.current.pendingRun.params).toEqual({ panels: 8 })
})

it('publishes an explicit logout marker and clears pending inputs before logout', () => {
  let logout
  vi.spyOn(auth, 'subscribeBeforeLogout').mockImplementation(callback => { logout = callback; return () => {} })
  const storage = memoryStorage()
  storage.setItem('leaf.jwt', accountToken('designer-a'))
  savePendingRun('count', { panels: 8 }, storage)
  const hook = renderController({ storage, readsPaused: true, services: {} })
  act(() => logout())
  expect(storage.getItem(PENDING_RUN_LOGOUT_KEY)).toEqual(expect.any(String))
  expect(storage.getItem(PENDING_RUN_KEY)).toBeNull()
  expect(hook.result.current.pendingRun).toBeNull()
})

it('clears saved inputs when another tab broadcasts explicit logout', () => {
  const storage = memoryStorage()
  storage.setItem('leaf.jwt', accountToken('designer-a'))
  savePendingRun('count', { panels: 8 }, storage)
  const hook = renderController({ storage, readsPaused: true, services: {} })
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: PENDING_RUN_LOGOUT_KEY, newValue: 'logout-1' }))
  })
  expect(storage.getItem(PENDING_RUN_KEY)).toBeNull()
  expect(hook.result.current.pendingRun).toBeNull()
})

it('keeps saved account inputs hidden when a different account signs in after token expiry', () => {
  const storage = memoryStorage()
  storage.setItem('leaf.jwt', accountToken('designer-a'))
  savePendingRun('count', { panels: 8 }, storage)
  const hook = renderController({ storage, readsPaused: true, services: {} })
  act(() => {
    storage.removeItem('leaf.jwt')
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: null }))
  })
  const token = accountToken('designer-b')
  act(() => {
    storage.setItem('leaf.jwt', token)
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: token }))
  })
  expect(storage.getItem(PENDING_RUN_KEY)).not.toBeNull()
  expect(hook.result.current.pendingRun).toBeNull()
})

it('keeps same-tab pending inputs visible after a 401', async () => {
  let unauthorized
  vi.spyOn(api, 'subscribeUnauthorized').mockImplementation(callback => { unauthorized = callback; return () => {} })
  const storage = memoryStorage()
  const token = accountToken('designer-a')
  storage.setItem('leaf.jwt', token)
  const pending = deferred()
  const hook = renderController({ services: { listJobs: vi.fn(async () => []) }, storage })
  let run
  act(() => {
    run = hook.result.current.runJob({
      toolName: 'count', execute: () => pending.promise, submission: { params: { panels: 8 } },
    })
  })
  act(() => {
    storage.removeItem('leaf.jwt')
    unauthorized()
  })
  await act(async () => {
    pending.reject(Object.assign(new Error('expired'), { status: 401 }))
    await run
  })
  const saved = storage.getItem(PENDING_RUN_KEY)
  expect(hook.result.current.pendingRun.params).toEqual({ panels: 8 })
  expect(JSON.parse(saved).account).toBe('studio:designer-a')
  act(() => {
    storage.setItem('leaf.jwt', token)
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: token }))
  })
  expect(hook.result.current.pendingRun.params).toEqual({ panels: 8 })
})

it('shows an anonymous 401 save only in the same tab', async () => {
  const sameTabStorage = memoryStorage()
  const pending = deferred()
  const sameTab = renderController({ services: { listJobs: vi.fn(async () => []) }, storage: sameTabStorage })
  let run
  act(() => {
    run = sameTab.result.current.runJob({
      toolName: 'count', execute: () => pending.promise, submission: { params: { panels: 6 } },
    })
  })
  await act(async () => {
    pending.reject(Object.assign(new Error('expired'), { status: 401 }))
    await run
  })
  expect(sameTab.result.current.pendingRun.params).toEqual({ panels: 6 })

  const crossTabStorage = memoryStorage()
  savePendingRun('count', { panels: 10 }, crossTabStorage)
  const crossTab = renderController({ services: {}, storage: crossTabStorage, readsPaused: true })
  expect(crossTab.result.current.pendingRun.params).toEqual({ panels: 10 })
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: null }))
  })
  expect(crossTab.result.current.pendingRun).toBeNull()
  expect(crossTabStorage.getItem(PENDING_RUN_KEY)).not.toBeNull()
})

it('routes every non-null pending-run publication through the identity guard', () => {
  const source = controllerSource
  const guardStart = source.indexOf('const publishPendingRun')
  const guardEnd = source.indexOf('\n  useEffect(', guardStart)
  const publications = [...source.matchAll(/setPendingRun\((?!null\))/g)].map(match => match.index)
  expect(guardStart).toBeGreaterThan(-1)
  expect(publications).toHaveLength(1)
  expect(publications[0]).toBeGreaterThan(guardStart)
  expect(publications[0]).toBeLessThan(guardEnd)
})

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

it('ignores a stale sign-in failure from a run once reads resume', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const onAuthRequired = vi.fn()
  const services = {
    listJobs: vi.fn(async () => []),
    getJob: vi.fn(async () => ({ job_id: 'job-1', status: 'running', tool: 'count' })),
    attachToJob: vi.fn(async () => ({ ok: true, tool: 'count' })),
  }
  const hook = renderController({ services, storage, onAuthRequired })
  let run
  act(() => {
    run = hook.result.current.runJob({
      toolName: 'count', execute: () => pending.promise, submission: { params: { a: 1 } },
    })
  })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => { hook.rerender({ readsPaused: false }) })
  await act(async () => {
    pending.reject(Object.assign(new Error('expired'), { status: 401 }))
    await run
  })
  expect(onAuthRequired).not.toHaveBeenCalledWith(true)
  expect(hook.result.current.authRequired).toBe(false)
  expect(readPendingRun(storage)).toBeNull()
})

it("keeps a newer paused run's inputs when an older run fails sign-in", async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const services = { listJobs: vi.fn(async () => []) }
  const hook = renderController({ services, storage })
  let run
  act(() => {
    run = hook.result.current.runJob({
      toolName: 'count', execute: () => pending.promise, submission: { params: { a: 1 } },
    })
  })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => {
    expect(await hook.result.current.runJob({
      toolName: 'count', execute: vi.fn(), submission: { params: { b: 2 } },
    })).toBeNull()
  })
  expect(readPendingRun(storage).params).toEqual({ b: 2 })
  await act(async () => {
    pending.reject(Object.assign(new Error('expired'), { status: 401 }))
    await run
  })
  expect(readPendingRun(storage).params).toEqual({ b: 2 })
})

it('ignores a stale attach sign-in failure once reads resume', async () => {
  const storage = memoryStorage()
  const pending = deferred()
  const onAuthRequired = vi.fn()
  const services = {
    listJobs: vi.fn(async () => []),
    getJob: vi.fn(async () => ({ job_id: 'job-1', status: 'running', tool: 'count' })),
    attachToJob: vi.fn(() => pending.promise),
  }
  const hook = renderController({ services, storage, onAuthRequired })
  let attach
  act(() => { attach = hook.result.current.attachJob('job-1', { toolName: 'count' }) })
  act(() => { hook.rerender({ readsPaused: true }) })
  await act(async () => { hook.rerender({ readsPaused: false }) })
  await act(async () => {
    pending.reject(Object.assign(new Error('expired'), { status: 401 }))
    await attach
  })
  expect(onAuthRequired).not.toHaveBeenCalledWith(true)
  expect(hook.result.current.authRequired).toBe(false)
})
