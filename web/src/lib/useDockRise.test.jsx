// @vitest-environment jsdom
import { createElement, useRef } from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import useDockRise, { DOCK_RISE_VAR, TABLET_DRAWER_QUERY } from './useDockRise.js'

let mq
let observers
let top

function Shell({ enabled = true }) {
  const appRef = useRef(null)
  useDockRise(appRef, enabled)
  return createElement('div', { className: 'app', ref: appRef },
    createElement('div', { className: 'center-col' },
      createElement('div', {
        className: 'bar-dock',
        ref: (dock) => {
          if (dock) dock.getBoundingClientRect = () => ({ top })
        },
      })))
}

beforeEach(() => {
  top = 883
  observers = []
  mq = { matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }
  vi.stubGlobal('matchMedia', vi.fn(() => mq))
  vi.stubGlobal('innerHeight', 1024)
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback) => { callback(); return 1 }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback) {
      this.callback = callback
      this.observe = vi.fn()
      this.disconnect = vi.fn()
      observers.push(this)
    }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useDockRise', () => {
  it('publishes the tablet dock rise and follows content height changes', () => {
    const { container } = render(createElement(Shell))
    const app = container.querySelector('.app')
    expect(window.matchMedia).toHaveBeenCalledWith(TABLET_DRAWER_QUERY)
    expect(app.style.getPropertyValue(DOCK_RISE_VAR)).toBe('141px')
    expect(observers[0].observe).toHaveBeenCalledWith(app.querySelector('.bar-dock'))
    top = 926
    act(() => observers[0].callback())
    expect(app.style.getPropertyValue(DOCK_RISE_VAR)).toBe('98px')
  })

  it('leaves the property absent outside the tablet frame', () => {
    mq.matches = false
    const { container } = render(createElement(Shell))
    expect(container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('')
    expect(observers[0].observe).not.toHaveBeenCalled()
  })

  it('sets nothing when disabled', () => {
    const { container } = render(createElement(Shell, { enabled: false }))
    expect(container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('')
    expect(window.matchMedia).not.toHaveBeenCalled()
    expect(observers).toHaveLength(0)
  })

  it('removes the property and disconnects on unmount', () => {
    const removeListener = vi.spyOn(window, 'removeEventListener')
    const { container, unmount } = render(createElement(Shell))
    const app = container.firstChild
    unmount()
    expect(app.style.getPropertyValue(DOCK_RISE_VAR)).toBe('')
    expect(observers[0].disconnect).toHaveBeenCalled()
    const listener = mq.addEventListener.mock.calls[0][1]
    expect(mq.removeEventListener).toHaveBeenCalledWith('change', listener)
    expect(removeListener).toHaveBeenCalledWith('resize', listener)
  })

  it('removes the value on frame exit and republishes on entry', () => {
    const { container } = render(createElement(Shell))
    const change = mq.addEventListener.mock.calls[0][1]
    mq.matches = false
    act(() => change())
    expect(container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('')
    mq.matches = true
    act(() => change())
    expect(container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('141px')
  })

  it('coalesces events into one pending frame and cancels it on unmount', () => {
    const frames = []
    requestAnimationFrame.mockImplementation((callback) => { frames.push(callback); return frames.length })
    const { container, unmount } = render(createElement(Shell))
    act(() => {
      window.dispatchEvent(new Event('resize'))
      mq.addEventListener.mock.calls[0][1]()
    })
    expect(frames).toHaveLength(1)
    act(() => frames[0]())
    expect(container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('141px')
    act(() => {
      observers[0].callback()
      window.dispatchEvent(new Event('resize'))
    })
    expect(frames).toHaveLength(2)
    unmount()
    expect(cancelAnimationFrame).toHaveBeenCalledWith(2)
  })

  it('re-observes a replacement dock and avoids redundant style writes', () => {
    const { container } = render(createElement(Shell))
    const app = container.firstChild
    const setProperty = vi.spyOn(app.style, 'setProperty')
    act(() => observers[0].callback())
    expect(setProperty).not.toHaveBeenCalled()
    const replacement = document.createElement('div')
    replacement.className = 'bar-dock'
    replacement.getBoundingClientRect = () => ({ top: 926 })
    app.querySelector('.bar-dock').replaceWith(replacement)
    act(() => window.dispatchEvent(new Event('resize')))
    expect(observers[0].observe).toHaveBeenLastCalledWith(replacement)
    expect(app.style.getPropertyValue(DOCK_RISE_VAR)).toBe('98px')
  })

  it('works without ResizeObserver and does nothing when matchMedia throws', () => {
    vi.stubGlobal('ResizeObserver', undefined)
    const first = render(createElement(Shell))
    expect(first.container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('141px')
    first.unmount()
    window.matchMedia.mockImplementation(() => { throw new Error('unavailable') })
    const second = render(createElement(Shell))
    expect(second.container.firstChild.style.getPropertyValue(DOCK_RISE_VAR)).toBe('')
  })
})
