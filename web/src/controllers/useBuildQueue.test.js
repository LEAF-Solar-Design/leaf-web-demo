// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import useBuildQueue from './useBuildQueue.js'

const good = (id, state = 'running') => ({
  id, lane: 'fleet', state, title: id, requested_by: null, started: null, elapsed_ms: null,
  estimate_ms: null, cost_usd: null, receipts: [], terminal: { verified: false, promoted: false },
  actions: [], status: { word: state, tint: 'ok', detail: null },
})

afterEach(() => vi.useRealTimers())

describe('useBuildQueue', () => {
  it('makes no request in mock mode', async () => {
    const listBuilds = vi.fn()
    const { result } = renderHook(() => useBuildQueue({ mock: true, services: { listBuilds } }))
    await act(async () => {})
    expect(listBuilds).not.toHaveBeenCalled()
    expect(result.current.builds).toEqual([])
    expect(result.current.runningCount).toBe(0)
  })

  it('validates every record, drops the malformed ones with a warning, and counts the open ones', async () => {
    const listBuilds = vi.fn().mockResolvedValue({
      builds: [good('a'), { id: 'b', lane: 'ci' }, good('c', 'done'), good('d', 'queued')],
      warnings: ['fleet: gateway not configured', 42, 'x'.repeat(300)],
    })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 60_000, services: { listBuilds } }))
    await waitFor(() => expect(result.current.builds).toHaveLength(3))
    expect(result.current.builds.map((r) => r.id)).toEqual(['a', 'c', 'd'])
    expect(result.current.dropped).toBe(1)
    expect(result.current.warnings).toEqual(['fleet: gateway not configured', 'builds: 1 malformed record(s) dropped'])
    expect(result.current.runningCount).toBe(2)
    expect(listBuilds).toHaveBeenCalledWith(undefined, 20)
  })

  it('keeps the last good list when a poll fails and stops on 401 until resumed', async () => {
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockResolvedValueOnce({ builds: [good('a')] })
      .mockRejectedValueOnce(Object.assign(new Error('GET /api/builds -> 401'), { status: 401 }))
      .mockResolvedValue({ builds: [good('z')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.builds.map((r) => r.id)).toEqual(['a'])
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); await Promise.resolve() })
    expect(result.current.builds.map((r) => r.id)).toEqual(['a'])
    expect(result.current.warnings).toEqual(['builds: sign-in required'])
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve() })
    expect(listBuilds).toHaveBeenCalledTimes(2)
    await act(async () => { result.current.resume() })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(listBuilds).toHaveBeenCalledTimes(3)
    expect(result.current.builds.map((r) => r.id)).toEqual(['z'])
  })

  it('a body without a builds array is an empty list, not a crash', async () => {
    const listBuilds = vi.fn().mockResolvedValue({ nope: true })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 60_000, services: { listBuilds } }))
    await waitFor(() => expect(listBuilds).toHaveBeenCalled())
    await act(async () => {})
    expect(result.current.builds).toEqual([])
    expect(result.current.warnings).toEqual(['builds: 1 malformed record(s) dropped'])
  })
})

// W6-E01: the feed status the rail reads to say when its cards may be stale.
const flush = () => act(async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve() })
const failWith = (status) => Object.assign(new Error(`GET /api/builds -> ${status}`), { status })

describe('useBuildQueue feed status', () => {
  it('E01 row1 a good poll reads status live', async () => {
    // Starts from a failure so the good poll must actively set 'live'.
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockRejectedValueOnce(failWith(503))
      .mockResolvedValue({ builds: [good('a')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    expect(result.current.status).toBe('stale')
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.builds).toHaveLength(1)
    expect(result.current.status).toBe('live')
  })

  it('E01 row2 a 503 after a good poll keeps the builds, reads stale and warns builds: poll failed', async () => {
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockResolvedValueOnce({ builds: [good('a')] })
      .mockRejectedValueOnce(failWith(503))
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    expect(result.current.status).toBe('live')
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.builds.map((r) => r.id)).toEqual(['a'])
    expect(result.current.status).toBe('stale')
    expect(result.current.warnings).toEqual(['builds: poll failed'])
  })

  it('E01 row3 resume after stale polls again at once and reads live', async () => {
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockResolvedValueOnce({ builds: [good('a')] })
      .mockRejectedValueOnce(failWith(503))
      .mockResolvedValue({ builds: [good('b')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(result.current.status).toBe('stale')
    await act(async () => { result.current.resume() })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(3)
    expect(result.current.status).toBe('live')
    expect(result.current.builds.map((r) => r.id)).toEqual(['b'])
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(4)
  })

  it('E01 row4 a 401 reads auth, warns builds: sign-in required, and makes no further list call', async () => {
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockResolvedValueOnce({ builds: [good('a')] })
      .mockRejectedValueOnce(failWith(401))
      .mockResolvedValue({ builds: [good('z')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(result.current.status).toBe('auth')
    expect(result.current.warnings).toEqual(['builds: sign-in required'])
    expect(result.current.builds.map((r) => r.id)).toEqual(['a'])
    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('auth')
  })

  it('E01 row5 resume after auth polls again', async () => {
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockResolvedValueOnce({ builds: [good('a')] })
      .mockRejectedValueOnce(failWith(401))
      .mockResolvedValue({ builds: [good('z')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(result.current.status).toBe('auth')
    await act(async () => { result.current.resume() })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(3)
    expect(result.current.status).toBe('live')
    expect(result.current.builds.map((r) => r.id)).toEqual(['z'])
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(4)
  })

  it('E01 row6 malformed records count as dropped while status stays live', async () => {
    // Starts from a failure so the partly malformed poll must actively set 'live'.
    vi.useFakeTimers()
    const listBuilds = vi.fn()
      .mockRejectedValueOnce(failWith(503))
      .mockResolvedValue({ builds: [good('a'), { id: 'b', lane: 'ci' }] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    expect(result.current.status).toBe('stale')
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(result.current.builds).toHaveLength(1)
    expect(result.current.dropped).toBe(1)
    expect(result.current.status).toBe('live')
  })

  it('E01 row7 mock mode reads idle and never calls the list service', async () => {
    const listBuilds = vi.fn()
    const { result } = renderHook(() => useBuildQueue({ mock: true, services: { listBuilds } }))
    await act(async () => {})
    expect(result.current.status).toBe('idle')
    expect(listBuilds).not.toHaveBeenCalled()
  })

  // Out-of-order responses: request A is still pending when the interval
  // starts request B; whichever resolves last, only the newer result counts.
  const deferred = () => {
    let resolve
    let reject
    const promise = new Promise((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }

  it('E01 row15 a success that resolves after a newer 401 does not clear auth and leaves no interval', async () => {
    vi.useFakeTimers()
    const a = deferred()
    const listBuilds = vi.fn()
      .mockImplementationOnce(() => a.promise)
      .mockRejectedValueOnce(failWith(401))
      .mockResolvedValue({ builds: [good('z')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('auth')
    await act(async () => { a.resolve({ builds: [good('a')] }) })
    await flush()
    expect(result.current.status).toBe('auth')
    expect(result.current.warnings).toEqual(['builds: sign-in required'])
    expect(result.current.builds).toEqual([])
    await act(async () => { vi.advanceTimersByTime(3000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('auth')
  })

  it('E01 row16 an older 503 that resolves after a newer success leaves live', async () => {
    vi.useFakeTimers()
    const a = deferred()
    const listBuilds = vi.fn()
      .mockImplementationOnce(() => a.promise)
      .mockResolvedValue({ builds: [good('b')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.builds.map((r) => r.id)).toEqual(['b'])
    expect(result.current.status).toBe('live')
    await act(async () => { a.reject(failWith(503)) })
    await flush()
    expect(result.current.status).toBe('live')
    expect(result.current.warnings).not.toContain('builds: poll failed')
    expect(result.current.builds.map((r) => r.id)).toEqual(['b'])
  })

  it('E01 row17 an older success that resolves after a newer 503 leaves stale', async () => {
    vi.useFakeTimers()
    const a = deferred()
    const listBuilds = vi.fn()
      .mockImplementationOnce(() => a.promise)
      .mockRejectedValue(failWith(503))
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('stale')
    await act(async () => { a.resolve({ builds: [good('a')] }) })
    await flush()
    expect(result.current.status).toBe('stale')
    expect(result.current.warnings).toEqual(['builds: poll failed'])
    expect(result.current.builds).toEqual([])
  })

  // Row 15's late success is also OLDER than the 401, so the sequence check
  // alone rejects it. Here the late success is NEWER than the applied 401, so
  // only the paused term keeps it out.
  it('E01 row19 an applied 401 pauses the generation so a newer pending success cannot apply', async () => {
    vi.useFakeTimers()
    const a = deferred()
    const b = deferred()
    const listBuilds = vi.fn()
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise)
      .mockResolvedValue({ builds: [good('z')] })
    const { result } = renderHook(() => useBuildQueue({ mock: false, pollIntervalMs: 1000, services: { listBuilds } }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    await act(async () => { a.reject(failWith(401)) })
    await flush()
    expect(result.current.status).toBe('auth')
    expect(result.current.warnings).toEqual(['builds: sign-in required'])
    await act(async () => { b.resolve({ builds: [good('b')] }) })
    await flush()
    expect(result.current.status).toBe('auth')
    expect(result.current.warnings).toEqual(['builds: sign-in required'])
    expect(result.current.builds).toEqual([])
    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()
    expect(listBuilds).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('auth')
  })
})
