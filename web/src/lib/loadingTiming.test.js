// @vitest-environment jsdom
import { StrictMode, createElement } from 'react'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { loadingPhaseAt, useLoadingPhase } from './loadingTiming.js'

describe('loadingPhaseAt', () => {
  it('stays hidden below 200 ms and shows at the boundary', () => {
    for (let elapsed = 0; elapsed < 200; elapsed += 1) {
      expect(loadingPhaseAt(1000, 1000 + elapsed)).toBe('hidden')
    }
    expect(loadingPhaseAt(1000, 1200)).toBe('shown')
  })

  it('holds a shown state for 400 ms even when work has finished', () => {
    expect(loadingPhaseAt(0, 200, 250)).toBe('shown')
    expect(loadingPhaseAt(0, 250, 250)).toBe('shown')
    expect(loadingPhaseAt(0, 599, 250)).toBe('shown')
    expect(loadingPhaseAt(0, 600, 250)).toBe('done')
    expect(loadingPhaseAt(0, 800, 800)).toBe('done')
  })

  it('turns long at 10 seconds from the start of work', () => {
    expect(loadingPhaseAt(1000, 10999)).toBe('shown')
    expect(loadingPhaseAt(1000, 11000)).toBe('long')
    expect(loadingPhaseAt(1000, 20000)).toBe('long')
    expect(loadingPhaseAt(1000, 20000, 20000)).toBe('done')
  })

  it.each([0, 1, 50, 99, 199])('never shows work completed at %i ms', (doneMs) => {
    expect(loadingPhaseAt(0, doneMs, doneMs)).toBe('done')
    expect(loadingPhaseAt(0, 200, doneMs)).toBe('done')
    expect(loadingPhaseAt(0, 10000, doneMs)).toBe('done')
  })

  it('handles no cycle and custom timing boundaries', () => {
    expect(loadingPhaseAt(null, 10000)).toBe('hidden')
    const timing = { showAfterMs: 50, minShowMs: 100, longAfterMs: 120 }
    expect(loadingPhaseAt(0, 49, null, timing)).toBe('hidden')
    expect(loadingPhaseAt(0, 50, null, timing)).toBe('shown')
    expect(loadingPhaseAt(0, 120, null, timing)).toBe('long')
    expect(loadingPhaseAt(0, 149, 60, timing)).toBe('shown')
    expect(loadingPhaseAt(0, 150, 60, timing)).toBe('done')
  })
})

describe('useLoadingPhase', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  const advance = (ms) => act(() => vi.advanceTimersByTime(ms))

  it('shows after 200 ms, stays visible for 400 ms, then finishes', () => {
    const { result, rerender } = renderHook(({ loading }) => useLoadingPhase(loading), {
      initialProps: { loading: true },
    })
    expect(result.current).toBe('hidden')
    advance(199)
    expect(result.current).toBe('hidden')
    advance(1)
    expect(result.current).toBe('shown')
    advance(50)
    rerender({ loading: false })
    expect(result.current).toBe('shown')
    advance(349)
    expect(result.current).toBe('shown')
    advance(1)
    expect(result.current).toBe('done')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('turns long at 10 seconds and finishes immediately after the minimum', () => {
    const { result, rerender } = renderHook(({ loading }) => useLoadingPhase(loading), {
      initialProps: { loading: true },
    })
    advance(9999)
    expect(result.current).toBe('shown')
    advance(1)
    expect(result.current).toBe('long')
    rerender({ loading: false })
    expect(result.current).toBe('done')
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([0, 1, 50, 99, 199])('never flashes when work ends at %i ms', (duration) => {
    const observed = []
    const { result, rerender } = renderHook(({ loading }) => {
      const phase = useLoadingPhase(loading)
      observed.push(phase)
      return phase
    }, { initialProps: { loading: true } })
    advance(duration)
    rerender({ loading: false })
    expect(result.current).toBe('done')
    advance(20000)
    expect(result.current).toBe('done')
    expect(observed).not.toContain('shown')
    expect(observed).not.toContain('long')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('starts a fresh cycle after completion and uses custom durations', () => {
    const timing = { showAfterMs: 50, minShowMs: 100, longAfterMs: 500 }
    const { result, rerender } = renderHook(({ loading }) => useLoadingPhase(loading, timing), {
      initialProps: { loading: false },
    })
    expect(result.current).toBe('hidden')
    rerender({ loading: true })
    advance(50)
    expect(result.current).toBe('shown')
    rerender({ loading: false })
    advance(99)
    expect(result.current).toBe('shown')
    advance(1)
    expect(result.current).toBe('done')
    rerender({ loading: true })
    expect(result.current).toBe('hidden')
    advance(499)
    expect(result.current).toBe('shown')
    advance(1)
    expect(result.current).toBe('long')
  })

  it('keeps the full minimum when the display timer runs late', () => {
    const { result, rerender } = renderHook(({ loading }) => useLoadingPhase(loading), {
      initialProps: { loading: true },
    })
    vi.setSystemTime(300)
    advance(200)
    expect(result.current).toBe('shown')
    rerender({ loading: false })
    advance(399)
    expect(result.current).toBe('shown')
    advance(1)
    expect(result.current).toBe('done')
  })

  it('keeps an existing indicator visible when work restarts during its minimum', () => {
    const { result, rerender } = renderHook(({ loading }) => useLoadingPhase(loading), {
      initialProps: { loading: true },
    })
    advance(200)
    expect(result.current).toBe('shown')
    advance(50)
    rerender({ loading: false })
    rerender({ loading: true })
    expect(result.current).toBe('shown')
    advance(50)
    rerender({ loading: false })
    advance(299)
    expect(result.current).toBe('shown')
    advance(1)
    expect(result.current).toBe('done')
    advance(10000)
    expect(result.current).toBe('done')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('cleans up timers in StrictMode and on unmount', () => {
    const wrapper = ({ children }) => createElement(StrictMode, null, children)
    const { result, unmount } = renderHook(() => useLoadingPhase(true), { wrapper })
    expect(vi.getTimerCount()).toBe(1)
    advance(200)
    expect(result.current).toBe('shown')
    expect(vi.getTimerCount()).toBe(1)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
