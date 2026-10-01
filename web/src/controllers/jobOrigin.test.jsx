// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController from './useJobController.js'

afterEach(cleanup)
const storage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

it('captures the drawing at submission, before the asynchronous job returns or acknowledges', async () => {
  const pending = deferred(), raw = { ok: true, result: {} }
  const hook = renderHook(({ drawingKey }) => useJobController({ mock: true, storage, drawingKey }), {
    initialProps: { drawingKey: 'engine:first-v1.dxf' },
  })
  let run, callbacks
  act(() => { run = hook.result.current.runJob({ toolName: 'count', execute: (c) => { callbacks = c; return pending.promise } }) })
  hook.rerender({ drawingKey: 'engine:second-v2.dxf' })
  act(() => callbacks.onSubmit('job-1'))
  await act(async () => { pending.resolve(raw); await run })
  expect(hook.result.current.result.origin).toEqual({ drawingKey: 'engine:first-v1.dxf' })
  expect(await run).toEqual({ ...raw, origin: { drawingKey: 'engine:first-v1.dxf' } })
  expect(raw).not.toHaveProperty('origin')
})

it('leaves envelopes unchanged for callers without the optional drawing key', async () => {
  const hook = renderHook(() => useJobController({ mock: true, storage }))
  await act(async () => { await hook.result.current.runJob({ toolName: 'count', execute: async () => ({ ok: true }) }) })
  expect(hook.result.current.result).not.toHaveProperty('origin')
})

it.each([null, ''])('omits origin when the submitted drawing key is %s, even if the index loads before completion', async (drawingKey) => {
  const pending = deferred()
  const hook = renderHook(({ drawingKey }) => useJobController({ mock: true, storage, drawingKey }), {
    initialProps: { drawingKey },
  })
  let run
  act(() => { run = hook.result.current.runJob({ toolName: 'count', execute: () => pending.promise }) })
  hook.rerender({ drawingKey: 'engine:loaded-v1.dxf' })
  await act(async () => { pending.resolve({ ok: true }); await run })
  expect(hook.result.current.result).not.toHaveProperty('origin')
  expect(await run).not.toHaveProperty('origin')
})

it('does not invent an origin for history attachments or adopted results', async () => {
  const services = {
    listJobs: vi.fn(async () => []),
    attachToJob: vi.fn(async () => ({ ok: true })),
    recordToEnvelope: vi.fn(() => ({ ok: true })),
  }
  const hook = renderHook(() => useJobController({ storage, services, drawingKey: 'console:now' }))
  await act(async () => { await hook.result.current.attachJob('history-1') })
  expect(hook.result.current.result).not.toHaveProperty('origin')
  await act(async () => { await hook.result.current.attachJob('history-2', { record: { status: 'complete' } }) })
  expect(hook.result.current.result).not.toHaveProperty('origin')
  act(() => hook.result.current.adoptEnvelope({ ok: true }))
  expect(hook.result.current.result).not.toHaveProperty('origin')
})

it('keeps superseded results as origin-free stale records', async () => {
  const first = deferred()
  const hook = renderHook(() => useJobController({ mock: true, storage, drawingKey: 'console:one' }))
  let run
  act(() => { run = hook.result.current.runJob({ toolName: 'count', execute: ({ onSubmit }) => { onSubmit('old-job'); return first.promise } }) })
  await act(async () => { await hook.result.current.runJob({ toolName: 'count', execute: async () => ({ ok: true }) }) })
  await act(async () => { first.resolve({ ok: true, result: { new_version: 2 } }); await run })
  expect(hook.result.current.staleResults).toEqual([{ job_id: 'old-job', tool: 'count', new_version: 2 }])
  expect(hook.result.current.staleResults[0]).not.toHaveProperty('origin')
})
