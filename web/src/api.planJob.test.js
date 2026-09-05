// W4g-3c: a live plan save resolves only when its job carries a receipt.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { config, loadPlanJobPointer, saveDrawingVersionPlan } from './api.js'

const DRAWING = 'demo-plan'
const JOB = 'plan-job-4'
const POINTER = 'leaf.cadedit.plan-job.' + DRAWING
const BYTES = new Uint8Array([48, 10, 69, 79, 70, 10])
const ACCEPTED = {
  drawing_id: DRAWING, job_id: JOB, parent: 4, commit: 'dwg-plan',
  commit_note: 'live writer', plan_sha256: 'plan-digest', source_sha256: 'source-digest',
}
const NEW_VERSION = { drawing_id: DRAWING, version: 5, parent: 4 }

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

function complete(cost = { usd_est: 0.125, engine_seconds: 12 }) {
  return {
    job_id: JOB,
    status: 'complete',
    result: {
      ok: true,
      result: { new_version: NEW_VERSION, workitem_id: 'aps-4', new_version_readable: true },
      cost,
    },
  }
}

let storage
let streams
beforeEach(() => {
  vi.useFakeTimers()
  const values = new Map()
  storage = {
    getItem: vi.fn((key) => values.get(key) ?? null),
    setItem: vi.fn((key, value) => values.set(key, String(value))),
    removeItem: vi.fn((key) => values.delete(key)),
  }
  vi.stubGlobal('sessionStorage', storage)
  streams = []
  vi.stubGlobal('EventSource', vi.fn(function () {
    this.close = vi.fn()
    streams.push(this)
  }))
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function serve(records, body = ACCEPTED, status = 202, earlierRecords = []) {
  let next = 0
  let nextEarlier = 0
  const fetchImpl = vi.fn(async (url, opts) => {
    if (opts?.method === 'POST') {
      expect(url).toBe(`${config.apiBase}/api/drawings/${DRAWING}/versions/plan`)
      return response(body, status)
    }
    const jobId = url === `${config.apiBase}/api/jobs/plan-job-old` ? 'plan-job-old' : JOB
    expect(url).toBe(`${config.apiBase}/api/jobs/${jobId}`)
    const record = jobId === JOB ? records[next++] : earlierRecords[nextEarlier++]
    if (record instanceof Error) throw record
    if (!record) throw new Error('unexpected job read')
    return response({ job_id: jobId, ...record })
  })
  vi.stubGlobal('fetch', fetchImpl)
  return fetchImpl
}

function save(opts = {}) {
  return saveDrawingVersionPlan(DRAWING, BYTES, 4, 'source-digest', {}, 'cap-4', opts)
}

describe('saveDrawingVersionPlan job receipts', () => {
  it('A1 refuses a 202 without a job id without subscribing or changing storage', async () => {
    const body = { drawing_id: DRAWING }
    serve([], body)
    await expect(save()).rejects.toMatchObject({
      message: 'the server accepted the plan without a job id', status: 502, body,
    })
    expect(streams).toHaveLength(0)
    expect(storage.setItem).not.toHaveBeenCalled()
    expect(storage.removeItem).not.toHaveBeenCalled()
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
  })

  it('A2 refuses a completed job without a version and clears its terminal pointer', async () => {
    const env = { ok: true, result: {}, cost: null }
    serve([{ ...complete(), result: env }])
    await expect(save()).rejects.toMatchObject({
      message: 'live commit reported no version', status: 502, jobId: JOB, body: env,
    })
    expect(storage.removeItem).toHaveBeenCalledWith(POINTER)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
  })

  it('A3 rejects a foreign job record and reattaches to the accepted job', async () => {
    serve([{ ...complete(), job_id: 'plan-job-OTHER' }, complete()])
    await expect(save()).resolves.toMatchObject({ job_id: JOB, head: 5, live: true })
    expect(streams).toHaveLength(2)
    expect(streams.every((stream) => stream.close.mock.calls.length === 1)).toBe(true)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
  })

  it('A4 refuses a new post when an earlier save landed and names its version', async () => {
    storage.setItem(POINTER, 'plan-job-old')
    const record = { ...complete(), job_id: 'plan-job-old' }
    record.result.result.new_version = { ...NEW_VERSION, version: 9 }
    const fetchImpl = serve([], ACCEPTED, 202, [record])
    await expect(save()).rejects.toMatchObject({
      status: 409, landedVersion: 9, jobId: 'plan-job-old',
      message: 'an earlier save landed as version 9; reopen the drawing before saving again',
    })
    expect(fetchImpl.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(0)
    expect(storage.removeItem).toHaveBeenCalledWith(POINTER)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
  })

  it('A5 posts a new plan after reconciling an earlier terminal failure', async () => {
    storage.setItem(POINTER, 'plan-job-old')
    const error = { error_code: 'WRITE_FAILED', message: 'the earlier write failed' }
    const fetchImpl = serve([complete()], ACCEPTED, 202, [{ status: 'failed', error }])
    await expect(save()).resolves.toMatchObject({ job_id: JOB, head: 5, live: true })
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/jobs/plan-job-old`)
    expect(fetchImpl.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(1)
    expect(storage.removeItem).toHaveBeenCalledWith(POINTER)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
  })

  it('A6 keeps the earlier pointer and posts nothing when reconciliation loses monitoring', async () => {
    storage.setItem(POINTER, 'plan-job-old')
    const fetchImpl = serve([], ACCEPTED, 202, Array.from({ length: 24 }, () => new Error('offline')))
    const pending = save().catch((error) => error)
    await vi.advanceTimersByTimeAsync(22000)
    expect(await pending).toMatchObject({
      outcomeUnknown: true, jobId: 'plan-job-old',
      message: 'outcome unknown: job plan-job-old may still be running; reopen the drawing to see whether the version landed',
    })
    expect(fetchImpl.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(0)
    expect(storage.removeItem).not.toHaveBeenCalled()
    expect(loadPlanJobPointer(DRAWING)).toBe('plan-job-old')
  })

  it('returns a 201 body without subscribing or storing a job', async () => {
    const body = { new_version: NEW_VERSION, head: 5, commit: 'dwg-plan', cost: { engine_usd: 0 } }
    const fetchImpl = serve([], body, 201)
    expect(await save()).toBe(body)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const request = fetchImpl.mock.calls[0][1]
    expect(request.body.get('parent_version')).toBe('4')
    expect(request.headers['X-Checkout-Capability']).toBe('cap-4')
    expect(storage.setItem).not.toHaveBeenCalled()
    expect(streams).toHaveLength(0)
  })

  it('waits through running to complete, maps the live receipt, and clears the pointer', async () => {
    const fetchImpl = serve([{ status: 'running', progress: 'writing DWG' }, complete()])
    const onStatus = vi.fn()
    let resolved = false
    const pending = save({ onStatus }).then((receipt) => { resolved = true; return receipt })
    await vi.advanceTimersByTimeAsync(0)
    expect(resolved).toBe(false)
    expect(storage.setItem).toHaveBeenCalledWith(POINTER, JOB)
    expect(loadPlanJobPointer(DRAWING)).toBe(JOB)
    expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({ status: 'running', progress: 'writing DWG', job_id: JOB }))
    await vi.advanceTimersByTimeAsync(1000)
    expect(await pending).toEqual({
      drawing_id: DRAWING, job_id: JOB, new_version: NEW_VERSION, head: 5, parent: 4,
      commit: 'dwg-plan', commit_note: 'live writer', plan_sha256: 'plan-digest', source_sha256: 'source-digest',
      workitem_id: 'aps-4', new_version_readable: true, live: true,
      cost: { engine: 'aps-workitem', engine_usd: 0.125, engine_seconds: 12 },
    })
    expect(storage.removeItem).toHaveBeenCalledWith(POINTER)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(3)
    expect(streams[0].close).toHaveBeenCalledTimes(1)
  })

  it('rejects a failed terminal envelope without reattaching or posting a sidecar', async () => {
    const error = { error_code: 'WRITE_FAILED', message: 'APS rejected the plan' }
    const fetchImpl = serve([{ status: 'failed', error }])
    await expect(save()).rejects.toMatchObject({
      message: error.message, status: 422, jobId: JOB,
      body: { ok: false, error },
    })
    expect(storage.removeItem).toHaveBeenCalledWith(POINTER)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(streams).toHaveLength(1)
  })

  it('reports absent cost fields as unknown and preserves pending read-back', async () => {
    const record = complete({})
    record.result.result.new_version_readable = false
    serve([record])
    expect(await save()).toMatchObject({
      live: true, new_version_readable: false,
      cost: { engine: 'aps-workitem', engine_usd: null, engine_seconds: null },
    })
  })

  it('reattaches once after twelve poll failures and resolves the same job', async () => {
    const fetchImpl = serve([...Array.from({ length: 12 }, () => new Error('offline')), complete()])
    const pending = save()
    await vi.advanceTimersByTimeAsync(11000)
    expect(await pending).toMatchObject({ job_id: JOB, head: 5, live: true })
    expect(fetchImpl.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(1)
    expect(fetchImpl).toHaveBeenCalledTimes(14)
    expect(streams).toHaveLength(2)
    expect(streams.every((stream) => stream.close.mock.calls.length === 1)).toBe(true)
    expect(loadPlanJobPointer(DRAWING)).toBeNull()
  })

  it('reports a second monitoring loss as outcome unknown and keeps the resumable pointer', async () => {
    const fetchImpl = serve(Array.from({ length: 24 }, () => new Error('offline')))
    const pending = save().catch((error) => error)
    await vi.advanceTimersByTimeAsync(22000)
    expect(await pending).toMatchObject({
      outcomeUnknown: true, jobId: JOB,
      message: `outcome unknown: job ${JOB} may still be running; reopen the drawing to see whether the version landed`,
    })
    expect(streams).toHaveLength(2)
    expect(fetchImpl).toHaveBeenCalledTimes(25)
    expect(storage.removeItem).not.toHaveBeenCalled()
    expect(loadPlanJobPointer(DRAWING)).toBe(JOB)
  })
})
