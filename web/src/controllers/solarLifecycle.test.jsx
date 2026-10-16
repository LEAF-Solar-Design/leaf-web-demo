// @vitest-environment jsdom
//
// solar-parity-017: a result that arrives after a newer run is listed as stale
// and never replaces the design on screen, and an expired sign-in keeps the
// run: an unsubmitted run is kept behind 'Review and run', a submitted one
// keeps its inflight pointer so it reattaches after sign-in.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { act, cleanup, fireEvent, render, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import JobRail from '../components/JobRail.jsx'
import useJobController, {
  INFLIGHT_JOB_KEY,
  MAX_PENDING_RUN_BYTES,
  MAX_STALE_RESULTS,
  PENDING_RUN_KEY,
  PENDING_RUN_TTL_MS,
  readInflightJob,
} from './useJobController.js'

afterEach(cleanup)

const HERE = dirname(fileURLToPath(import.meta.url))
const TOOL = 'solar-schedule'

function memoryStorage(seed = {}) {
  const map = new Map(Object.entries(seed))
  return {
    map,
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)) },
    removeItem: (key) => { map.delete(key) },
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function makeServices(overrides = {}) {
  return {
    attachToJob: vi.fn(() => new Promise(() => {})),
    closeJobBeacon: vi.fn(() => true),
    getJob: vi.fn(async () => null),
    listJobs: vi.fn(async () => []),
    recordToEnvelope: vi.fn((record) => ({ ok: record?.status === 'complete', tool: record?.tool })),
    ...overrides,
  }
}

function mountHook({ storage = memoryStorage(), services = makeServices(), resetKey = 'k0', ...callbacks } = {}) {
  const hook = renderHook(
    ({ key }) => useJobController({
      services,
      storage,
      resetKey: key,
      pollIntervalMs: 1000000000,
      ...callbacks,
    }),
    { initialProps: { key: resetKey } },
  )
  return { ...hook, storage, services }
}

async function startRun(hook, { jobId = null, toolName = TOOL, submission = null } = {}) {
  const gate = deferred()
  let run
  await act(async () => {
    run = hook.result.current.runJob({
      toolName,
      submission,
      execute: ({ onSubmit }) => {
        if (jobId) onSubmit(jobId)
        return gate.promise
      },
    })
  })
  return { gate, run }
}

async function settle(entry, value) {
  await act(async () => {
    entry.gate.resolve(value)
    await entry.run
  })
}

async function fail(entry, cause) {
  await act(async () => {
    entry.gate.reject(cause)
    await entry.run
  })
}

describe('useJobController contract', () => {
  it('exports the pending run and stale bounds', () => {
    expect(PENDING_RUN_KEY).toBe('leaf.pendingRun')
    expect(PENDING_RUN_TTL_MS).toBe(1800000)
    expect(MAX_PENDING_RUN_BYTES).toBe(65536)
    expect(MAX_STALE_RESULTS).toBe(5)
  })
})

describe('late results are listed as stale, never applied', () => {
  it('lists a superseded ok result and seats only the newest run', async () => {
    const onCompleteVersion = vi.fn()
    const onNotice = vi.fn()
    const hook = mountHook({ onCompleteVersion, onNotice })
    const a = await startRun(hook, { jobId: 'job-a' })
    const b = await startRun(hook, { jobId: 'job-b' })

    await settle(a, { ok: true, tool: TOOL, result: { new_version: 7 } })
    expect(hook.result.current.staleResults).toEqual([{ job_id: 'job-a', tool: TOOL, new_version: 7 }])
    expect(onCompleteVersion).toHaveBeenCalledTimes(0)
    expect(onNotice).toHaveBeenCalledTimes(0)
    expect(hook.result.current.result).toBeNull()

    await settle(b, { ok: true, result: { new_version: 8 } })
    expect(onCompleteVersion).toHaveBeenCalledTimes(1)
    expect(onCompleteVersion.mock.calls[0][0]).toBe(8)
  })

  it('lists a committed result that finishes after another job is adopted', async () => {
    const hook = mountHook()
    const running = await startRun(hook, { jobId: 'job-background' })

    act(() => {
      hook.result.current.adoptEnvelope(
        { ok: true, tool: 'count-panels', result: { count: 12 } },
        { jobId: 'job-adopted', toolName: 'count-panels' },
      )
    })
    await settle(running, { ok: true, tool: TOOL, result: { new_version: 7 } })

    expect(hook.result.current.result).toMatchObject({ tool: 'count-panels' })
    expect(hook.result.current.staleResults).toEqual([
      { job_id: 'job-background', tool: TOOL, new_version: 7 },
    ])
  })

  it('lists a committed result that finishes after the user detaches', async () => {
    const hook = mountHook()
    const running = await startRun(hook, { jobId: 'job-detached' })

    act(() => { hook.result.current.detachJob() })
    await settle(running, { ok: true, tool: TOOL, result: { new_version: 8 } })

    expect(hook.result.current.currentJob).toBeNull()
    expect(hook.result.current.staleResults).toEqual([
      { job_id: 'job-detached', tool: TOOL, new_version: 8 },
    ])
  })

  it('does not list a superseded failed result', async () => {
    const hook = mountHook()
    const a = await startRun(hook, { jobId: 'job-a' })
    await startRun(hook, { jobId: 'job-b' })
    await settle(a, { ok: false })
    expect(hook.result.current.staleResults).toHaveLength(0)
  })

  it('does not list a result from before reset()', async () => {
    const hook = mountHook()
    const a = await startRun(hook, { jobId: 'job-a' })
    await startRun(hook, { jobId: 'job-b' })
    act(() => { hook.result.current.reset() })
    await settle(a, { ok: true, tool: TOOL, result: { new_version: 7 } })
    expect(hook.result.current.staleResults).toHaveLength(0)
  })

  it('does not list a result from before a resetKey change', async () => {
    const hook = mountHook()
    const a = await startRun(hook, { jobId: 'job-a' })
    await startRun(hook, { jobId: 'job-b' })
    hook.rerender({ key: 'k1' })
    await settle(a, { ok: true, tool: TOOL, result: { new_version: 7 } })
    expect(hook.result.current.staleResults).toHaveLength(0)
  })

  it('keeps the newest five stale results, newest first', async () => {
    const hook = mountHook()
    const runs = []
    for (let i = 1; i <= 8; i += 1) runs.push(await startRun(hook, { jobId: `j${i}` }))
    for (let i = 0; i < 7; i += 1) await settle(runs[i], { ok: true, tool: TOOL, result: {} })
    expect(hook.result.current.staleResults.map((row) => row.job_id)).toEqual(['j7', 'j6', 'j5', 'j4', 'j3'])
  })

  it('records one row per job id and dismisses it', async () => {
    const attach = deferred()
    const services = makeServices({ attachToJob: vi.fn(() => attach.promise) })
    const hook = mountHook({ services })
    let attached
    await act(async () => { attached = hook.result.current.attachJob('job-a', { toolName: TOOL }) })
    const x = await startRun(hook, { jobId: 'job-a' })
    await startRun(hook, { jobId: 'job-z' })

    await act(async () => {
      attach.resolve({ ok: true, tool: TOOL, result: {} })
      await attached
    })
    await settle(x, { ok: true, tool: TOOL, result: {} })
    expect(hook.result.current.staleResults.map((row) => row.job_id)).toEqual(['job-a'])

    act(() => { hook.result.current.dismissStaleResult('job-a') })
    expect(hook.result.current.staleResults).toHaveLength(0)
  })

  it('does not list a superseded run that never got a job id', async () => {
    const hook = mountHook()
    const a = await startRun(hook)
    await startRun(hook, { jobId: 'job-b' })
    await settle(a, { ok: true, tool: TOOL, result: { new_version: 7 } })
    expect(hook.result.current.staleResults).toHaveLength(0)
  })
})

describe('an expired sign-in keeps the run', () => {
  it('keeps an unsubmitted run as the pending run and raises sign-in once', async () => {
    const onAuthRequired = vi.fn()
    const hook = mountHook({ onAuthRequired })
    const before = Date.now()
    const a = await startRun(hook, { submission: { params: { zone: 'A' } } })
    await fail(a, { status: 401, message: 'x' })
    const after = Date.now()

    const stored = JSON.parse(hook.storage.getItem(PENDING_RUN_KEY))
    expect(stored.v).toBe(1)
    expect(stored.tool).toBe(TOOL)
    expect(stored.params).toEqual({ zone: 'A' })
    expect(stored.ts).toBeGreaterThanOrEqual(before)
    expect(stored.ts).toBeLessThanOrEqual(after)
    expect(Object.keys(stored).sort()).toEqual(['params', 'tool', 'ts', 'v'])
    expect(onAuthRequired.mock.calls.filter(([value]) => value === true)).toHaveLength(1)
    expect(hook.result.current.error).not.toBeNull()
    expect(hook.result.current.pendingRun).toEqual({ tool: TOOL, params: { zone: 'A' }, ts: stored.ts })
  })

  it('keeps a submitted run on its inflight pointer instead', async () => {
    const hook = mountHook()
    const a = await startRun(hook, { jobId: 'job-x', submission: { params: { zone: 'A' } } })
    await fail(a, { status: 401 })
    expect(hook.storage.getItem(PENDING_RUN_KEY)).toBeNull()
    expect(readInflightJob(hook.storage)?.job_id).toBe('job-x')
  })

  it('clears the pointer and keeps nothing on a server error', async () => {
    const hook = mountHook()
    const a = await startRun(hook, { jobId: 'job-y', submission: { params: { zone: 'A' } } })
    await fail(a, { status: 500 })
    expect(hook.storage.getItem(PENDING_RUN_KEY)).toBeNull()
    expect(readInflightJob(hook.storage)).toBeNull()
    expect(hook.storage.getItem(INFLIGHT_JOB_KEY)).toBeNull()
  })

  it('reads a fresh pending run at mount and drops an expired or malformed one', () => {
    const fresh = memoryStorage({
      [PENDING_RUN_KEY]: JSON.stringify({ v: 1, tool: TOOL, params: { zone: 'A' }, ts: Date.now() - 60000 }),
    })
    expect(mountHook({ storage: fresh }).result.current.pendingRun.tool).toBe(TOOL)

    const expired = memoryStorage({
      [PENDING_RUN_KEY]: JSON.stringify({ v: 1, tool: TOOL, params: { zone: 'A' }, ts: Date.now() - 1800001 }),
    })
    expect(mountHook({ storage: expired }).result.current.pendingRun).toBeNull()
    expect(expired.map.has(PENDING_RUN_KEY)).toBe(false)

    const malformed = memoryStorage({ [PENDING_RUN_KEY]: '{not json' })
    expect(mountHook({ storage: malformed }).result.current.pendingRun).toBeNull()
    expect(malformed.map.has(PENDING_RUN_KEY)).toBe(false)
  })

  it('takes the pending run once, and discards it', () => {
    const record = () => JSON.stringify({ v: 1, tool: TOOL, params: { zone: 'A' }, ts: Date.now() - 1000 })
    const storage = memoryStorage({ [PENDING_RUN_KEY]: record() })
    const hook = mountHook({ storage })
    const stored = JSON.parse(storage.getItem(PENDING_RUN_KEY))
    let taken
    act(() => { taken = hook.result.current.takePendingRun() })
    expect(taken).toEqual({ tool: TOOL, params: { zone: 'A' }, ts: stored.ts })
    expect(storage.map.has(PENDING_RUN_KEY)).toBe(false)
    let again
    act(() => { again = hook.result.current.takePendingRun() })
    expect(again).toBeNull()

    const other = memoryStorage({ [PENDING_RUN_KEY]: record() })
    const second = mountHook({ storage: other })
    expect(second.result.current.pendingRun).not.toBeNull()
    act(() => { second.result.current.discardPendingRun() })
    expect(other.map.has(PENDING_RUN_KEY)).toBe(false)
    expect(second.result.current.pendingRun).toBeNull()
  })

  it('writes nothing for oversize params or a bad tool name, and still raises sign-in', async () => {
    const onAuthRequired = vi.fn()
    const hook = mountHook({ onAuthRequired })
    const big = await startRun(hook, { submission: { params: { blob: 'x'.repeat(MAX_PENDING_RUN_BYTES + 1) } } })
    await fail(big, { status: 401 })
    expect(hook.storage.getItem(PENDING_RUN_KEY)).toBeNull()

    const bad = await startRun(hook, { toolName: 'bad name', submission: { params: { zone: 'A' } } })
    await fail(bad, { status: 401 })
    expect(hook.storage.getItem(PENDING_RUN_KEY)).toBeNull()
    expect(hook.result.current.pendingRun).toBeNull()
    expect(onAuthRequired.mock.calls.filter(([value]) => value === true)).toHaveLength(2)
  })
})

// The server's 401 is an ordinary envelope: apiFetch never throws, so
// runToolAsync resolves it instead of rejecting.
const UNAUTHENTICATED = {
  ok: false,
  tool: null,
  error: { error_code: 'UNAUTHENTICATED' },
  degraded_mode: false,
}

async function flushBoot() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
}

describe('CORR1 a real sign-in expiry keeps the run', () => {
  it('CORR1 a resolved UNAUTHENTICATED envelope before submit keeps the pending run', async () => {
    const onAuthRequired = vi.fn()
    const hook = mountHook({ onAuthRequired })
    const a = await startRun(hook, { submission: { params: { zone: 'A' } } })
    await settle(a, UNAUTHENTICATED)

    const stored = JSON.parse(hook.storage.getItem(PENDING_RUN_KEY))
    expect(stored).not.toBeNull()
    expect(stored.tool).toBe(TOOL)
    expect(stored.params).toEqual({ zone: 'A' })
    expect(hook.result.current.pendingRun?.tool).toBe(TOOL)
    expect(onAuthRequired.mock.calls.filter(([value]) => value === true)).toHaveLength(1)
  })

  it('CORR2 a resolved UNAUTHENTICATED envelope after submit is a failed job result', async () => {
    const onAuthRequired = vi.fn()
    const hook = mountHook({ onAuthRequired })
    const a = await startRun(hook, { jobId: 'job-x', submission: { params: { zone: 'A' } } })
    await settle(a, UNAUTHENTICATED)

    expect(readInflightJob(hook.storage)).toBeNull()
    expect(hook.storage.getItem(PENDING_RUN_KEY)).toBeNull()
    expect(hook.result.current.pendingRun).toBeNull()
    expect(onAuthRequired.mock.calls.filter(([value]) => value === true)).toHaveLength(0)
    expect(hook.result.current.result).toEqual(UNAUTHENTICATED)
  })

  it('CORR2 a thrown 401 while polling after submit still keeps the pointer', async () => {
    const onAuthRequired = vi.fn()
    const hook = mountHook({ onAuthRequired })
    const a = await startRun(hook, { jobId: 'job-x', submission: { params: { zone: 'A' } } })
    await fail(a, new Error('lost job job-x: GET /api/jobs/job-x -> 401'))

    expect(readInflightJob(hook.storage)?.job_id).toBe('job-x')
    expect(hook.storage.getItem(PENDING_RUN_KEY)).toBeNull()
    expect(onAuthRequired.mock.calls.filter(([value]) => value === true)).toHaveLength(1)
  })

  it('CORR1 boot reattach keeps the pointer on a 401 and clears it on a 500', async () => {
    const pointer = () => JSON.stringify({ job_id: 'job-boot', tool: TOOL, ts: Date.now() })

    const denied = memoryStorage({ [INFLIGHT_JOB_KEY]: pointer() })
    const deniedServices = makeServices({
      getJob: vi.fn(async () => { throw Object.assign(new Error('GET /api/jobs/job-boot -> 401'), { status: 401 }) }),
    })
    const kept = mountHook({ storage: denied, services: deniedServices })
    await flushBoot()
    expect(deniedServices.getJob).toHaveBeenCalledTimes(1)
    expect(readInflightJob(denied)?.job_id).toBe('job-boot')
    expect(kept.result.current.inflight?.job_id).toBe('job-boot')
    kept.unmount()

    const broken = memoryStorage({ [INFLIGHT_JOB_KEY]: pointer() })
    const brokenServices = makeServices({
      getJob: vi.fn(async () => { throw Object.assign(new Error('GET /api/jobs/job-boot -> 500'), { status: 500 }) }),
    })
    const cleared = mountHook({ storage: broken, services: brokenServices })
    await flushBoot()
    expect(brokenServices.getJob).toHaveBeenCalledTimes(1)
    expect(readInflightJob(broken)).toBeNull()
    expect(cleared.result.current.inflight).toBeNull()
  })

  it('CORR1 Review and run keeps the pending run when the catalog load fails', async () => {
    const record = JSON.stringify({ v: 1, tool: TOOL, params: { zone: 'A' }, ts: Date.now() - 1000 })
    const storage = memoryStorage({ [PENDING_RUN_KEY]: record })
    const hook = mountHook({ storage })
    const loadTools = vi.fn(async () => { throw Object.assign(new Error('GET /api/tools -> 503'), { status: 503 }) })
    let outcome
    await act(async () => {
      outcome = await hook.result.current.preparePendingRun(loadTools).then(
        () => 'resolved',
        (cause) => cause,
      )
    })
    expect(loadTools).toHaveBeenCalledTimes(1)
    expect(outcome?.status).toBe(503)
    expect(storage.map.has(PENDING_RUN_KEY)).toBe(true)
    expect(hook.result.current.pendingRun?.tool).toBe(TOOL)
  })

  it('CORR1 Review and run takes the pending run only after the catalog loads', async () => {
    const record = () => JSON.stringify({ v: 1, tool: TOOL, params: { zone: 'A' }, ts: Date.now() - 1000 })
    const storage = memoryStorage({ [PENDING_RUN_KEY]: record() })
    const hook = mountHook({ storage })
    let prepared
    await act(async () => {
      prepared = await hook.result.current.preparePendingRun(async () => [{ name: 'other' }, { name: TOOL }])
    })
    expect(prepared.tool).toEqual({ name: TOOL })
    expect(prepared.pending.params).toEqual({ zone: 'A' })
    expect(storage.map.has(PENDING_RUN_KEY)).toBe(false)
    expect(hook.result.current.pendingRun).toBeNull()

    const gone = memoryStorage({ [PENDING_RUN_KEY]: record() })
    const second = mountHook({ storage: gone })
    let missing
    await act(async () => {
      missing = await second.result.current.preparePendingRun(async () => [{ name: 'other' }])
    })
    expect(missing.tool).toBeNull()
    expect(missing.pending.tool).toBe(TOOL)

    const empty = mountHook()
    const loadTools = vi.fn(async () => [])
    let none
    await act(async () => { none = await empty.result.current.preparePendingRun(loadTools) })
    expect(none).toBeNull()
    expect(loadTools).toHaveBeenCalledTimes(0)
  })
})

describe('JobRail notes', () => {
  it('renders one stale note per row with a Dismiss action', () => {
    const onDismissStale = vi.fn()
    const { container, getByText } = render(
      <JobRail mock jobs={[]} currentJob={null} staleResults={[{ job_id: 'job-a', tool: TOOL }]} onDismissStale={onDismissStale} />,
    )
    const notes = container.querySelectorAll('[data-stale-job="job-a"]')
    expect(notes).toHaveLength(1)
    expect(notes[0].textContent).toContain(
      'solar-schedule finished after this view changed. Its result was not loaded into this view.',
    )
    fireEvent.click(getByText('Dismiss'))
    expect(onDismissStale).toHaveBeenCalledTimes(1)
    expect(onDismissStale).toHaveBeenCalledWith('job-a')
  })

  it('renders no stale note when there are none', () => {
    const first = render(<JobRail mock jobs={[]} currentJob={null} />)
    expect(first.container.querySelectorAll('[data-stale-job]')).toHaveLength(0)
    cleanup()
    const second = render(<JobRail mock jobs={[]} currentJob={null} staleResults={[]} />)
    expect(second.container.querySelectorAll('[data-stale-job]')).toHaveLength(0)
  })

  it('renders the pending run with Review and run and Discard', () => {
    const onResumePendingRun = vi.fn()
    const onDiscardPendingRun = vi.fn()
    const { container, getByText } = render(
      <JobRail
        mock
        jobs={[]}
        currentJob={null}
        pendingRun={{ tool: TOOL }}
        onResumePendingRun={onResumePendingRun}
        onDiscardPendingRun={onDiscardPendingRun}
      />,
    )
    const notes = container.querySelectorAll('[data-pending-run]')
    expect(notes).toHaveLength(1)
    expect(notes[0].textContent).toContain(
      'Your sign-in expired before solar-schedule was submitted. Your inputs were kept.',
    )
    fireEvent.click(getByText('Review and run'))
    fireEvent.click(getByText('Discard'))
    expect(onResumePendingRun).toHaveBeenCalledTimes(1)
    expect(onDiscardPendingRun).toHaveBeenCalledTimes(1)
  })

  it('renders neither note on the spine', () => {
    const { container } = render(
      <JobRail
        mock
        spine
        jobs={[]}
        currentJob={null}
        staleResults={[{ job_id: 'job-a', tool: TOOL }]}
        pendingRun={{ tool: TOOL }}
      />,
    )
    expect(container.querySelectorAll('[data-stale-job]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-pending-run]')).toHaveLength(0)
  })
})

describe('wiring pins', () => {
  const app = readFileSync(join(HERE, '..', 'App.jsx'), 'utf8')
  const frame = readFileSync(join(HERE, '..', 'site', 'SurfaceFrame.jsx'), 'utf8')

  it('keeps the job controller reset on the drawing, never the surface', () => {
    const start = app.indexOf('useJobController({')
    expect(start).toBeGreaterThan(-1)
    const end = app.indexOf('})', start)
    expect(end).toBeGreaterThan(start)
    const args = app.slice(start, end)
    expect(args.includes('resetKey')).toBe(true)
    expect(args.includes('activeSurface')).toBe(false)
  })

  it('passes the submission inside the onRun runJob call', () => {
    const start = app.indexOf('const envelope = await runJob(')
    expect(start).toBeGreaterThan(-1)
    const end = app.indexOf('execute:', start)
    expect(app.slice(start, end).includes('submission: { params: merged }')).toBe(true)
  })

  it('hands the stale and pending props to the rail', () => {
    const start = app.indexOf('jobRail={{')
    expect(start).toBeGreaterThan(-1)
    const rail = app.slice(start, app.indexOf('}}', start))
    for (const key of ['staleResults', 'onDismissStale', 'pendingRun', 'onResumePendingRun', 'onDiscardPendingRun']) {
      expect(rail.includes(key)).toBe(true)
      expect(frame.includes(`${key}={rail.${key}}`)).toBe(true)
    }
  })

  it('resumes a pending run through the confirm ladder, never a direct run', () => {
    const start = app.indexOf('const onResumePendingRun = useCallback(')
    expect(start).toBeGreaterThan(-1)
    const body = app.slice(start, app.indexOf('}, [', start))
    expect(body.includes('if (mock) return')).toBe(true)
    expect(body.includes('getTools(false)')).toBe(true)
    expect(body.includes("onRequestCatalogRun(tool, pending.params, null, 'catalog', { complete: true })")).toBe(true)
    expect(body.includes('is no longer in your catalog. Your saved inputs were discarded.')).toBe(true)
    expect(body.includes('onRun(')).toBe(false)
  })

  it('CORR1 loads the catalog before taking the pending run, and keeps the inputs on a failed load', () => {
    const start = app.indexOf('const onResumePendingRun = useCallback(')
    const body = app.slice(start, app.indexOf('}, [', start))
    expect(body.includes('preparePendingRun(() => getTools(false))')).toBe(true)
    expect(body.includes('takePendingRun()')).toBe(false)
    expect(body.includes('could not be prepared, so your inputs were kept.')).toBe(true)
  })
})
