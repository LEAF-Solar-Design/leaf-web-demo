import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLocalCadBridge } from './localCadBridge.js'

const payload = (extra = {}) => ({ rev: 7, epoch: 'boot-a', status: 'connected', drawing: 'site.dwg',
  intake: { drawing: 'site.dwg', lines: [{ handle: 'A', pts: [[0, 0, 0], [1, 1, 0]] }] },
  change: { summary: 'LEAFGENERATE added 108 objects' }, ...extra })
const response = data => ({ ok: true, json: async () => data })
const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve() }

describe('local bridge polling', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('is opt-in, tries ports in order, and sends cursor queries with a plain GET', async () => {
    const fetchImpl = vi.fn().mockRejectedValueOnce(new Error('down'))
      .mockResolvedValue(response(payload()))
    const onUpdate = vi.fn(), onStatus = vi.fn()
    const bridge = createLocalCadBridge({ ports: [8125, 8126], fetchImpl, onUpdate, onStatus })
    expect(fetchImpl).not.toHaveBeenCalled()
    bridge.start()
    await flush()
    expect(fetchImpl.mock.calls.map(call => call[0])).toEqual([
      'http://127.0.0.1:8125/api/live/intake?since=0&epoch=',
      'http://127.0.0.1:8126/api/live/intake?since=0&epoch=',
    ])
    expect(fetchImpl.mock.calls[1][1]).toEqual({ method: 'GET', cache: 'no-store', signal: expect.any(AbortSignal) })
    expect(onUpdate.mock.calls[0][0]).toMatchObject({ rev: 7, epoch: 'boot-a', drawing: 'site.dwg',
      summary: 'LEAFGENERATE added 108 objects', intake: { source: 'bridge', documentId: 'autocad:site.dwg' } })
    await vi.advanceTimersByTimeAsync(400)
    expect(fetchImpl.mock.calls[2][0]).toContain('since=7&epoch=boot-a')
    expect(onStatus.mock.calls.map(call => call[0].status)).toEqual(['connecting', 'connected'])
    bridge.stop()
  })

  it('skips unchanged updates and accepts a restarted epoch at the same revision', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload()))
      .mockResolvedValueOnce(response(payload({ unchanged: true, intake: undefined })))
      .mockResolvedValue(response(payload({ epoch: 'boot-b' })))
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onUpdate })
    bridge.start(); await flush()
    await vi.advanceTimersByTimeAsync(400)
    expect(onUpdate).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(400)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot().epoch).toBe('boot-b')
    bridge.stop()
  })

  it('backs off unreachable ports up to the limit and recovers at the normal interval', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('offline'))
    const onStatus = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onStatus, intervalMs: 100, maxBackoffMs: 400 })
    bridge.start(); await flush()
    expect(bridge.snapshot().status).toBe('unreachable')
    await vi.advanceTimersByTimeAsync(199)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(400)
    expect(fetchImpl).toHaveBeenCalledTimes(3)
    fetchImpl.mockResolvedValue(response(payload()))
    await vi.advanceTimersByTimeAsync(400)
    expect(bridge.snapshot().status).toBe('connected')
    await vi.advanceTimersByTimeAsync(100)
    expect(fetchImpl).toHaveBeenCalledTimes(5)
    bridge.stop()
  })

  it('treats busy/offline AutoCAD as waiting and retains summaries on unchanged polls', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload()))
      .mockResolvedValueOnce(response(payload({ unchanged: true, intake: undefined, change: null })))
      .mockResolvedValueOnce(response(payload({ status: 'busy', unchanged: true, intake: undefined, change: null })))
      .mockResolvedValue(response(payload({ status: 'offline', drawing: null, unchanged: true, intake: undefined })))
    const bridge = createLocalCadBridge({ fetchImpl })
    bridge.start(); await flush()
    await vi.advanceTimersByTimeAsync(400)
    expect(bridge.snapshot().summary).toBe('LEAFGENERATE added 108 objects')
    await vi.advanceTimersByTimeAsync(400)
    expect(bridge.snapshot().status).toBe('waiting')
    await vi.advanceTimersByTimeAsync(800)
    expect(bridge.snapshot()).toMatchObject({ status: 'waiting', drawing: null })
    bridge.stop()
  })

  it('cancels timers and ignores late responses even after another start', async () => {
    let finish
    const fetchImpl = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValue(response(payload({ epoch: 'new' })))
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onUpdate })
    bridge.start(); bridge.stop(); bridge.start(); await flush()
    finish(response(payload({ epoch: 'old' }))); await flush()
    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(bridge.snapshot().epoch).toBe('new')
    bridge.stop()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot()).toMatchObject({ running: false, status: 'stopped' })
  })

  it('aborts an in-flight request on stop, including its deadline', async () => {
    const fetchImpl = vi.fn(() => new Promise(() => {}))
    const bridge = createLocalCadBridge({ fetchImpl })
    bridge.start()
    const signal = fetchImpl.mock.calls[0][1].signal
    expect(signal.aborted).toBe(false)
    bridge.stop(); await flush()
    expect(signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(10000)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('times out even a fetch that ignores abort and polls again after backoff', async () => {
    const fetchImpl = vi.fn().mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValue(response(payload()))
    const bridge = createLocalCadBridge({ fetchImpl })
    bridge.start()
    await vi.advanceTimersByTimeAsync(3999)
    expect(bridge.snapshot().status).toBe('connecting')
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true)
    expect(bridge.snapshot()).toMatchObject({ status: 'unreachable', error: 'Bridge request timed out' })
    await vi.advanceTimersByTimeAsync(800)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot().status).toBe('connected')
    bridge.stop()
  })

  it('reports conversion exceptions without changing status or losing the next poll', async () => {
    const badIntake = { get drawing() { throw new Error('cannot convert') } }
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload({ intake: badIntake })))
      .mockResolvedValue(response(payload()))
    const onStatus = vi.fn(), onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onStatus, onUpdate })
    bridge.start(); await flush()
    expect(onStatus.mock.calls.at(-1)[0]).toMatchObject({ status: 'connecting', error: 'cannot convert' })
    expect(onUpdate).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(400)
    expect(onUpdate).toHaveBeenCalledTimes(1)
    bridge.stop()
  })

  it('rejects malformed payloads and ignores lower revisions until the epoch changes', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({ rev: 1, epoch: 'boot-a' }))
      .mockResolvedValueOnce(response(payload()))
      .mockResolvedValueOnce(response(payload({ rev: 6, drawing: 'stale.dwg' })))
      .mockResolvedValue(response(payload({ rev: 1, epoch: 'boot-b' })))
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onUpdate })
    bridge.start(); await flush()
    expect(bridge.snapshot().status).toBe('unreachable')
    await vi.advanceTimersByTimeAsync(800)
    await vi.advanceTimersByTimeAsync(400)
    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(bridge.snapshot()).toMatchObject({ rev: 7, drawing: 'site.dwg' })
    await vi.advanceTimersByTimeAsync(400)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot()).toMatchObject({ rev: 1, epoch: 'boot-b' })
    bridge.stop()
  })
})
