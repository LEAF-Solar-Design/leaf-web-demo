// @vitest-environment jsdom
import { act, cleanup, render, renderHook, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ResultPanel from '../components/ResultPanel.jsx'
import useJobController, { readInflightJob } from './useJobController.js'

afterEach(cleanup)

const TOOL = 'count-panels'
const initialScope = { resetKey: 'project-a', drawingKey: 'project-a:drawing-a:v1' }
const goodResult = {
  ok: true, tool: TOOL, version: 1,
  result: { counts: { Panels: 12 }, total: 12 },
  overlay: { highlight_handles: ['A1'] },
  timing_ms: 75, cost: { engine_seconds: 1, usd_est: 0.02 },
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function mountController(options = {}) {
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: (key) => { values.delete(key) },
  }
  const services = { listJobs: vi.fn(async () => []), ...options.services }
  const onNotice = vi.fn()
  const hook = renderHook((scope) => useJobController({
    storage, services, onNotice, pollIntervalMs: 60000, ...scope,
  }), { initialProps: initialScope })
  return { ...hook, storage, services, onNotice }
}

async function succeed(hook, envelope = goodResult) {
  await act(async () => {
    await hook.result.current.runJob({ toolName: envelope.tool, execute: async () => envelope })
  })
  return hook.result.current.result
}

function startRun(hook, { toolName = TOOL, jobId = null } = {}) {
  const pending = deferred()
  let run
  act(() => {
    run = hook.result.current.runJob({
      toolName,
      execute: ({ onSubmit, onStatus }) => {
        if (jobId) onSubmit(jobId)
        onStatus({ status: 'running', progress: 'Counting panels' })
        return pending.promise
      },
    })
  })
  return { ...pending, run }
}

async function settle(attempt, output, rejects = false) {
  let returned
  await act(async () => {
    if (rejects) attempt.reject(output)
    else attempt.resolve(output)
    returned = await attempt.run
  })
  return returned
}

it.each([
  ['validation refusal', { ok: false, error: 'Choose a positive panel count.' }, false, 'Choose a positive panel count.'],
  ['failed job', { ok: false, error: { error_code: 'COMPUTE_FAILED', message: 'Panel counting failed.', retryable: true }, timing_ms: 20 }, false, 'Panel counting failed.'],
  ['transport rejection', new Error('The connection was interrupted.'), true, 'The connection was interrupted.'],
])('retains the last success while pending and shows the latest %s beside it', async (_name, failure, rejects, message) => {
  const hook = mountController()
  const previous = await succeed(hook)
  const attempt = startRun(hook, { jobId: 'job-rerun' })

  expect(hook.result.current.running).toBe(true)
  expect(hook.result.current.result).toBe(previous)
  expect(hook.result.current.error).toBeNull()
  expect(hook.result.current.currentJob).toMatchObject({ job_id: 'job-rerun', status: 'running', cost: null })

  const returned = await settle(attempt, failure, rejects)
  expect(returned).toEqual(rejects ? null : { ...failure, origin: { drawingKey: initialScope.drawingKey } })
  expect(hook.result.current.result).toBe(previous)
  expect(hook.result.current.result.overlay).toBe(goodResult.overlay)
  expect(hook.result.current.error).toBe(`Latest attempt failed: ${message}`)
  expect(hook.result.current.running).toBe(false)
  expect(hook.result.current.currentJob).toMatchObject({ job_id: 'job-rerun', status: 'failed', cost: null })
  expect(readInflightJob(hook.storage)).toBeNull()
  expect(hook.onNotice).toHaveBeenCalledTimes(1)

  render(<ResultPanel {...hook.result.current} tool={{ name: TOOL }} />)
  expect(screen.getByRole('cell', { name: '12' })).toBeVisible()
  expect(screen.getByText('Passed')).toBeVisible()
  expect(screen.getByRole('alert')).toHaveTextContent(`Latest attempt failed: ${message}`)
})

it('replaces the retained result on success and clears the previous attempt failure', async () => {
  const hook = mountController()
  const previous = await succeed(hook)
  await settle(startRun(hook), { ok: false, error: { message: 'Panel counting failed.' } })
  const retry = startRun(hook)
  expect(hook.result.current.result).toBe(previous)
  expect(hook.result.current.error).toBeNull()

  const replacement = { ...goodResult, result: { counts: { Panels: 24 }, total: 24 }, timing_ms: 90 }
  await settle(retry, replacement)
  expect(hook.result.current.result).toMatchObject(replacement)
  expect(hook.result.current.result).not.toBe(previous)
  expect(hook.result.current.error).toBeNull()
  expect(hook.result.current.currentJob).toMatchObject({ status: 'complete', elapsed_ms: 90, cost: replacement.cost })
  expect(hook.onNotice).toHaveBeenCalledTimes(2)
})

it('keeps the same valid result through consecutive failed reruns', async () => {
  const hook = mountController()
  const previous = await succeed(hook)
  await settle(startRun(hook), { ok: false, error: { message: 'The count was refused.' } })
  await settle(startRun(hook), new Error('The retry failed.'), true)
  expect(hook.result.current.result).toBe(previous)
  expect(hook.result.current.error).toBe('Latest attempt failed: The retry failed.')
})

it.each([
  ['project', { resetKey: 'project-b', drawingKey: 'project-b:drawing-a:v1' }],
  ['project reset key', { ...initialScope, resetKey: 'project-b' }],
  ['drawing', { ...initialScope, drawingKey: 'project-a:drawing-b:v1' }],
  ['drawing version', { ...initialScope, drawingKey: 'project-a:drawing-a:v2' }],
])('clears the previous result and error when the %s changes', async (_name, nextScope) => {
  const hook = mountController()
  await succeed(hook)
  await settle(startRun(hook), { ok: false, error: { message: 'The count was refused.' } })

  hook.rerender(nextScope)
  expect(hook.result.current.result).toBeNull()
  expect(hook.result.current.error).toBeNull()
  expect(hook.result.current.currentJob).toBeNull()
  // Returning to the old scope cannot resurrect a cleared result.
  hook.rerender(initialScope)
  const attempt = startRun(hook)
  expect(hook.result.current.result).toBeNull()
  const failure = { ok: false, error: { message: 'Still invalid.' } }
  await settle(attempt, failure)
  expect(hook.result.current.result).toMatchObject(failure)
})

it('clears retention when a different capability starts, even if that attempt fails', async () => {
  const hook = mountController()
  await succeed(hook)
  const differentTool = startRun(hook, { toolName: 'inspect-layers' })
  expect(hook.result.current.result).toBeNull()
  const failure = { ok: false, tool: 'inspect-layers', error: { message: 'Choose a layer.' } }
  await settle(differentTool, failure)
  expect(hook.result.current.result).toMatchObject(failure)

  const originalTool = startRun(hook)
  expect(hook.result.current.result).toBeNull()
  await settle(originalTool, new Error('The count failed.'), true)
  expect(hook.result.current.result).toBeNull()
  expect(hook.result.current.error).toBe('The count failed.')
})

it('ignores a superseded rejection while the newer rerun keeps the valid result', async () => {
  const hook = mountController()
  const previous = await succeed(hook)
  const older = startRun(hook)
  const newer = startRun(hook)
  await settle(older, new Error('The older attempt failed.'), true)
  expect(hook.result.current.running).toBe(true)
  expect(hook.result.current.result).toBe(previous)
  expect(hook.result.current.error).toBeNull()
  await settle(newer, { ok: false, error: { message: 'The latest attempt was refused.' } })
  expect(hook.result.current.result).toBe(previous)
  expect(hook.result.current.error).toBe('Latest attempt failed: The latest attempt was refused.')
})

it('clears retention on an explicit reset and ignores the old completion', async () => {
  const hook = mountController()
  await succeed(hook)
  const attempt = startRun(hook)
  act(() => hook.result.current.reset())
  await settle(attempt, { ok: false, error: { message: 'The old run failed.' } })
  expect(hook.result.current.result).toBeNull()
  expect(hook.result.current.error).toBeNull()
  expect(hook.result.current.currentJob).toBeNull()
})

it('retains a successful adopted result for the same capability and drawing', async () => {
  const hook = mountController()
  act(() => hook.result.current.adoptEnvelope(goodResult, { jobId: 'job-history' }))
  const attempt = startRun(hook)
  expect(hook.result.current.result).toBe(goodResult)
  await settle(attempt, { ok: false, error: { message: 'The count was refused.' } })
  expect(hook.result.current.result).toBe(goodResult)
  expect(hook.result.current.error).toBe('Latest attempt failed: The count was refused.')
})

it('does not retain an adopted history result from a different drawing', async () => {
  const hook = mountController()
  const historical = { ...goodResult, origin: { drawingKey: 'project-a:drawing-a:v0' } }
  act(() => hook.result.current.adoptEnvelope(historical))
  const attempt = startRun(hook)
  expect(hook.result.current.result).toBeNull()
  await settle(attempt, new Error('The count failed.'), true)
  expect(hook.result.current.result).toBeNull()
})

it('clears a retained result when reads pause and cannot restore it after resuming', async () => {
  const hook = mountController()
  await succeed(hook)
  const attempt = startRun(hook)
  hook.rerender({ ...initialScope, readsPaused: true })
  expect(hook.result.current.result).toBeNull()
  expect(hook.result.current.error).toBeNull()
  await settle(attempt, { ok: false, error: { message: 'The paused run failed.' } })
  hook.rerender({ ...initialScope, readsPaused: false })
  expect(hook.result.current.result).toBeNull()
  expect(hook.result.current.error).toBeNull()
  const next = startRun(hook)
  expect(hook.result.current.result).toBeNull()
  await settle(next, new Error('The new run failed.'), true)
  expect(hook.result.current.result).toBeNull()
  expect(hook.result.current.error).toBe('The new run failed.')
})

it('keeps history attachment behavior separate from rerun retention', async () => {
  const pending = deferred()
  const hook = mountController({ services: { attachToJob: vi.fn(() => pending.promise) } })
  await succeed(hook)
  let attachment
  act(() => { attachment = hook.result.current.attachJob('job-history', { toolName: TOOL }) })
  expect(hook.result.current.result).toBeNull()
  const failure = { ok: false, error: { message: 'The historical run failed.' } }
  await act(async () => { pending.resolve(failure); await attachment })
  expect(hook.result.current.result).toBe(failure)
  expect(hook.result.current.error).toBeNull()
})
