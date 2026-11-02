// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import useJobController from './useJobController.js'

const pollIntervalMs = 2500
let visibility

beforeEach(() => {
  vi.useFakeTimers()
  visibility = 'visible'
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function renderController(listJobs, options = {}) {
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: (key) => { values.delete(key) },
  }
  return renderHook((props) => useJobController({
    services: { listJobs }, storage, pollIntervalMs, ...props,
  }), { initialProps: options })
}

async function advance(intervals = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(intervals * pollIntervalMs) })
}

function changeVisibility(next) {
  act(() => {
    visibility = next
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

it('waits for a request to settle and then a full interval before polling again', async () => {
  const pending = deferred()
  const listJobs = vi.fn(() => pending.promise)
  const hook = renderController(listJobs)
  expect(listJobs).toHaveBeenCalledTimes(1)
  await advance(10)
  expect(listJobs).toHaveBeenCalledTimes(1)

  const rows = [{ id: 'job-1', status: 'complete' }]
  await act(async () => { pending.resolve(rows); await pending.promise })
  expect(hook.result.current.jobs).toEqual(rows)
  await act(async () => { await vi.advanceTimersByTimeAsync(pollIntervalMs - 1) })
  expect(listJobs).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(listJobs).toHaveBeenCalledTimes(2)
})

it('makes no requests while initially hidden and polls immediately when visible', async () => {
  visibility = 'hidden'
  const listJobs = vi.fn(async () => [])
  renderController(listJobs)
  expect(listJobs).not.toHaveBeenCalled()
  await advance(4)
  expect(listJobs).not.toHaveBeenCalled()

  changeVisibility('visible')
  expect(listJobs).toHaveBeenCalledTimes(1)
  await act(async () => {})
  await advance()
  expect(listJobs).toHaveBeenCalledTimes(2)
})

it('skips due requests after the tab becomes hidden', async () => {
  const listJobs = vi.fn(async () => [])
  renderController(listJobs)
  await act(async () => {})
  expect(listJobs).toHaveBeenCalledTimes(1)
  changeVisibility('hidden')
  await advance(4)
  expect(listJobs).toHaveBeenCalledTimes(1)
  changeVisibility('visible')
  expect(listJobs).toHaveBeenCalledTimes(2)
})

it('does not overlap an in-flight request when visibility changes', async () => {
  const pending = deferred()
  const listJobs = vi.fn(() => pending.promise)
  renderController(listJobs)
  changeVisibility('hidden')
  changeVisibility('visible')
  changeVisibility('hidden')
  changeVisibility('visible')
  await advance(10)
  expect(listJobs).toHaveBeenCalledTimes(1)
  await act(async () => { pending.resolve([]); await pending.promise })
  await advance()
  expect(listJobs).toHaveBeenCalledTimes(2)
})

it('retries an ordinary failure after one interval', async () => {
  const listJobs = vi.fn()
    .mockRejectedValueOnce(new Error('network'))
    .mockResolvedValue([])
  const hook = renderController(listJobs)
  await act(async () => {})
  expect(hook.result.current.authRequired).toBe(false)
  expect(listJobs).toHaveBeenCalledTimes(1)
  await advance()
  expect(listJobs).toHaveBeenCalledTimes(2)
})

it('reports unauthorized and stops polling even when visibility changes', async () => {
  const listJobs = vi.fn().mockRejectedValue(Object.assign(new Error('expired'), { status: 401 }))
  const onAuthRequired = vi.fn()
  const hook = renderController(listJobs, { onAuthRequired })
  await act(async () => {})
  expect(hook.result.current.authRequired).toBe(true)
  expect(onAuthRequired).toHaveBeenCalledWith(true)
  await advance(5)
  changeVisibility('hidden')
  changeVisibility('visible')
  await advance(5)
  expect(listJobs).toHaveBeenCalledTimes(1)
})

it('cancels the pending timeout and removes the visibility listener on unmount', async () => {
  const listJobs = vi.fn(async () => [])
  const hook = renderController(listJobs)
  await act(async () => {})
  expect(vi.getTimerCount()).toBe(1)
  hook.unmount()
  expect(vi.getTimerCount()).toBe(0)
  changeVisibility('hidden')
  changeVisibility('visible')
  await advance(5)
  expect(listJobs).toHaveBeenCalledTimes(1)
})

it.each(['resolve', 'reject'])('ignores a late %s after unmount', async (settle) => {
  const pending = deferred()
  const listJobs = vi.fn(() => pending.promise)
  const onAuthRequired = vi.fn()
  const hook = renderController(listJobs, { onAuthRequired })
  const lastResult = hook.result.current
  hook.unmount()
  await act(async () => {
    if (settle === 'resolve') pending.resolve([{ id: 'late', status: 'complete' }])
    else pending.reject(Object.assign(new Error('expired'), { status: 401 }))
    await pending.promise.catch(() => {})
  })
  expect(hook.result.current).toBe(lastResult)
  expect(onAuthRequired).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
  await advance(5)
  expect(listJobs).toHaveBeenCalledTimes(1)
})
