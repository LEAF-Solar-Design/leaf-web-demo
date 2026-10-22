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

  it('never loads text support for geometry-only payloads', async () => {
    const loadTextGlyphs = vi.fn()
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl: vi.fn().mockResolvedValue(response(payload())),
      loadTextGlyphs, onUpdate })
    bridge.start(); await flush()
    await vi.advanceTimersByTimeAsync(400)
    expect(loadTextGlyphs).not.toHaveBeenCalled()
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(onUpdate.mock.calls[0][0].intake.polylines).toHaveLength(1)
    bridge.stop()
  })

  it('loads text support once and seats glyphs on successive payloads', async () => {
    const helper = await import('./bridgeText.js')
    const loadTextGlyphs = vi.fn().mockResolvedValue(helper)
    const intake = { ...payload().intake,
      texts: [{ handle: 'text', text: 'PV', pt: [2, 2], height: 1 }] }
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl: vi.fn().mockResolvedValue(response(payload({ intake }))),
      loadTextGlyphs, onUpdate })
    bridge.start(); await flush(); await flush()
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    expect(onUpdate.mock.calls[0][0].intake.polylines.some(row => row.handle === 'text' && row.strokeOnly)).toBe(true)
    await vi.advanceTimersByTimeAsync(400)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    bridge.stop()
  })

  it('seats geometry and keeps polling after a text load failure, retrying only after 30 seconds', async () => {
    const helper = await import('./bridgeText.js')
    const loadTextGlyphs = vi.fn().mockRejectedValueOnce(new Error('font unavailable')).mockResolvedValue(helper)
    const intake = { ...payload().intake, texts: [{ text: 'PV', pt: [0, 0], height: 1 }] }
    const fetchImpl = vi.fn().mockResolvedValue(response(payload({ intake })))
    const onUpdate = vi.fn(), onStatus = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, loadTextGlyphs, onUpdate, onStatus, intervalMs: 1000 })
    bridge.start(); await flush(); await flush()
    expect(onStatus.mock.calls.map(call => call[0])).toContainEqual(expect.objectContaining({
      status: 'connecting', error: 'font unavailable',
    }))
    expect(bridge.snapshot()).toMatchObject({ status: 'connected', error: 'font unavailable', rev: 7 })
    expect(onUpdate.mock.calls[0][0].intake).toMatchObject({ textPending: 1, points: 2 })
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(28999)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot()).toMatchObject({ status: 'connected', error: null })
    expect(onUpdate.mock.calls.at(-1)[0].intake.polylines.some(row => row.strokeOnly)).toBe(true)
    bridge.stop()
  })

  it('shares a pending text load across stop/start and ignores the stopped poll', async () => {
    const helper = await import('./bridgeText.js')
    let finish
    const loadTextGlyphs = vi.fn(() => new Promise(resolve => { finish = resolve }))
    const intake = { texts: [{ text: 'PV', pt: [0, 0], height: 1 }] }
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl: vi.fn().mockResolvedValue(response(payload({ intake }))),
      loadTextGlyphs, onUpdate })
    bridge.start(); await flush()
    bridge.stop(); bridge.start(); await flush()
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    expect(onUpdate).not.toHaveBeenCalled()
    finish(helper); await flush(); await flush()
    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(onUpdate.mock.calls[0][0].intake.polylines.some(row => row.strokeOnly)).toBe(true)
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
    expect(bridge.snapshot().error).toBeNull()
    bridge.stop()
  })

  it.each(['offline', 'busy', 'unreachable', 'epoch'])('reports %s loss and recovers with a full intake at the same revision', async loss => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload()))
    if (loss === 'unreachable') fetchImpl.mockRejectedValueOnce(new Error('bridge down'))
    else fetchImpl.mockResolvedValueOnce(response(payload({ intake: undefined, unchanged: true,
      ...(loss === 'epoch' ? { epoch: 'boot-b' } : { status: loss }) })))
    fetchImpl.mockResolvedValue(response(payload({ epoch: loss === 'epoch' ? 'boot-b' : 'boot-a' })))
    const onLoss = vi.fn(), onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onLoss, onUpdate })
    bridge.start(); await flush()
    await vi.advanceTimersByTimeAsync(400)
    expect(onLoss).toHaveBeenCalledTimes(1)
    expect(bridge.snapshot()).toMatchObject({ rev: 0, epoch: '' })
    await vi.advanceTimersByTimeAsync(loss === 'epoch' ? 400 : 800)
    expect(fetchImpl.mock.calls[2][0]).toContain('since=0&epoch=')
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(onUpdate.mock.calls.at(-1)[0]).toMatchObject({ rev: 7, intake: { source: 'bridge' } })
    bridge.stop()
  })

  it('keeps a conversion error visible after connecting until a good update clears it', async () => {
    const badIntake = { get drawing() { throw new Error('cannot convert update') } }
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload()))
      .mockResolvedValueOnce(response(payload({ rev: 8, intake: badIntake })))
      .mockResolvedValueOnce(response(payload({ unchanged: true, intake: undefined })))
      .mockResolvedValue(response(payload({ rev: 8 })))
    const onUpdate = vi.fn(), onStatus = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onUpdate, onStatus })
    bridge.start(); await flush()
    await vi.advanceTimersByTimeAsync(400)
    expect(onStatus.mock.calls.at(-1)[0]).toMatchObject({ status: 'connected', error: 'cannot convert update' })
    expect(onUpdate).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(400)
    expect(bridge.snapshot().error).toBe('cannot convert update')
    await vi.advanceTimersByTimeAsync(400)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot()).toMatchObject({ status: 'connected', error: null, rev: 8 })
    bridge.stop()
  })

  it('accepts a full unchanged recovery intake even when the bridge uses an empty epoch', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload({ epoch: '' })))
      .mockResolvedValueOnce(response(payload({ epoch: '', status: 'offline', intake: undefined })))
      .mockResolvedValue(response(payload({ epoch: '', unchanged: true })))
    const onLoss = vi.fn(), onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, onLoss, onUpdate })
    bridge.start(); await flush()
    await vi.advanceTimersByTimeAsync(400)
    expect(onLoss).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(800)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(bridge.snapshot()).toMatchObject({ rev: 7, epoch: '' })
    bridge.stop()
  })

  it('retries failed text on an unchanged drawing after 30 seconds and clears the reported error', async () => {
    const helper = await import('./bridgeText.js')
    const loadTextGlyphs = vi.fn().mockRejectedValueOnce(new Error('text unavailable')).mockResolvedValue(helper)
    const intake = { ...payload().intake, texts: [{ handle: 'label', text: 'PV', pt: [0, 0], height: 1 }] }
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload({ intake })))
      .mockResolvedValue(response(payload({ unchanged: true, intake: undefined })))
    const onUpdate = vi.fn(), onStatus = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, loadTextGlyphs, onUpdate, onStatus, intervalMs: 1000 })
    bridge.start(); await flush(); await flush()
    expect(bridge.snapshot()).toMatchObject({ error: 'text unavailable', textPending: true })
    await vi.advanceTimersByTimeAsync(29999)
    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(2)
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(onUpdate.mock.calls[1][0]).toMatchObject({ epoch: 'boot-a', rev: 7 })
    expect(onUpdate.mock.calls[1][0].intake.polylines.some(row => row.handle === 'label' && row.strokeOnly)).toBe(true)
    expect(onStatus.mock.calls.at(-1)[0]).toMatchObject({ error: null, textPending: false })
    bridge.stop()
  })

  it('does not replay pending labels from an older revision over a failed newer update', async () => {
    const loadTextGlyphs = vi.fn().mockRejectedValue(new Error('labels unavailable'))
    const intake = { ...payload().intake, texts: [{ text: 'PV', pt: [0, 0], height: 1 }] }
    const badIntake = { get drawing() { throw new Error('cannot convert new revision') } }
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(payload({ intake })))
      .mockResolvedValueOnce(response(payload({ rev: 8, intake: badIntake })))
      .mockResolvedValue(response(payload({ rev: 8, unchanged: true, intake: undefined })))
    const onUpdate = vi.fn()
    const bridge = createLocalCadBridge({ fetchImpl, loadTextGlyphs, onUpdate, intervalMs: 1000 })
    bridge.start(); await flush(); await flush()
    await vi.advanceTimersByTimeAsync(31000)
    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(loadTextGlyphs).toHaveBeenCalledTimes(1)
    expect(bridge.snapshot().error).toBe('cannot convert new revision')
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
