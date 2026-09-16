import { useState } from 'react'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STUDIO_MOTION, useLeavingGround, useStudioTransition } from './useStudioTransition.js'

const isDrafting = (id) => id === 'cad' || id === 'solar'
const advance = (ms) => act(() => vi.advanceTimersByTime(ms))
function transition(committed = 'cad', reducedMotion = false) {
  const onCommit = vi.fn()
  const hook = renderHook((props) => useStudioTransition(props), {
    initialProps: { committed, onCommit, isDrafting, reducedMotion },
  })
  return { ...hook, onCommit, request: (target) => act(() => hook.result.current.request(target)) }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('studio chrome transitions', () => {
  it("an unrelated re-render with the unchanged prop does not erase the hook's own commit", () => {
    const onCommit = vi.fn()
    const { result, rerender } = renderHook(({ committed }) => useStudioTransition({
      committed, onCommit, isDrafting, reducedMotion: false,
    }), { initialProps: { committed: 'cad' } })
    act(() => result.current.request('browser'))
    expect(result.current.phase).toBe('out')
    advance(STUDIO_MOTION.chromeOutMs)
    expect(onCommit.mock.calls).toEqual([['browser']])
    rerender({ committed: 'cad' })
    act(() => result.current.request('cad'))
    expect(onCommit.mock.calls).toEqual([['browser'], ['cad']])
    expect(result.current.phase).toBe('in')
    expect(vi.getTimerCount()).toBe(1)
  })

  it("the parent's rendered value wins once it changes", () => {
    const onCommit = vi.fn()
    const { result, rerender } = renderHook(({ committed }) => useStudioTransition({
      committed, onCommit, isDrafting, reducedMotion: false,
    }), { initialProps: { committed: 'cad' } })
    act(() => result.current.request('browser'))
    advance(STUDIO_MOTION.chromeOutMs)
    expect(onCommit.mock.calls).toEqual([['browser']])
    rerender({ committed: 'solar' })
    act(() => result.current.request('solar'))
    expect(onCommit.mock.calls).toEqual([['browser']])
    act(() => result.current.request('cad'))
    expect(onCommit.mock.calls).toEqual([['browser'], ['cad']])
    expect(result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('exitPending reads true only while an exit is pending', () => {
    const { result } = renderHook(() => {
      const [committed, setCommitted] = useState('cad')
      return useStudioTransition({ committed, onCommit: setCommitted, isDrafting, reducedMotion: false })
    })
    const exitPending = result.current.exitPending
    expect(exitPending()).toBe(false)
    act(() => {
      result.current.request('browser')
      expect(exitPending()).toBe(true)
    })
    act(() => result.current.settle())
    expect(exitPending()).toBe(false)
    act(() => result.current.request('cad'))
    expect(result.current.phase).toBe('in')
    expect(exitPending()).toBe(false)
    act(() => {
      result.current.request('browser')
      expect(exitPending()).toBe(true)
      result.current.request('cad')
      expect(exitPending()).toBe(false)
    })
    expect(result.current.phase).toBe('idle')
    expect(result.current.exitPending).toBe(exitPending)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps Browser as the latest batched request after the CAD exit', () => {
    const onCommit = vi.fn()
    const { result } = renderHook(() => {
      const [committed, setCommitted] = useState('browser')
      const transition = useStudioTransition({
        committed,
        onCommit: (target) => { onCommit(target); setCommitted(target) },
        isDrafting,
        reducedMotion: false,
      })
      return { ...transition, committed }
    })
    act(() => {
      result.current.request('cad')
      result.current.request('browser')
    })
    expect(result.current.committed).toBe('cad')
    expect(onCommit.mock.calls).toEqual([['cad']])
    expect(result.current.phase).toBe('out')
    expect(vi.getTimerCount()).toBe(1)

    act(() => vi.advanceTimersByTime(STUDIO_MOTION.chromeOutMs))
    expect(result.current.committed).toBe('browser')
    expect(onCommit.mock.calls).toEqual([['cad'], ['browser']])
    expect(result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([
    ['cad', 'solar', false],
    ['cad', 'browser', true],
  ])('keeps the latest request in one batch from %s through %s (reduced motion: %s)', (initial, intermediate, reducedMotion) => {
    const onCommit = vi.fn()
    const { result } = renderHook(() => {
      const [committed, setCommitted] = useState(initial)
      const transition = useStudioTransition({
        committed,
        onCommit: (target) => { onCommit(target); setCommitted(target) },
        isDrafting,
        reducedMotion,
      })
      return { ...transition, committed }
    })
    act(() => {
      result.current.request(intermediate)
      result.current.request(initial)
    })
    expect(result.current.committed).toBe(initial)
    expect(onCommit.mock.calls).toEqual([[intermediate], [initial]])
    expect(result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('exports the frozen motion contract', () => {
    expect(STUDIO_MOTION).toEqual({ chromeOutMs: 80, chromeInMs: 140, groundMs: 180, easing: 'cubic-bezier(.2,0,0,1)' })
    expect(Object.isFrozen(STUDIO_MOTION)).toBe(true)
  })
  it('fades CAD out before committing Browser at 80 ms', () => {
    const hook = transition()
    hook.request('browser')
    expect(hook.result.current.phase).toBe('out')
    advance(79)
    expect(hook.onCommit).not.toHaveBeenCalled()
    advance(1)
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith('browser')
    expect(hook.result.current.phase).toBe('idle')
  })
  it('commits CAD immediately then fades in for 140 ms', () => {
    const hook = transition('browser')
    hook.request('cad')
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith('cad')
    expect(hook.result.current.phase).toBe('in')
    advance(139)
    expect(hook.result.current.phase).toBe('in')
    advance(1)
    expect(hook.result.current.phase).toBe('idle')
  })
  it.each([['cad', 'solar'], ['solar', 'cad']])('commits %s to %s without chrome timers', (from, target) => {
    const hook = transition(from)
    hook.request(target)
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith(target)
    expect(hook.result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })
  it.each([['browser', 'ios'], ['ios', 'browser']])('fades the distinct %s and %s chrome with the ground settled by 260 ms', (from, target) => {
    const hook = transition(from)
    hook.request(target)
    expect(hook.result.current.phase).toBe('out')
    advance(79)
    expect(hook.onCommit).not.toHaveBeenCalled()
    advance(1)
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith(target)
    expect(hook.result.current.phase).toBe('in')
    advance(139)
    expect(hook.result.current.phase).toBe('in')
    advance(41)
    expect(hook.result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })
  it('cancels a Browser to iOS exit when Browser is the latest request', () => {
    const hook = transition('browser')
    hook.request('ios')
    advance(40)
    hook.request('browser')
    advance(260)
    expect(hook.onCommit).not.toHaveBeenCalled()
    expect(hook.result.current.phase).toBe('idle')
  })
  it('ignores a request for the committed profile', () => {
    const hook = transition()
    hook.request('cad')
    expect(hook.onCommit).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('cancels a pending exit when the user returns to CAD', () => {
    const hook = transition()
    hook.request('browser')
    advance(40)
    hook.request('cad')
    expect(hook.result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
    advance(500)
    expect(hook.onCommit).not.toHaveBeenCalled()
  })
  it('replaces and restarts a pending exit', () => {
    const hook = transition()
    hook.request('browser')
    advance(40)
    hook.request('ios')
    advance(79)
    expect(hook.onCommit).not.toHaveBeenCalled()
    advance(1)
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith('ios')
    expect(hook.result.current.phase).toBe('idle')
  })
  it('settles the pending commit immediately for a drawing action', () => {
    const hook = transition()
    hook.request('browser')
    advance(30)
    act(() => hook.result.current.settle())
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith('browser')
    expect(hook.result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })
  it('ends an entrance before processing the next request', () => {
    const hook = transition('browser')
    hook.request('cad')
    hook.rerender({ committed: 'cad', onCommit: hook.onCommit, isDrafting, reducedMotion: false })
    advance(30)
    hook.request('browser')
    expect(hook.result.current.phase).toBe('out')
    advance(80)
    expect(hook.onCommit.mock.calls).toEqual([['cad'], ['browser']])
    expect(hook.result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })
  it.each([['cad', 'browser'], ['browser', 'cad'], ['cad', 'solar'], ['browser', 'ios']])('reduces %s to %s to a synchronous commit', (from, target) => {
    const hook = transition(from, true)
    hook.request(target)
    expect(hook.onCommit).toHaveBeenCalledExactlyOnceWith(target)
    expect(hook.result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })
  it('reads the default media preference at request time', () => {
    let reduce = false
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: reduce })))
    const onCommit = vi.fn()
    const { result } = renderHook(() => useStudioTransition({ committed: 'cad', onCommit, isDrafting }))
    reduce = true
    act(() => result.current.request('browser'))
    expect(onCommit).toHaveBeenCalledExactlyOnceWith('browser')
    expect(result.current.phase).toBe('idle')
    expect(vi.getTimerCount()).toBe(0)
  })
  it('never commits after unmount', () => {
    const hook = transition()
    hook.request('browser')
    hook.unmount()
    expect(vi.getTimerCount()).toBe(0)
    advance(500)
    expect(hook.onCommit).not.toHaveBeenCalled()
  })
})

describe('leaving ground', () => {
  it('retains the previous ground for 180 ms', () => {
    const hook = renderHook((ground) => useLeavingGround(ground, { reducedMotion: false }), { initialProps: 'drawing' })
    expect(hook.result.current).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
    hook.rerender('board')
    expect(hook.result.current).toBe('drawing')
    advance(179)
    expect(hook.result.current).toBe('drawing')
    advance(1)
    expect(hook.result.current).toBeNull()
  })
  it('replaces the older ground and restarts the fade', () => {
    const hook = renderHook((ground) => useLeavingGround(ground, { reducedMotion: false }), { initialProps: 'drawing' })
    hook.rerender('board')
    advance(100)
    hook.rerender('device-stage')
    expect(hook.result.current).toBe('board')
    advance(179)
    expect(hook.result.current).toBe('board')
    advance(1)
    expect(hook.result.current).toBeNull()
  })
  it('does not fade from null or under reduced motion', () => {
    const hook = renderHook(({ ground, reducedMotion }) => useLeavingGround(ground, { reducedMotion }), {
      initialProps: { ground: null, reducedMotion: false },
    })
    hook.rerender({ ground: 'drawing', reducedMotion: false })
    expect(hook.result.current).toBeNull()
    hook.rerender({ ground: 'board', reducedMotion: true })
    expect(hook.result.current).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('clears the fade timer on unmount', () => {
    const hook = renderHook((ground) => useLeavingGround(ground), { initialProps: 'drawing' })
    hook.rerender('board')
    hook.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
