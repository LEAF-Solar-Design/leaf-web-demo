// @vitest-environment jsdom
import { Profiler } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CadOverview from './CadOverview.jsx'

const shared = vi.hoisted(() => ({ objects: null }))
vi.mock('./DrawingObjectsContext.jsx', () => ({ useDrawingObjects: () => shared.objects }))
const b = (minX, minY, maxX, maxY) => ({ minX, minY, maxX, maxY })
const STORAGE_KEY = 'leaf.cad-overview.collapsed'
const control = () => screen.getByRole('button', { name: 'Drawing overview', exact: true })
const viewport = () => document.querySelector('[data-overview-viewport]')
const rectangle = () => ['x', 'y', 'width', 'height'].map((name) => viewport()?.getAttribute(name))

function fixture(count = 2) {
  const records = Array.from({ length: count }, (_, i) => ({ id: `h:${i}`, handles: [String(i)],
    bounds: count === 2 ? b(i * 80, i * 80, i * 80 + 20, i * 80 + 20) : b(i % 64, Math.floor(i / 64), i % 64 + 1, Math.floor(i / 64) + 1) }))
  const index = { drawingKey: 'drawing-a', intake: {}, records,
    byId: new Map(records.map((r) => [r.id, r])), byHandle: new Map(records.map((r) => [r.handles[0], r])) }
  shared.objects = { index, focusId: 'h:0', selectedHandles: ['0'], focus: vi.fn(), clearFocus: vi.fn(), publishSelection: vi.fn() }
  let snapshot = { pose: { target: [50, 50, 0], zoom: 2 }, viewport: b(25, 25, 75, 75) }
  let scene = { drawingKey: index.drawingKey, intake: index.intake, ready: true }
  const listeners = new Set(), unsubscribe = vi.fn()
  const api = {
    getDrawingScene: vi.fn(() => scene),
    subscribeCamera: vi.fn((listener) => {
      listeners.add(listener); listener(snapshot)
      return () => { unsubscribe(); listeners.delete(listener) }
    }),
    setView: vi.fn(({ center }) => {
      const dx = center.x - snapshot.pose.target[0], dy = center.y - snapshot.pose.target[1]
      snapshot = { pose: { ...snapshot.pose, target: [center.x, center.y, 0] },
        viewport: b(snapshot.viewport.minX + dx, snapshot.viewport.minY + dy, snapshot.viewport.maxX + dx, snapshot.viewport.maxY + dy) }
      listeners.forEach((listener) => listener(snapshot))
    }),
    frame: vi.fn(), setFocusMarker: vi.fn(), select: vi.fn(), pushView: vi.fn(),
  }
  return { index, api, unsubscribe, ref: { current: api },
    scene: (next) => { scene = { ...scene, ...next } },
    publish: (next = snapshot) => act(() => { snapshot = next; listeners.forEach((listener) => listener(snapshot)) }),
  }
}

beforeEach(() => { sessionStorage.clear(); vi.stubGlobal('innerWidth', 1366) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); shared.objects = null })

describe('CadOverview', () => {
  it('renders the outline, distinct overlapping focus and selection, and current camera', () => {
    const f = fixture()
    render(<CadOverview viewerRef={f.ref} />)
    expect(control()).toBeTruthy()
    expect(document.querySelector('.cad-overview-outline')).not.toBeNull()
    expect(document.querySelector('[data-overview-focus] path')).not.toBeNull()
    expect(document.querySelector('[data-overview-selected]').tagName.toLowerCase()).toBe('rect')
    expect(screen.getByText('◇ Focus')).toBeTruthy()
    expect(screen.getByText('□ 1 selected')).toBeTruthy()
    const before = rectangle()
    f.publish({ pose: { target: [60, 50, 0] }, viewport: b(35, 25, 85, 75) })
    expect(rectangle()).not.toEqual(before)
    expect(document.querySelector('[aria-live], [role="status"]')).toBeNull()
  })

  it('deduplicates rounded rectangles while retaining subpixel camera snapshots', () => {
    const f = fixture(), committed = vi.fn()
    render(<Profiler id="overview" onRender={committed}><CadOverview viewerRef={f.ref} /></Profiler>)
    const commits = committed.mock.calls.length
    f.publish({ pose: { target: [50.001, 50, 0] }, viewport: b(25.001, 25, 75.003, 75) })
    expect(committed).toHaveBeenCalledTimes(commits)
    fireEvent.keyDown(control(), { key: 'ArrowRight' })
    expect(f.api.setView.mock.lastCall[0].center.x).toBeCloseTo(62.5015, 8)
    expect(f.api.setView.mock.lastCall[0].center.y).toBe(50)
  })

  it('updates focus and selection independently of an unchanged camera rectangle', () => {
    const f = fixture(), view = render(<CadOverview viewerRef={f.ref} />)
    const before = rectangle()
    const focusX = document.querySelector('[data-overview-focus] rect').getAttribute('x')
    shared.objects = { ...shared.objects, focusId: 'h:1', selectedHandles: ['0', '1'] }
    view.rerender(<CadOverview viewerRef={f.ref} />)
    expect(rectangle()).toEqual(before)
    expect(document.querySelector('[data-overview-focus] rect').getAttribute('x')).not.toBe(focusX)
    expect(screen.getByText('□ 2 selected')).toBeTruthy()
    expect(f.api.subscribeCamera).toHaveBeenCalledTimes(1)
  })

  it('uses camera-only click and keyboard moves, retains focus, and swallows prompt fallback clicks', () => {
    const f = fixture(), fallback = vi.fn()
    const { container } = render(<div onClick={fallback}><CadOverview viewerRef={f.ref} /></div>)
    vi.spyOn(container.querySelector('svg'), 'getBoundingClientRect').mockReturnValue({ left: 10, top: 20, width: 180, height: 112 })
    fireEvent.click(control(), { detail: 1, clientX: 100, clientY: 76 })
    expect(f.api.setView).toHaveBeenLastCalledWith({ center: { x: 50, y: 50 } })
    expect(document.activeElement).toBe(control())
    expect(fallback).not.toHaveBeenCalled()
    for (const key of ['ArrowRight', 'ArrowUp', 'ArrowLeft', 'ArrowDown']) {
      expect(fireEvent.keyDown(control(), { key })).toBe(false)
    }
    expect(f.api.setView).toHaveBeenLastCalledWith({ center: { x: 50, y: 50 } })
    expect(fireEvent.keyDown(control(), { key: 'Enter' })).toBe(false)
    // Native Enter click synthesis must not produce a second recenter.
    fireEvent.click(control(), { detail: 0 })
    fireEvent.keyUp(control(), { key: 'Enter' })
    expect(f.api.setView).toHaveBeenCalledTimes(6)
    expect(f.api.setView).toHaveBeenLastCalledWith({ center: { x: 10, y: 10 } })
    for (const call of f.api.setView.mock.calls) expect(Object.keys(call[0])).toEqual(['center'])
    for (const spy of [f.api.frame, f.api.setFocusMarker, f.api.select, f.api.pushView,
      shared.objects.focus, shared.objects.clearFocus, shared.objects.publishSelection]) expect(spy).not.toHaveBeenCalled()
    expect(shared.objects.focusId).toBe('h:0')
    expect(shared.objects.selectedHandles).toEqual(['0'])
  })

  it('continues moving off drawing from the latest target, not the clipped rectangle', () => {
    const f = fixture()
    render(<CadOverview viewerRef={f.ref} />)
    f.publish({ pose: { target: [1000.125, 1000, 0] }, viewport: b(975.025, 975, 1025.225, 1025) })
    const clipped = rectangle()
    fireEvent.keyDown(control(), { key: 'ArrowRight' })
    expect(f.api.setView.mock.lastCall[0].center.x).toBeCloseTo(1012.675)
    fireEvent.keyDown(control(), { key: 'ArrowRight' })
    expect(f.api.setView.mock.lastCall[0].center.x).toBeCloseTo(1025.225)
    expect(rectangle()).toEqual(clipped)
  })

  it('subscribes once on delayed attachment, replaces handles and unsubscribes on unmount', () => {
    const first = fixture(), ref = { current: null }
    const view = render(<CadOverview viewerRef={ref} />)
    expect(screen.queryByRole('button', { name: 'Drawing overview', exact: true })).toBeNull()
    ref.current = first.api
    view.rerender(<CadOverview viewerRef={ref} />)
    expect(first.api.subscribeCamera).toHaveBeenCalledTimes(1)
    view.rerender(<CadOverview viewerRef={ref} />)
    expect(first.api.subscribeCamera).toHaveBeenCalledTimes(1)
    const replacement = fixture()
    ref.current = replacement.api
    view.rerender(<CadOverview viewerRef={ref} />)
    expect(first.unsubscribe).toHaveBeenCalledTimes(1)
    expect(replacement.api.subscribeCamera).toHaveBeenCalledTimes(1)
    expect(first.unsubscribe.mock.invocationCallOrder[0]).toBeLessThan(replacement.api.subscribeCamera.mock.invocationCallOrder[0])
    view.unmount()
    expect(replacement.unsubscribe).toHaveBeenCalledTimes(1)
  })

  it.each(['not-ready', 'wrong-key', 'wrong-intake'])('hides an early index or mismatched scene (%s) and recovers with an unchanged rectangle', (reason) => {
    const f = fixture()
    const mismatch = reason === 'not-ready' ? { ready: false } : reason === 'wrong-key' ? { drawingKey: 'drawing-b' } : { intake: {} }
    f.scene(mismatch)
    render(<CadOverview viewerRef={f.ref} />)
    expect(document.querySelector('[data-cad-overview]')).toBeNull()
    f.scene({ ready: true, drawingKey: f.index.drawingKey, intake: f.index.intake }); f.publish()
    expect(control()).toBeTruthy()
    f.scene(mismatch); f.publish()
    expect(document.querySelector('[data-cad-overview]')).toBeNull()
  })

  it.each(['click', 'ArrowRight', 'Enter'])('rechecks scene identity at the %s action before a camera publication', (action) => {
    const f = fixture()
    render(<CadOverview viewerRef={f.ref} />)
    const button = control()
    vi.spyOn(button.querySelector('svg'), 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 180, height: 112 })
    f.scene({ intake: {} })
    if (action === 'click') fireEvent.click(button, { detail: 1, clientX: 90, clientY: 56 })
    else fireEvent.keyDown(button, { key: action })
    expect(f.api.setView).not.toHaveBeenCalled()
    expect(document.querySelector('[data-cad-overview]')).toBeNull()
  })

  it('hides without index or bounds and does nothing on Enter without focus', () => {
    const f = fixture(), original = shared.objects
    shared.objects = null
    const view = render(<CadOverview viewerRef={f.ref} />)
    expect(document.querySelector('[data-cad-overview]')).toBeNull()
    shared.objects = { ...original, index: { ...f.index, records: [{ bounds: null }] } }
    view.rerender(<CadOverview viewerRef={f.ref} />)
    expect(document.querySelector('[data-cad-overview]')).toBeNull()
    shared.objects = { ...original, focusId: null }
    view.rerender(<CadOverview viewerRef={f.ref} />)
    fireEvent.keyDown(control(), { key: 'Enter' })
    expect(f.api.setView).not.toHaveBeenCalled()
  })

  it.each([390, 800, 980])('starts collapsed at CSS width %s and remembers expansion across remounts', (width) => {
    vi.stubGlobal('innerWidth', width)
    const f = fixture(), view = render(<CadOverview viewerRef={f.ref} />)
    expect(screen.queryByRole('button', { name: 'Drawing overview', exact: true })).toBeNull()
    expect(document.querySelector('[data-cad-overview]')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Expand drawing overview' }))
    expect(control()).toBeTruthy()
    view.unmount()
    render(<CadOverview viewerRef={f.ref} />)
    expect(control()).toBeTruthy()
  })

  it.each([[390, 'false'], [1366, 'true']])('stored preference overrides width %s (%s)', (width, saved) => {
    vi.stubGlobal('innerWidth', width); sessionStorage.setItem(STORAGE_KEY, saved)
    const f = fixture()
    render(<CadOverview viewerRef={f.ref} />)
    expect(document.querySelector('[data-cad-overview]').getAttribute('data-collapsed')).toBe(saved)
  })

  it('survives denied session storage and remembers collapse when storage works', () => {
    const f = fixture(), view = render(<CadOverview viewerRef={f.ref} />)
    fireEvent.click(screen.getByRole('button', { name: 'Collapse drawing overview' }))
    view.unmount()
    expect(sessionStorage.getItem(STORAGE_KEY)).toBe('true')
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    render(<CadOverview viewerRef={f.ref} />)
    expect(control()).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse drawing overview' }))
    expect(screen.getByRole('button', { name: 'Expand drawing overview' })).toBeTruthy()
  })

  it('limits a 2,345-record rooftop to 64 context elements', () => {
    const f = fixture(2345)
    render(<CadOverview viewerRef={f.ref} />)
    expect(document.querySelectorAll('[data-overview-context]').length).toBeGreaterThan(0)
    expect(document.querySelectorAll('[data-overview-context]').length).toBeLessThanOrEqual(64)
  })
})
