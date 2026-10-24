// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController, { INFLIGHT_JOB_KEY, readInflightJob } from './useJobController.js'

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

function renderController({ services, storage, onAuthRequired }) {
  return renderHook(() => useJobController({
    readsPaused: false, services, storage, onAuthRequired, pollIntervalMs: 60000,
  }))
}

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
