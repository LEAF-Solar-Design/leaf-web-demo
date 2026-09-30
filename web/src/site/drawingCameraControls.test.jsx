// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DrawingCameraControls from './DrawingCameraControls.jsx'

afterEach(cleanup)
const initial = () => ({ pose: { target: [100, 200, 0], zoom: 7 },
  viewport: { minX: 500, maxX: 580, minY: -300, maxY: -180 } })
function viewer(snapshot = initial()) {
  const listeners = new Set(), off = vi.fn()
  const api = {
    subscribeCamera: vi.fn((listener) => {
      listeners.add(listener); listener(snapshot)
      return () => { off(); listeners.delete(listener) }
    }),
    setView: vi.fn(() => true), off,
    emit: (next) => { snapshot = next; act(() => listeners.forEach((listener) => listener(next))) },
    select: vi.fn(), focus: vi.fn(), applyEdit: vi.fn(), pushView: vi.fn(),
  }
  return api
}
function mount(api = viewer()) {
  const viewerRef = { current: api }, announce = vi.fn()
  const tree = () => <DrawingCameraControls viewerRef={viewerRef} announce={announce} />
  const mounted = render(tree())
  return { ...mounted, api, viewerRef, announce, rerender: () => mounted.rerender(tree()) }
}

it.each([['North', 100, 260], ['South', 100, 140], ['East', 140, 200], ['West', 60, 200]])(
  '%s pans half the visible extent from the target, in one instant call preserving zoom', (direction, x, y) => {
    const h = mount(), snapshot = initial()
    h.api.emit(snapshot)
    fireEvent.click(screen.getByRole('button', { name: direction }))
    expect(h.api.setView).toHaveBeenCalledExactlyOnceWith({ center: { x, y } })
    expect(snapshot.pose.zoom).toBe(7)
    expect(h.announce).toHaveBeenCalledExactlyOnceWith(`View centre ${x.toFixed(2)}, ${y.toFixed(2)} drawing units`)
    for (const key of ['select', 'focus', 'applyEdit', 'pushView']) expect(h.api[key]).not.toHaveBeenCalled()
    expect(h.api.subscribeCamera).toHaveBeenCalledTimes(1)
  },
)

it('accumulates repeated activations before publication, then uses the latest viewport', () => {
  const h = mount(), east = screen.getByRole('button', { name: 'East' })
  fireEvent.click(east); fireEvent.click(east)
  expect(h.api.setView.mock.calls).toEqual([[{ center: { x: 140, y: 200 } }], [{ center: { x: 180, y: 200 } }]])
  h.api.emit({ pose: { target: [180, 200, 0], zoom: 3 }, viewport: { minX: 0, maxX: 20, minY: 0, maxY: 40 } })
  fireEvent.click(east)
  expect(h.api.setView).toHaveBeenLastCalledWith({ center: { x: 190, y: 200 } })
  expect(h.api.subscribeCamera).toHaveBeenCalledTimes(1)
})

it('keeps a synchronous camera publication instead of replacing it with the optimistic target', () => {
  const h = mount(), east = screen.getByRole('button', { name: 'East' })
  h.api.setView.mockImplementationOnce(() => {
    h.api.emit({ pose: { target: [145, 210, 0], zoom: 3 },
      viewport: { minX: 0, maxX: 20, minY: 0, maxY: 40 } })
    return true
  })
  fireEvent.click(east)
  fireEvent.click(east)
  expect(h.api.setView.mock.calls).toEqual([
    [{ center: { x: 140, y: 200 } }], [{ center: { x: 155, y: 210 } }],
  ])
  expect(h.api.subscribeCamera).toHaveBeenCalledTimes(1)
})

it('does not accumulate a refused move', () => {
  const h = mount(), east = screen.getByRole('button', { name: 'East' })
  h.api.setView.mockReturnValueOnce(false)
  fireEvent.click(east)
  fireEvent.click(east)
  expect(h.api.setView.mock.calls).toEqual([
    [{ center: { x: 140, y: 200 } }], [{ center: { x: 140, y: 200 } }],
  ])
  expect(h.announce).toHaveBeenCalledExactlyOnceWith('View centre 140.00, 200.00 drawing units')
})

it('only announces successful button pans, including the same destination twice', () => {
  const h = mount()
  h.api.emit(initial())
  expect(h.announce).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'North' }))
  h.api.emit(initial()) // wheel / drag publication is silent
  fireEvent.click(screen.getByRole('button', { name: 'North' }))
  expect(h.announce.mock.calls).toEqual([
    ['View centre 100.00, 260.00 drawing units'], ['View centre 100.00, 260.00 drawing units'],
  ])
  h.api.setView.mockReturnValue(false)
  fireEvent.click(screen.getByRole('button', { name: 'North' }))
  expect(h.announce).toHaveBeenCalledTimes(2)
})

it.each([null, { pose: null, viewport: null },
  { ...initial(), viewport: { minX: 0, maxX: 0, minY: 0, maxY: 2 } },
  { ...initial(), viewport: { minX: 4, maxX: 0, minY: 0, maxY: 2 } },
  { ...initial(), viewport: { minX: 0, maxX: Infinity, minY: 0, maxY: 2 } },
  { ...initial(), pose: { target: [NaN, 2, 0], zoom: 1 } },
  { ...initial(), pose: { target: [1, 2, Infinity], zoom: 1 } },
  { ...initial(), pose: { target: [1, 2, 0], zoom: 0 } },
])('stays keyboard reachable with an explanation for invalid snapshot %#', (snapshot) => {
  const h = mount(viewer(snapshot)), north = screen.getByRole('button', { name: 'North' })
  north.focus()
  expect(north).toHaveFocus()
  expect(north).toHaveAttribute('aria-disabled', 'true')
  expect(north).toHaveAccessibleDescription('Pan is unavailable until the drawing viewer has a visible viewport.')
  fireEvent.click(north)
  expect(h.api.setView).not.toHaveBeenCalled()
  expect(h.announce).not.toHaveBeenCalled()
})

it('attaches late, replaces subscriptions, ignores stale callbacks and clears readiness on null', () => {
  const h = mount(null), first = viewer(), second = viewer()
  h.viewerRef.current = first; h.rerender()
  const oldListener = first.subscribeCamera.mock.calls[0][0]
  expect(screen.getByRole('button', { name: 'East' })).not.toHaveAttribute('aria-disabled')
  h.rerender(); first.emit(initial())
  expect(first.subscribeCamera).toHaveBeenCalledTimes(1)
  h.viewerRef.current = second; h.rerender()
  expect(first.off).toHaveBeenCalledTimes(1)
  act(() => oldListener(null))
  expect(screen.getByRole('button', { name: 'East' })).not.toHaveAttribute('aria-disabled')
  second.emit(null)
  expect(screen.getByRole('button', { name: 'East' })).toHaveAttribute('aria-disabled', 'true')
  second.emit(initial())
  expect(second.subscribeCamera).toHaveBeenCalledTimes(1)
  h.unmount()
  expect(second.off).toHaveBeenCalledTimes(1)
})

it('does not pan a replaced handle before its subscription has caught up', () => {
  const h = mount(), second = viewer()
  h.viewerRef.current = second
  fireEvent.click(screen.getByRole('button', { name: 'West' }))
  expect(h.api.setView).not.toHaveBeenCalled()
  expect(second.setView).not.toHaveBeenCalled()
})
