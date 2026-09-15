// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import useDrawingViewport, { computeSafeRect } from './useDrawingViewport.js'

const rect = (left, top, width, height) => ({ left, top, width, height })
const canvas = rect(0, 0, 1920, 940)
const cover = (edge, ...values) => ({ edge, rect: rect(...values) })
const chrome = [cover('top', 0, 0, 1920, 28), cover('top', 0, 28, 1920, 95),
  cover('top', 0, 123, 1920, 32), cover('top', 250, 155, 1670, 26),
  cover('left', 0, 155, 250, 754), cover('bottom', 600, 880, 720, 25), cover('bottom', 0, 909, 1920, 31)]

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('computeSafeRect', () => {
  it.each([
    [50, 798], [undefined, 848], [0, 848], [-10, 848], [NaN, 848], [Infinity, 848],
  ])('reserves valid bottom space (%s)', (reserve, height) => {
    expect(computeSafeRect(canvas, [{ ...cover('bottom', 600, 880, 720, 25), reserve }])).toEqual(rect(16, 16, 1888, height))
  })
  it.each([
    [380, 50, rect(16, 396, 768, 28)],
    [380, 10, rect(16, 396, 768, 18)],
    [480, 50, null],
  ])('uses reserves only when a safe rectangle remains (top %s, reserve %s)', (topHeight, reserve, expected) => {
    expect(computeSafeRect(rect(0, 0, 800, 500), [
      cover('top', 0, 0, 800, topHeight),
      { ...cover('bottom', 200, 440, 400, 25), reserve },
    ])).toEqual(expected)
  })
  it('reserves space toward the interior after resolving the side', () => {
    expect(computeSafeRect(canvas, [{ ...cover('top', 0, 0, 1920, 28), reserve: 10 }])).toEqual(rect(16, 54, 1888, 870))
    expect(computeSafeRect(canvas, [{ ...cover('nearest', 0, 400, 100, 100), reserve: 20 }])).toEqual(rect(136, 16, 1768, 908))
  })
  it('measures full-bleed chrome, a closed pane and a wrapped prompt', () => {
    expect(computeSafeRect(canvas, chrome)).toEqual(rect(266, 197, 1638, 667))
    expect(computeSafeRect(canvas, chrome.filter(({ edge }) => edge !== 'left'))).toEqual(rect(16, 197, 1888, 667))
    expect(computeSafeRect(canvas, [...chrome, cover('bottom', 600, 830, 720, 50)])).toEqual(rect(266, 197, 1638, 617))
  })
  it('returns canvas-local coordinates for the current inset ground', () => {
    expect(computeSafeRect(rect(250, 155, 1670, 754), chrome)).toEqual(rect(16, 42, 1638, 667))
  })
  it('resolves the activity rail to the nearest edge', () => {
    expect(computeSafeRect(canvas, [cover('nearest', 1700, 300, 220, 500)])).toEqual(rect(16, 16, 1668, 908))
  })
  it('skips empty and outside occluders and rejects a collapsed safe area', () => {
    expect(computeSafeRect(canvas, [cover('top', 0, 0, 0, 100), cover('left', 2000, 0, 10, 100)])).toEqual(rect(16, 16, 1888, 908))
    expect(computeSafeRect(canvas, [...chrome, cover('bottom', 600, 200, 720, 25)])).toBeNull()
  })
})

describe('useDrawingViewport', () => {
  it('coalesces resize notifications, ignores pointers and removes observers and listeners', () => {
    document.body.innerHTML = '<main class="center-scroll"><div class="studio-shell"><div id="ground"><div class="viewer-canvas"><canvas></canvas></div></div><div class="bar-dock"></div></div></main>'
    const root = document.querySelector('#ground')
    const canvasElement = root.querySelector('canvas')
    const dock = document.querySelector('.bar-dock')
    const measure = vi.spyOn(canvasElement, 'getBoundingClientRect').mockReturnValue(canvas)
    vi.spyOn(dock, 'getBoundingClientRect').mockReturnValue(rect(600, 880, 720, 25))
    let notify
    const disconnect = vi.fn(), observe = vi.fn()
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback) { notify = callback }
      observe = observe
      unobserve = vi.fn()
      disconnect = disconnect
    })
    const frames = new Map()
    let id = 0
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback) => { frames.set(++id, callback); return id }))
    vi.stubGlobal('cancelAnimationFrame', vi.fn((key) => frames.delete(key)))
    const flush = () => act(() => { const pending = [...frames.values()]; frames.clear(); pending.forEach((callback) => callback()) })
    const removed = vi.spyOn(window, 'removeEventListener')
    const scrollRemoved = vi.spyOn(document.querySelector('main'), 'removeEventListener')
    const shellRemoved = vi.spyOn(document.querySelector('.studio-shell'), 'removeEventListener')
    const specs = [['.bar-dock', 'bottom', { reserve: 50 }]]
    const { result, unmount } = renderHook(() => useDrawingViewport(root, specs))
    flush()
    expect(measure).toHaveBeenCalledTimes(1)
    expect(result.current).toEqual(rect(16, 16, 1888, 798))
    expect(observe).toHaveBeenCalledWith(canvasElement)
    expect(observe).toHaveBeenCalledWith(dock)
    const previous = result.current
    act(() => { notify(); notify(); notify() })
    expect(frames.size).toBe(1)
    flush()
    expect(measure).toHaveBeenCalledTimes(2)
    expect(result.current).toBe(previous)
    window.dispatchEvent(new Event('pointermove'))
    flush()
    expect(measure).toHaveBeenCalledTimes(2)
    act(() => { window.dispatchEvent(new Event('resize')); document.querySelector('main').dispatchEvent(new Event('scroll')) })
    expect(frames.size).toBe(1)
    unmount()
    expect(disconnect).toHaveBeenCalledTimes(1)
    expect(frames.size).toBe(0)
    expect(removed).toHaveBeenCalledWith('resize', expect.any(Function))
    expect(scrollRemoved).toHaveBeenCalledWith('scroll', expect.any(Function))
    expect(shellRemoved).toHaveBeenCalledWith('scroll', expect.any(Function))
  })

  it('returns null without a drawing ground', () => {
    const { result } = renderHook(() => useDrawingViewport(null, []))
    expect(result.current).toBeNull()
  })
})
