// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController, { readInflightJob, saveInflightJob } from './useJobController.js'

afterEach(cleanup)

it('clears listed jobs when reads pause', async () => {
  const storage = { getItem: () => null }
  const services = {
    listJobs: vi.fn().mockResolvedValue([{ id: 'job-1', status: 'running' }]),
  }
  const { result, rerender } = renderHook(({ readsPaused }) => useJobController({
    readsPaused, services, storage, pollIntervalMs: 60000,
  }), { initialProps: { readsPaused: false } })
  await waitFor(() => expect(result.current.jobs.length).toBe(1))

  rerender({ readsPaused: true })

  expect(result.current.jobs).toEqual([])
})

it('pauses guest job reads while preserving the in-flight pointer and resumes polling', async () => {
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: (key) => { values.delete(key) },
  }
  saveInflightJob('job-1', 'count', storage)
  const services = {
    listJobs: vi.fn(async () => []),
    getJob: vi.fn(async () => ({ id: 'job-1', status: 'running' })),
  }
  const hook = renderHook(({ readsPaused }) => useJobController({
    readsPaused, services, storage, pollIntervalMs: 60000,
  }), { initialProps: { readsPaused: true } })

  await act(async () => {})
  expect(services.listJobs).not.toHaveBeenCalled()
  expect(services.getJob).not.toHaveBeenCalled()
  expect(readInflightJob(storage)?.job_id).toBe('job-1')

  await act(async () => { hook.rerender({ readsPaused: false }) })
  expect(services.listJobs).toHaveBeenCalled()
})

it('clears the current job and stale results when reads pause', async () => {
  const services = { listJobs: vi.fn(async () => []) }
  const storage = { getItem: () => null }
  const { result, rerender } = renderHook(({ readsPaused }) => useJobController({
    readsPaused, services, storage, pollIntervalMs: 60000,
  }), { initialProps: { readsPaused: false } })
  await act(async () => {})

  act(() => {
    result.current.adoptEnvelope({ ok: true, tool: 'count' }, { jobId: 'job-9', toolName: 'count' })
  })
  expect(result.current.currentJob?.job_id).toBe('job-9')

  act(() => { rerender({ readsPaused: true }) })

  expect(result.current.currentJob).toBeNull()
  expect(result.current.result).toBeNull()
  expect(result.current.staleResults).toEqual([])
})

it('drops a jobs response that settles after reads pause', async () => {
  let resolveJobs
  const deferred = new Promise((resolve) => { resolveJobs = resolve })
  const services = {
    listJobs: vi.fn().mockResolvedValueOnce([]).mockReturnValueOnce(deferred),
  }
  const storage = { getItem: () => null }
  const { result, rerender } = renderHook(({ readsPaused }) => useJobController({
    readsPaused, services, storage, pollIntervalMs: 60000,
  }), { initialProps: { readsPaused: false } })
  await act(async () => {})

  let pending
  act(() => { pending = result.current.refreshJobs() })
  act(() => { rerender({ readsPaused: true }) })
  resolveJobs([{ id: 'job-1', status: 'running' }])
  await act(async () => { await pending })

  expect(result.current.jobs).toEqual([])
})
