import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { attachToJob, runToolAsync } from './api.js'

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

let streams
let fetchImpl

beforeEach(() => {
  vi.useFakeTimers()
  streams = []
  vi.stubGlobal('EventSource', class {
    constructor() {
      this.closed = false
      streams.push(this)
    }

    close() {
      this.closed = true
    }
  })
  fetchImpl = vi.fn(async () => response({ job_id: 'j1', status: 'running' }))
  vi.stubGlobal('fetch', fetchImpl)
})

afterEach(() => {
  vi.clearAllTimers()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('job subscription signals', () => {
  it('attachToJob stops polling and closes the stream when its signal aborts', async () => {
    const controller = new AbortController()
    const pending = attachToJob('j1', { signal: controller.signal })
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(streams).toHaveLength(1)

    controller.abort()
    await rejected
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(streams[0].closed).toBe(true)
  })

  it('attachToJob with an already aborted signal makes no read', async () => {
    await expect(attachToJob('j1', { signal: AbortSignal.abort() }))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(streams).toHaveLength(0)
  })

  it('runToolAsync still delivers the job id when the signal aborts before the submit returns', async () => {
    let resolveSubmit
    fetchImpl.mockImplementationOnce(() => new Promise((resolve) => { resolveSubmit = resolve }))
    const controller = new AbortController()
    const onSubmit = vi.fn()
    const pending = runToolAsync({ name: 'count' }, {}, 'rooftop_demo', {
      onSubmit, signal: controller.signal,
    })
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })

    controller.abort()
    resolveSubmit(response({ job_id: 'j9' }, 202))
    await rejected
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith('j9')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][1].method).toBe('POST')
    expect(fetchImpl.mock.calls[0][1].signal).toBeUndefined()
    expect(streams).toHaveLength(0)
  })

  it('attachToJob without a signal behaves as before', async () => {
    const envelope = { ok: true, tool: 'count' }
    fetchImpl.mockResolvedValueOnce(response({ job_id: 'j1', status: 'complete', result: envelope }))
    const pending = attachToJob('j1')
    await vi.advanceTimersByTimeAsync(0)
    await expect(pending).resolves.toEqual(envelope)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(streams).toHaveLength(1)
    expect(streams[0].closed).toBe(true)
  })
})
