// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController, { INFLIGHT_JOB_KEY, PENDING_RUN_KEY, readInflightJob, readPendingRun } from './useJobController.js'

afterEach(() => { cleanup(); sessionStorage.clear() })

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

function renderController({ services, storage, onAuthRequired }) {
  return renderHook(() => useJobController({
    readsPaused: false, services, storage, onAuthRequired, pollIntervalMs: 60000,
  }))
}

it('binds an anonymous paused submission to the first sign-in after the redirect', async () => {
  const storage = memoryStorage()
  const hook = renderHook(() => useJobController({ storage, readsPaused: true }))
  await act(async () => {
    await hook.result.current.runJob({ toolName: 'count', submission: { params: { panels: 6 } }, execute: vi.fn() })
  })
  expect(JSON.parse(storage.getItem(PENDING_RUN_KEY)).account).toBeNull()
  hook.unmount()
  storage.setItem('leaf.jwt', `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'designer-a' }))}.signature`)
  const restored = renderHook(() => useJobController({ storage, readsPaused: true }))
  expect(restored.result.current.pendingRun.params).toEqual({ panels: 6 })
  expect(JSON.parse(storage.getItem(PENDING_RUN_KEY)).account).toBe(JSON.stringify(['studio', 'designer-a', '']))
  restored.unmount()
  const signedIn = renderController({ storage, services: { listJobs: vi.fn(async () => []) } })
  let pending
  act(() => { pending = signedIn.result.current.takePendingRun() })
  expect(pending.params).toEqual({ panels: 6 })
  expect(storage.getItem(PENDING_RUN_KEY)).toBeNull()
})

it('keeps an anonymous run bound to its first account when another account signs in', async () => {
  const storage = memoryStorage()
  const hook = renderHook(() => useJobController({ storage, readsPaused: true }))
  await act(async () => {
    await hook.result.current.runJob({ toolName: 'count', submission: { params: { panels: 6 } }, execute: vi.fn() })
  })
  const token = `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'designer-a' }))}.signature`
  act(() => {
    storage.setItem('leaf.jwt', token)
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: token }))
  })
  expect(JSON.parse(storage.getItem(PENDING_RUN_KEY)).account).toBe(JSON.stringify(['studio', 'designer-a', '']))
  const other = `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'designer-b' }))}.signature`
  act(() => {
    storage.setItem('leaf.jwt', other)
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: other }))
  })
  expect(hook.result.current.pendingRun).toBeNull()
  expect(readPendingRun(storage, Date.now(), JSON.stringify(['studio', 'designer-a', '']))?.params).toEqual({ panels: 6 })
  expect(storage.getItem(PENDING_RUN_KEY)).not.toBeNull()
})

it('does not bind an anonymous submission in a browser session that did not save it', async () => {
  const storage = memoryStorage()
  const hook = renderHook(() => useJobController({ storage, readsPaused: true }))
  await act(async () => {
    await hook.result.current.runJob({ toolName: 'count', submission: { params: { panels: 6 } }, execute: vi.fn() })
  })
  hook.unmount()
  const saved = storage.getItem(PENDING_RUN_KEY)
  sessionStorage.clear()
  storage.setItem('leaf.jwt', `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'designer-b' }))}.signature`)
  const otherSession = renderHook(() => useJobController({ storage, readsPaused: true }))
  expect(otherSession.result.current.pendingRun).toBeNull()
  expect(storage.getItem(PENDING_RUN_KEY)).toBe(saved)
})

it('asks for sign-in when the boot reattach read is refused and nothing newer started', async () => {
  const storage = memoryStorage()
  storage.setItem(INFLIGHT_JOB_KEY, JSON.stringify({ job_id: 'job-boot', tool: 'count', ts: Date.now() }))
  const onAuthRequired = vi.fn()
  const services = {
    getJob: vi.fn(async () => {
      throw Object.assign(new Error('GET /api/jobs/job-boot -> 401'), { status: 401 })
    }),
    listJobs: vi.fn(async () => []),
  }
  renderController({ services, storage, onAuthRequired })
  await act(async () => {})
  await act(async () => {})
  expect(onAuthRequired).toHaveBeenCalledWith(true)
  expect(readInflightJob(storage).job_id).toBe('job-boot')
})

it('does not ask a signed-in user to sign in when a stale reattach read is refused after a newer run', async () => {
  const storage = memoryStorage()
  storage.setItem(INFLIGHT_JOB_KEY, JSON.stringify({ job_id: 'job-boot', tool: 'count', ts: Date.now() }))
  const pending = deferred()
  const onAuthRequired = vi.fn()
  const services = {
    getJob: vi.fn(() => pending.promise),
    listJobs: vi.fn(async () => []),
  }
  const hook = renderController({ services, storage, onAuthRequired })
  await act(async () => {
    await hook.result.current.runJob({
      toolName: 'count',
      execute: (options) => {
        options.onSubmit('job-B')
        return Promise.resolve({ ok: true, tool: 'count' })
      },
    })
  })
  await act(async () => {
    pending.reject(Object.assign(new Error('GET /api/jobs/job-boot -> 401'), { status: 401 }))
  })
  await act(async () => {})
  expect(onAuthRequired).not.toHaveBeenCalledWith(true)
  expect(hook.result.current.authRequired).toBe(false)
})
