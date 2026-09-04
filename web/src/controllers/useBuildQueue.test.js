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
    expect(result.current.warnings).toEqual(['builds: poll failed'])
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
