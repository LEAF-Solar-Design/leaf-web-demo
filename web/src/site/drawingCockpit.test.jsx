/**
 * The drawing cockpit (W4b): view snaps drive the Viewer's ref surface and
 * fail quietly without one; the status readout is a rAF DOM write from the
 * ground's pointer traffic, formatted stably, cleared on leave, and torn
 * down on unmount.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useCallback, useRef, useState } from 'react'

import { BACK_UNAVAILABLE, CockpitStatus, FootRegion, StatusToggles, ViewCluster, ViewerGrid, formatCoordinate, formatScale, gridLayout, gridStep, paintGrid, useViewNavigation, zoomViewer } from './DrawingCockpit.jsx'
import { createViewHistory } from '../lib/viewHistory.js'
import useDrawingVersionController from '../controllers/useDrawingVersionController.js'

afterEach(cleanup)

describe('StatusToggles', () => {
  const modes = (detail) => act(() => { window.dispatchEvent(new CustomEvent('cockpit:modes', { detail })) })
  const button = (id) => document.querySelector(`[data-toggle="${id}"]`)
  const reason = 'not in the browser viewer yet'
  const expectDisabled = () => {
    expect(screen.getAllByRole('button').map((el) => el.dataset.toggle)).toEqual(['snap', 'grid', 'ortho', 'polar', 'osnap', 'fullscreen'])
    for (const id of ['snap', 'grid', 'ortho', 'polar', 'osnap']) {
      expect(button(id).disabled).toBe(true)
      expect(button(id).title).toContain('Not in the browser viewer yet.')
      expect(button(id).getAttribute('aria-label')).toContain(reason)
      expect(button(id).hasAttribute('aria-pressed')).toBe(false)
    }
  }

  it('starts with the five disabled placeholders in their original order', () => {
    render(<StatusToggles />)
    expectDisabled()
  })

  it('S12 explains each placeholder effect and preserves its accessible label', () => {
    render(<StatusToggles />)
    for (const [id, label, effect] of [
      ['snap', 'Snap mode', 'moves the cursor in fixed steps'],
      ['grid', 'Grid display', 'shows a reference grid behind the drawing'],
      ['ortho', 'Ortho mode', 'locks drawing to horizontal and vertical'],
      ['polar', 'Polar tracking', 'guides the cursor along set angles'],
      ['osnap', 'Object snap', 'locks the cursor onto existing geometry, like endpoints and midpoints'],
    ]) {
      expect(button(id).title).toBe(`${label}: ${effect}. Not in the browser viewer yet.`)
      expect(button(id).getAttribute('aria-label')).toBe(`${label} (unavailable: not in the browser viewer yet)`)
    }
  })

  it('requests the modes exactly once on mount', () => {
    const listener = vi.fn()
    window.addEventListener('cockpit:modes-request', listener)
    try {
      render(<StatusToggles />)
      expect(listener).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('cockpit:modes-request', listener)
    }
  })

  it('enables only ORTHO and OSNAP with the provider values and titles', () => {
    render(<StatusToggles />)
    const placeholders = ['snap', 'grid', 'polar'].map((id) => button(id).outerHTML)
    modes({ live: true, ortho: false, osnap: true })
    expect(button('ortho').disabled).toBe(false)
    expect(button('osnap').disabled).toBe(false)
    expect(button('ortho').getAttribute('aria-pressed')).toBe('false')
    expect(button('osnap').getAttribute('aria-pressed')).toBe('true')
    expect(button('ortho').title).toBe('Ortho mode off (F8). Locks drawing to horizontal and vertical.')
    expect(button('osnap').title).toBe('Object snap on (F3). Locks the cursor onto existing geometry, like endpoints and midpoints.')
    expect(button('ortho').getAttribute('aria-label')).toBe('Ortho mode')
    expect(button('osnap').getAttribute('aria-label')).toBe('Object snap')
    expect(['snap', 'grid', 'polar'].map((id) => button(id).outerHTML)).toEqual(placeholders)
    modes({ live: true, ortho: true, osnap: false })
    expect(button('ortho').title).toBe('Ortho mode on (F8). Locks drawing to horizontal and vertical.')
    expect(button('osnap').title).toBe('Object snap off (F3). Locks the cursor onto existing geometry, like endpoints and midpoints.')
  })

  it('dispatches one toggle and waits for the provider to change pressed state', () => {
    render(<StatusToggles />)
    modes({ live: true, ortho: false, osnap: true })
    const listener = vi.fn()
    window.addEventListener('cockpit:mode-toggle', listener)
    try {
      fireEvent.click(button('ortho'))
      expect(listener).toHaveBeenCalledTimes(1)
      expect(listener.mock.calls[0][0].detail).toEqual({ id: 'ortho' })
      expect(button('ortho').getAttribute('aria-pressed')).toBe('false')
      modes({ live: true, ortho: true, osnap: true })
      expect(button('ortho').getAttribute('aria-pressed')).toBe('true')
    } finally {
      window.removeEventListener('cockpit:mode-toggle', listener)
    }
  })

  it('restores the byte-identical disabled DOM when the bridge goes away', () => {
    const { container } = render(<StatusToggles />)
    const before = container.innerHTML
    modes({ live: true, ortho: true, osnap: false })
    modes({ live: false })
    expectDisabled()
    expect(container.innerHTML).toBe(before)
  })

  it('ignores malformed details without changing the rendered state', () => {
    const { container } = render(<StatusToggles />)
    modes({ live: true, ortho: false, osnap: true })
    const before = container.innerHTML
    for (const detail of [{ live: true, ortho: 'yes', osnap: true }, { ortho: true }, 'bad', null, { live: 'true' }, { live: true, ortho: false, osnap: 1 }]) {
      modes(detail)
      expect(container.innerHTML).toBe(before)
    }
  })

  it('removes its modes listener on unmount', () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    try {
      const { container, unmount } = render(<StatusToggles />)
      const listener = add.mock.calls.find(([type]) => type === 'cockpit:modes')[1]
      unmount()
      expect(remove).toHaveBeenCalledWith('cockpit:modes', listener)
      expect(() => modes({ live: true, ortho: true, osnap: false })).not.toThrow()
      expect(container.innerHTML).toBe('')
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  })

  it('enables GRID from the viewer detail and keeps snap and polar disabled with their reason', () => {
    render(<StatusToggles />)
    const placeholders = ['snap', 'polar'].map((id) => button(id).outerHTML)
    modes({ grid: false })
    expect(button('grid').disabled).toBe(false)
    expect(button('grid').getAttribute('aria-pressed')).toBe('false')
    expect(button('grid').getAttribute('aria-label')).toBe('Grid display')
    expect(button('grid').title).toBe('Grid display off. Shows a reference grid behind the drawing.')
    modes({ grid: true })
    expect(button('grid').getAttribute('aria-pressed')).toBe('true')
    expect(button('grid').title).toBe('Grid display on. Shows a reference grid behind the drawing.')
    for (const id of ['snap', 'polar']) {
      expect(button(id).disabled).toBe(true)
      expect(button(id).getAttribute('aria-label')).toContain(reason)
      expect(button(id).title).toContain('Not in the browser viewer yet.')
    }
    expect(['snap', 'polar'].map((id) => button(id).outerHTML)).toEqual(placeholders)
    // The engine is still absent, so ORTHO and OSNAP stay disabled too.
    expect(button('ortho').disabled).toBe(true)
    expect(button('osnap').disabled).toBe(true)
  })

  it('keeps GRID and the engine modes independent on the shared modes path', () => {
    render(<StatusToggles />)
    modes({ grid: true })
    modes({ live: true, ortho: true, osnap: false })
    expect(button('grid').getAttribute('aria-pressed')).toBe('true')
    expect(button('ortho').getAttribute('aria-pressed')).toBe('true')
    modes({ grid: false })
    expect(button('ortho').getAttribute('aria-pressed')).toBe('true')
    expect(button('osnap').getAttribute('aria-pressed')).toBe('false')
    modes({ live: false })
    expect(button('grid').disabled).toBe(false)
    expect(button('grid').getAttribute('aria-pressed')).toBe('false')
    expect(button('ortho').disabled).toBe(true)
  })

  it('dispatches one GRID toggle and restores the byte-identical disabled DOM when the grid owner goes away', () => {
    const { container } = render(<StatusToggles />)
    const before = container.innerHTML
    modes({ grid: false })
    const listener = vi.fn()
    window.addEventListener('cockpit:mode-toggle', listener)
    try {
      fireEvent.click(button('grid'))
      expect(listener).toHaveBeenCalledTimes(1)
      expect(listener.mock.calls[0][0].detail).toEqual({ id: 'grid' })
      expect(button('grid').getAttribute('aria-pressed')).toBe('false')
    } finally {
      window.removeEventListener('cockpit:mode-toggle', listener)
    }
    modes({ grid: null })
    expectDisabled()
    expect(container.innerHTML).toBe(before)
  })

  it('ignores a malformed grid value without changing the rendered state', () => {
    const { container } = render(<StatusToggles />)
    modes({ grid: true })
    const before = container.innerHTML
    for (const detail of [{ grid: 'on' }, { grid: 1 }, { grid: undefined }, { grid: true, live: true, ortho: 'x', osnap: true }]) {
      modes(detail)
      expect(container.innerHTML).toBe(before)
    }
  })

  // B1b: the object snap mode menu beside the live OSNAP master.
  const trigger = () => screen.queryByRole('button', { name: 'Object snap modes' })
  const items = () => screen.getAllByRole('menuitemcheckbox')
  const checked = () => items().filter((el) => el.getAttribute('aria-checked') === 'true').map((el) => el.dataset.kind)
  const limitLine = () => document.querySelector('[data-testid="cockpit-osnap-limited"]')

  it('B1B-D01 legacy publication defaults', () => {
    render(<StatusToggles />)
    modes({ live: true, ortho: false, osnap: true })
    expect(button('osnap').disabled).toBe(false)
    fireEvent.click(trigger())
    expect(checked()).toEqual(['endpoint', 'midpoint', 'centre', 'quadrant'])
    expect(limitLine().textContent).toBe('')
    expect(screen.queryByText('Object snap limited near this point.')).toBeNull()
    // The full shape replaces the defaults immediately, in both places.
    modes({ live: true, ortho: false, osnap: true, snapModes: 32 | 512, snapLimited: true })
    expect(checked()).toEqual(['intersection', 'nearest'])
    expect(limitLine().textContent).toBe('Object snap limited near this point.')
    expect(screen.getByRole('menu').parentElement.textContent).toContain('Object snap limited near this point.')
    // A legacy publication again reads as the defaults.
    modes({ live: true, ortho: true, osnap: false })
    expect(checked()).toEqual(['endpoint', 'midpoint', 'centre', 'quadrant'])
    expect(limitLine().textContent).toBe('')
  })

  it('B1B-D02 malformed supplied fields rejected', () => {
    const { container } = render(<StatusToggles />)
    modes({ live: true, ortho: false, osnap: true, snapModes: 55, snapLimited: true })
    const before = container.innerHTML
    for (const detail of [
      { live: true, ortho: false, osnap: true, snapModes: 2 ** 32 },
      { live: true, ortho: false, osnap: true, snapModes: 2 ** 32 + 1 },
      { live: true, ortho: false, osnap: true, snapModes: 1016 },
      { live: true, ortho: false, osnap: true, snapModes: 8 },
      { live: true, ortho: false, osnap: true, snapModes: -1 },
      { live: true, ortho: false, osnap: true, snapModes: 1.5 },
      { live: true, ortho: false, osnap: true, snapModes: '23' },
      { live: true, ortho: false, osnap: true, snapModes: null },
      { live: true, ortho: false, osnap: true, snapModes: undefined },
      { live: true, ortho: false, osnap: true, snapModes: Number.NaN },
      { live: true, ortho: false, osnap: true, snapLimited: 'true' },
      { live: true, ortho: false, osnap: true, snapLimited: 1 },
      { live: true, ortho: false, osnap: true, snapLimited: undefined },
      { live: true, ortho: false, osnap: true, snapLimited: null },
      { live: true, ortho: false, osnap: true, snapModes: 2 ** 32, grid: true },
      { grid: true, snapModes: 2 ** 32 },
      { grid: false, snapLimited: 'yes' },
      { live: false, snapModes: 'bad' },
    ]) {
      modes(detail)
      expect(container.innerHTML).toBe(before)
    }
    expect(button('grid').disabled).toBe(true)
  })

  it('B1B-D03 GRID preserves engine fields', () => {
    render(<StatusToggles />)
    modes({ live: true, ortho: true, osnap: false, snapModes: 1015, snapLimited: true })
    modes({ grid: true })
    modes({ grid: false })
    expect(button('ortho').getAttribute('aria-pressed')).toBe('true')
    expect(button('osnap').getAttribute('aria-pressed')).toBe('false')
    expect(limitLine().textContent).toBe('Object snap limited near this point.')
    fireEvent.click(trigger())
    expect(checked()).toEqual(['endpoint', 'midpoint', 'centre', 'quadrant', 'intersection', 'insertion', 'perpendicular', 'tangent', 'nearest'])
    expect(button('grid').getAttribute('aria-pressed')).toBe('false')
  })

  it('B1B-D04 status menu without prompt', () => {
    render(<StatusToggles />)
    expect(trigger()).toBeNull()
    modes({ live: true, ortho: false, osnap: false })
    const requests = vi.fn()
    const toggles = vi.fn()
    window.addEventListener('cockpit:osnap-mode-set', requests)
    window.addEventListener('cockpit:mode-toggle', toggles)
    try {
      // No command prompt exists here; the status menu stands alone.
      expect(document.getElementById('cockpit-prompt')).toBeNull()
      expect(trigger().getAttribute('aria-haspopup')).toBe('menu')
      expect(trigger().getAttribute('aria-expanded')).toBe('false')
      fireEvent.click(trigger())
      expect(trigger().getAttribute('aria-expanded')).toBe('true')
      expect(trigger().getAttribute('aria-controls')).toBe(screen.getByRole('menu').id)
      fireEvent.click(items()[4])
      expect(requests).toHaveBeenCalledTimes(1)
      expect(requests.mock.calls[0][0].detail).toEqual({ kind: 'intersection', enabled: true })
      // Controlled: nothing changes until the provider publishes.
      expect(items()[4].getAttribute('aria-checked')).toBe('false')
      modes({ live: true, ortho: false, osnap: false, snapModes: 55, snapLimited: false })
      expect(items()[4].getAttribute('aria-checked')).toBe('true')
      fireEvent.click(items()[0])
      expect(requests.mock.calls[1][0].detail).toEqual({ kind: 'endpoint', enabled: false })
      // A mode request never flips the master, and the menu stays open.
      expect(toggles).not.toHaveBeenCalled()
      expect(screen.getByRole('menu')).not.toBeNull()
    } finally {
      window.removeEventListener('cockpit:osnap-mode-set', requests)
      window.removeEventListener('cockpit:mode-toggle', toggles)
    }
  })

  it('B1B-D05 offline closes menu and restores DOM', () => {
    const add = vi.spyOn(document, 'addEventListener')
    const remove = vi.spyOn(document, 'removeEventListener')
    try {
      const { container } = render(<StatusToggles />)
      const before = container.innerHTML
      modes({ live: true, ortho: true, osnap: true, snapModes: 1015, snapLimited: true })
      fireEvent.click(trigger())
      expect(screen.getByRole('menu')).not.toBeNull()
      const outside = add.mock.calls.filter(([type]) => type === 'pointerdown')
      expect(outside).toHaveLength(1)
      modes({ live: false })
      expect(screen.queryByRole('menu')).toBeNull()
      expect(trigger()).toBeNull()
      expect(limitLine()).toBeNull()
      expect(remove).toHaveBeenCalledWith('pointerdown', outside[0][1], true)
      expectDisabled()
      expect(container.innerHTML).toBe(before)
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  })
})

describe('grid geometry', () => {
  it('picks the smallest 1-2-5 step at least twelve pixels wide, majors every fifth', () => {
    expect(gridStep(0.25)).toEqual({ minor: 5, major: 25 })
    expect(gridStep(0.5)).toEqual({ minor: 10, major: 50 })
    expect(gridStep(1 / 12)).toEqual({ minor: 1, major: 5 })
    expect(gridStep(0.15).minor).toBe(2)
    expect(gridStep(0.0004).minor).toBeCloseTo(0.005, 12)
    expect(gridStep(2000).minor).toBe(50000)
    for (const bad of [0, -1, NaN, Infinity, undefined]) expect(gridStep(bad)).toBeNull()
  })

  it('anchors the tiles to world (0, 0) and keeps every offset inside one tile', () => {
    const layout = gridLayout({ x: 105, y: 207 }, 0.5, { left: 0, top: 0 })
    expect(layout).toEqual({ step: 10, minorPx: 20, majorPx: 100, minorX: 5, minorY: 7, majorX: 5, majorY: 7 })
    const shifted = gridLayout({ x: -3, y: 40 }, 0.5, { left: 10, top: 20 })
    expect(shifted.minorX).toBe(7)
    expect(shifted.majorX).toBe(87)
    expect(shifted.minorY).toBe(0)
    expect(shifted.majorY).toBe(20)
    expect(gridLayout(null, 0.5, { left: 0, top: 0 })).toBeNull()
    expect(gridLayout({ x: 1, y: 1 }, 0, { left: 0, top: 0 })).toBeNull()
    expect(gridLayout({ x: NaN, y: 1 }, 0.5, { left: 0, top: 0 })).toBeNull()
  })

  it('paints a top-down pose and clears a tilted or missing one', () => {
    const layer = document.createElement('div')
    const viewer = { project: vi.fn(() => ({ x: 105, y: 207 })) }
    expect(paintGrid(layer, viewer, { position: [0, 0, 100], target: [0, 0, 0], worldPerPixel: 0.5 })).toBe(true)
    expect(layer.getAttribute('data-grid-step')).toBe('10')
    expect(viewer.project).toHaveBeenCalledWith(0, 0)
    expect(paintGrid(layer, viewer, { position: [50, 0, 100], target: [0, 0, 0], worldPerPixel: 0.5 })).toBe(false)
    expect(layer.hasAttribute('data-grid-step')).toBe(false)
    expect(layer.style.backgroundSize).toBe('')
    expect(paintGrid(layer, viewer, null)).toBe(false)
    expect(paintGrid(layer, null, { position: [0, 0, 100], target: [0, 0, 0], worldPerPixel: 0.5 })).toBe(false)
    expect(paintGrid(null, viewer, null)).toBe(false)
  })
})

describe('ViewerGrid', () => {
  const flat = (worldPerPixel) => ({ position: [0, 0, 100], target: [0, 0, 0], worldPerPixel })
  function cameraViewer(pose) {
    const listeners = new Set()
    let snapshot = { pose, viewport: null }
    return {
      project: vi.fn(() => ({ x: 105, y: 207 })),
      subscribeCamera: vi.fn((listener) => { listeners.add(listener); listener(snapshot); return () => listeners.delete(listener) }),
      emit(next) { snapshot = { pose: next, viewport: null }; act(() => listeners.forEach((listener) => listener(snapshot))) },
      listeners,
    }
  }
  function withGround(run) {
    const ground = document.createElement('div')
    document.body.appendChild(ground)
    const published = vi.fn()
    window.addEventListener('cockpit:modes', published)
    try { run(ground, published) } finally {
      cleanup()
      window.removeEventListener('cockpit:modes', published)
      ground.remove()
    }
  }
  const toggle = () => act(() => { window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'grid' } })) })
  const last = (published) => published.mock.calls.at(-1)[0].detail

  it('publishes off on mount, answers requests, and draws nothing until toggled', () => withGround((ground, published) => {
    const viewer = cameraViewer(flat(0.5))
    render(<ViewerGrid ground={ground} viewerRef={{ current: viewer }} />)
    expect(last(published)).toEqual({ grid: false })
    published.mockClear()
    act(() => { window.dispatchEvent(new CustomEvent('cockpit:modes-request')) })
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual({ grid: false })
    expect(ground.querySelector('[data-testid="viewer-grid"]')).toBeNull()
    expect(viewer.subscribeCamera).not.toHaveBeenCalled()
  }))

  it('toggles a layer behind the drawing that follows the camera and lets the pointer through', () => withGround((ground, published) => {
    const viewer = cameraViewer(flat(0.5))
    render(<ViewerGrid ground={ground} viewerRef={{ current: viewer }} />)
    toggle()
    expect(last(published)).toEqual({ grid: true })
    const layer = ground.querySelector('[data-testid="viewer-grid"]')
    expect(layer).not.toBeNull()
    expect(layer.parentElement).toBe(ground)
    expect(layer.getAttribute('aria-hidden')).toBe('true')
    expect(layer.style.zIndex).toBe('-1')
    expect(layer.style.pointerEvents).toBe('none')
    expect(layer.style.position).toBe('absolute')
    expect(viewer.subscribeCamera).toHaveBeenCalledTimes(1)
    expect(layer.getAttribute('data-grid-step')).toBe('10')
    viewer.emit(flat(0.25))
    expect(layer.getAttribute('data-grid-step')).toBe('5')
    viewer.emit({ position: [40, 0, 100], target: [0, 0, 0], worldPerPixel: 0.25 })
    expect(layer.hasAttribute('data-grid-step')).toBe(false)
    toggle()
    expect(last(published)).toEqual({ grid: false })
    expect(ground.querySelector('[data-testid="viewer-grid"]')).toBeNull()
    expect(viewer.listeners.size).toBe(0)
  }))

  it('waits for a lazy viewer to attach before subscribing', async () => {
    const ground = document.createElement('div')
    document.body.appendChild(ground)
    const viewerRef = { current: null }
    try {
      render(<ViewerGrid ground={ground} viewerRef={viewerRef} />)
      toggle()
      const layer = ground.querySelector('[data-testid="viewer-grid"]')
      expect(layer.hasAttribute('data-grid-step')).toBe(false)
      const viewer = cameraViewer(flat(0.5))
      viewerRef.current = viewer
      await act(async () => { await nextFrame() })
      expect(viewer.subscribeCamera).toHaveBeenCalledTimes(1)
      expect(layer.getAttribute('data-grid-step')).toBe('10')
    } finally {
      cleanup()
      ground.remove()
    }
  })

  it('publishes null and removes its layer and listeners on unmount', () => withGround((ground, published) => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    try {
      const viewer = cameraViewer(flat(0.5))
      const { unmount } = render(<ViewerGrid ground={ground} viewerRef={{ current: viewer }} />)
      const listeners = add.mock.calls.filter(([type]) => ['cockpit:modes-request', 'cockpit:mode-toggle'].includes(type))
      expect(listeners).toHaveLength(2)
      toggle()
      published.mockClear()
      unmount()
      expect(last(published)).toEqual({ grid: null })
      expect(ground.querySelector('[data-testid="viewer-grid"]')).toBeNull()
      expect(viewer.listeners.size).toBe(0)
      for (const [type, listener] of listeners) expect(remove).toHaveBeenCalledWith(type, listener)
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  }))

  it('never portals into a ground that is not an element', () => withGround((ground, published) => {
    render(<ViewerGrid ground={{}} viewerRef={{ current: null }} />)
    expect(() => toggle()).not.toThrow()
    expect(last(published)).toEqual({ grid: true })
    expect(document.querySelector('[data-testid="viewer-grid"]')).toBeNull()
  }))

  it('drives the status bar GRID toggle end to end while snap and polar stay disabled', () => withGround((ground) => {
    const viewer = cameraViewer(flat(0.5))
    render(<><StatusToggles /><ViewerGrid ground={ground} viewerRef={{ current: viewer }} /></>)
    const grid = () => document.querySelector('[data-toggle="grid"]')
    expect(grid().disabled).toBe(false)
    expect(grid().getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(grid())
    expect(grid().getAttribute('aria-pressed')).toBe('true')
    expect(ground.querySelector('[data-testid="viewer-grid"]')).not.toBeNull()
    fireEvent.click(grid())
    expect(grid().getAttribute('aria-pressed')).toBe('false')
    expect(ground.querySelector('[data-testid="viewer-grid"]')).toBeNull()
    for (const id of ['snap', 'polar']) {
      expect(document.querySelector(`[data-toggle="${id}"]`).disabled).toBe(true)
      expect(document.querySelector(`[data-toggle="${id}"]`).getAttribute('aria-label')).toContain('not in the browser viewer yet')
    }
  }))
})

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()))
// jsdom's PointerEvent carries no client coordinates; a MouseEvent with the
// pointer type name does, and the hook only reads clientX/clientY.
const move = (el, clientX, clientY) => el.dispatchEvent(new MouseEvent('pointermove', { clientX, clientY, bubbles: true }))
const leave = (el) => el.dispatchEvent(new MouseEvent('pointerleave', { bubbles: false }))

describe('formatting', () => {
  it('coordinates are fixed two-decimal and never "-0.00"', () => {
    expect(formatCoordinate(12.3456)).toBe('12.35')
    expect(formatCoordinate(-0.001)).toBe('0.00')
    expect(formatCoordinate(NaN)).toBe('·')
  })
  it('scale reads as drawing units per pixel, three significant digits', () => {
    expect(formatScale(0.41876)).toBe('1px = 0.419u')
    expect(formatScale(0)).toBe('·')
    expect(formatScale(undefined)).toBe('·')
  })
})

describe('ViewCluster', () => {
  it('Fit snaps home; zoom scales the current pose; no viewer is a no-op', () => {
    const viewer = { setView: vi.fn(() => true), getPose: vi.fn(() => ({ zoom: 2, worldPerPixel: 0.5 })) }
    const viewerRef = { current: viewer }
    render(<ViewCluster viewerRef={viewerRef} />)
    fireEvent.click(screen.getByRole('button', { name: 'Fit drawing to view' }))
    expect(viewer.setView).toHaveBeenCalledWith('home')
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(viewer.setView).toHaveBeenLastCalledWith({ zoom: 2.5 })
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    expect(viewer.setView).toHaveBeenLastCalledWith({ zoom: 1.6 })
    viewerRef.current = null
    expect(() => fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))).not.toThrow()
    expect(zoomViewer(null, 2)).toBe(false)
    expect(zoomViewer({ getPose: () => null, setView: vi.fn() }, 2)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// S3 Back and Up. The fake viewer exposes ONLY setView, getPose and frame:
// pose A until setView('home') runs, pose B after it, with a different
// target, zoom and scale, so Back must restore the SCALE, not the raw zoom.
// ---------------------------------------------------------------------------
const POSE_A = Object.freeze({ position: [5, 6, 100], target: [5, 6, 0], zoom: 4, near: 0.1, far: 1000, worldPerPixel: 0.25 })
const POSE_B = Object.freeze({ position: [50, 60, 100], target: [50, 60, 0], zoom: 1, near: 0.1, far: 1000, worldPerPixel: 2 })

function fakeViewer({ frameResult = true } = {}) {
  let homed = false
  return {
    setView: vi.fn((pose) => { if (pose === 'home') homed = true; return true }),
    getPose: vi.fn(() => ({ ...(homed ? POSE_B : POSE_A), target: [...(homed ? POSE_B : POSE_A).target] })),
    frame: vi.fn(() => frameResult),
  }
}

const NAV_INTAKE = {
  polylines: [
    { handle: 'P1', layer: 'Walls', pts: [[0, 0], [10, 4]] },
    { handle: 'P2', layer: 'Roof', pts: [[20, 20], [30, 25]] },
  ],
  inserts: [],
  faces3d: [],
  layers: ['Walls', 'Roof'],
}
const layerOf = (handle) => NAV_INTAKE.polylines.find((e) => e.handle === handle)?.layer ?? null

// App's wiring in miniature: ONE history, a size counter, selection and layers.
function NavHarness({ viewer, probe, initialSelected = null }) {
  const viewerRef = useRef(viewer)
  const history = useRef(null)
  if (!history.current) history.current = createViewHistory()
  const [size, setSize] = useState(0)
  const [selectedHandle, setSelectedHandle] = useState(initialSelected)
  const [visibleLayers, setVisibleLayers] = useState({ Walls: true, Roof: false })
  const nav = useViewNavigation({
    viewerRef, history: history.current, setHistorySize: setSize,
    selectedHandle, selectedLayer: layerOf(selectedHandle), setSelectedHandle,
    visibleLayers, setVisibleLayers, intake: NAV_INTAKE,
  })
  probe.current = { selectedHandle, visibleLayers, setSelectedHandle, setVisibleLayers, size, pushView: nav.pushView }
  return <ViewCluster viewerRef={viewerRef} onFit={nav.fit} canBack={size > 0} onBack={nav.back} onUp={nav.up} announcement={nav.announcement} />
}

const flushFrame = () => act(async () => { await nextFrame() })
const backButton = () => screen.getByRole('button', { name: 'Back to the previous view' })
const upButton = () => screen.getByRole('button', { name: 'Up one level' })
const liveText = () => screen.getByTestId('cockpit-view-live').textContent

describe('ViewCluster Back and Up (S3)', () => {
  it('shows visible Back and Up labels, and an empty Back is aria-disabled but focusable', () => {
    const probe = { current: null }
    render(<NavHarness viewer={fakeViewer()} probe={probe} />)
    const back = backButton()
    expect(back.textContent).toBe('Back')
    expect(upButton().textContent).toBe('Up')
    expect(back.getAttribute('aria-disabled')).toBe('true')
    expect(back.hasAttribute('disabled')).toBe(false)
    expect(back.title).toBe(BACK_UNAVAILABLE)
    const describedBy = back.getAttribute('aria-describedby')
    expect(document.getElementById(describedBy).textContent).toBe(BACK_UNAVAILABLE)
    expect(screen.getByRole('toolbar', { name: 'View' }).contains(back)).toBe(true)
  })

  it('Fit then Back restores the centre, the scale, the selection and the layers; focus stays on Back', async () => {
    const viewer = fakeViewer()
    const probe = { current: null }
    render(<NavHarness viewer={viewer} probe={probe} initialSelected="P1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Fit drawing to view' }))
    expect(viewer.setView).toHaveBeenLastCalledWith('home')
    expect(probe.current.size).toBe(1)
    expect(backButton().hasAttribute('aria-disabled')).toBe(false)
    // The user moves on: a different selection and different layers.
    act(() => {
      probe.current.setSelectedHandle(null)
      probe.current.setVisibleLayers({ Walls: false, Roof: true })
    })
    const back = backButton()
    back.focus()
    fireEvent.click(back)
    // Pose B is on screen now, so the zoom that reproduces A's scale is
    // B.zoom * B.worldPerPixel / A.worldPerPixel = 1 * 2 / 0.25.
    expect(viewer.setView).toHaveBeenLastCalledWith({ center: { x: 5, y: 6 }, zoom: 8 })
    expect(POSE_B.worldPerPixel * POSE_B.zoom / 8).toBe(POSE_A.worldPerPixel)
    expect(probe.current.selectedHandle).toBe('P1')
    expect(probe.current.visibleLayers).toEqual({ Walls: true, Roof: false })
    expect(document.activeElement).toBe(back)
    expect(back.getAttribute('aria-disabled')).toBe('true')
    await flushFrame()
    expect(liveText()).toBe('Back to your previous view, P1 selected')
  })

  it('activating an empty Back does nothing', () => {
    const viewer = fakeViewer()
    const probe = { current: null }
    render(<NavHarness viewer={viewer} probe={probe} initialSelected="P1" />)
    fireEvent.click(backButton())
    expect(viewer.setView).not.toHaveBeenCalled()
    expect(probe.current.selectedHandle).toBe('P1')
    expect(liveText()).toBe('')
  })

  it('Back with no selection in the snapshot announces the plain sentence', async () => {
    const probe = { current: null }
    render(<NavHarness viewer={fakeViewer()} probe={probe} />)
    fireEvent.click(screen.getByRole('button', { name: 'Fit drawing to view' }))
    fireEvent.click(backButton())
    await flushFrame()
    expect(liveText()).toBe('Back to your previous view')
  })

  it('Up with a selected entity preserves the selection and frames its layer; Back returns to the entity', async () => {
    const viewer = fakeViewer()
    const probe = { current: null }
    render(<NavHarness viewer={viewer} probe={probe} initialSelected="P1" />)
    fireEvent.click(upButton())
    expect(probe.current.selectedHandle).toBe('P1')
    expect(viewer.frame).toHaveBeenCalledTimes(1)
    expect(viewer.frame.mock.calls[0][0]).toEqual({ minX: 0, minY: 0, maxX: 10, maxY: 4 })
    expect(viewer.setView).not.toHaveBeenCalled()
    await flushFrame()
    expect(liveText()).toBe('Showing layer Walls')
    act(() => { probe.current.setVisibleLayers({ Walls: false, Roof: false }) })
    fireEvent.click(backButton())
    expect(viewer.setView).toHaveBeenLastCalledWith({ center: { x: 5, y: 6 }, zoom: 4 })
    expect(probe.current.selectedHandle).toBe('P1')
    expect(probe.current.visibleLayers).toEqual({ Walls: true, Roof: false })
    await flushFrame()
    expect(liveText()).toBe('Back to your previous view, P1 selected')
  })

  it('Up falls back to the whole drawing when frame returns false', async () => {
    const viewer = fakeViewer({ frameResult: false })
    const probe = { current: null }
    render(<NavHarness viewer={viewer} probe={probe} initialSelected="P1" />)
    fireEvent.click(upButton())
    expect(viewer.frame).toHaveBeenCalledTimes(1)
    expect(viewer.setView).toHaveBeenLastCalledWith('home')
    expect(probe.current.selectedHandle).toBe('P1')
    await flushFrame()
    expect(liveText()).toBe('Showing the whole drawing')
  })

  it('Up with nothing selected shows the whole drawing every time and re-announces a repeated message', async () => {
    const viewer = fakeViewer()
    const probe = { current: null }
    render(<NavHarness viewer={viewer} probe={probe} />)
    const up = upButton()
    up.focus()
    fireEvent.click(up)
    await flushFrame()
    expect(liveText()).toBe('Showing the whole drawing')
    fireEvent.click(up)
    // Cleared first, then set again on the next frame: a real mutation.
    expect(liveText()).toBe('')
    await flushFrame()
    expect(liveText()).toBe('Showing the whole drawing')
    fireEvent.click(up)
    expect(viewer.setView.mock.calls.filter(([pose]) => pose === 'home')).toHaveLength(3)
    expect(viewer.frame).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(up)
  })

  it('a jump pushed from outside the strip (the ribbon Fit) enables Back through the size counter alone', () => {
    const probe = { current: null }
    render(<NavHarness viewer={fakeViewer()} probe={probe} />)
    expect(backButton().getAttribute('aria-disabled')).toBe('true')
    act(() => { probe.current.pushView() })
    expect(backButton().hasAttribute('aria-disabled')).toBe(false)
  })

  it('never pushes a snapshot before layout (a null pose)', () => {
    const viewer = { setView: vi.fn(() => true), getPose: vi.fn(() => null), frame: vi.fn(() => false) }
    const probe = { current: null }
    render(<NavHarness viewer={viewer} probe={probe} />)
    fireEvent.click(screen.getByRole('button', { name: 'Fit drawing to view' }))
    expect(viewer.setView).toHaveBeenCalledWith('home')
    expect(probe.current.size).toBe(0)
    expect(backButton().getAttribute('aria-disabled')).toBe('true')
  })
})

// Scope reset: every controller source funnels through onResetSelection,
// which App wires to resetDrawingSelection, which clears the history.
function ScopeHarness({ probe }) {
  const history = useRef(null)
  if (!history.current) history.current = createViewHistory()
  const [size, setSize] = useState(0)
  const resetDrawingSelection = useCallback(() => {
    history.current.clear()
    setSize(0)
  }, [])
  const view = (version) => ({ intake: { ...NAV_INTAKE }, drawing_id: 'd1', version, head: 2, latest: 2 })
  const drawing = useDrawingVersionController({
    loadHead: async () => view(2),
    loadVersion: async (_id, version) => view(version),
    onResetSelection: resetDrawingSelection,
  })
  probe.current = {
    size,
    drawing,
    push: () => { if (history.current.push({ pose: POSE_A, selectedHandle: 'P1', visibleLayers: {} })) setSize(history.current.size()) },
    view,
  }
  return null
}

describe('Back history scope reset (S3)', () => {
  it('is empty after each controller source: reset, intake, version, preview and head', async () => {
    const probe = { current: null }
    render(<ScopeHarness probe={probe} />)
    const actions = () => probe.current.drawing.actions
    const cases = [
      ['reset', () => actions().reset()],
      ['intake', () => actions().seatIntake({ ...NAV_INTAKE }, { drawingId: 'd1', drawingState: { version: 2, head: 2, latest: 2 } })],
      ['version', () => actions().seatVersion(probe.current.view(2), { drawingId: 'd1' })],
      ['preview', () => actions().previewVersion(1)],
      ['head', () => actions().previewVersion(2)],
    ]
    for (const [source, run] of cases) {
      act(() => { probe.current.push() })
      expect(probe.current.size, `${source}: seeded`).toBe(1)
      await act(async () => { await run() })
      expect(probe.current.size, `${source}: cleared`).toBe(0)
    }
  })
})

// ---------------------------------------------------------------------------
// FootRegion (P1 studio-shell pass).
//
// The claim this pass makes is "the status bar is three REGIONS, not one
// strip arranged by flex order". Two things have to hold for that to be true
// rather than a restyle, and each is asserted here on the DOM itself:
//
//   ON  — the group is a real element that wraps exactly its own children,
//         so a region edge exists to style. A test that only read a class
//         name would pass against a wrapper that grouped the wrong nodes.
//   OFF — the wrapper contributes NO element at all. That is what keeps the
//         old shell's flat footer byte-identical, and "off renders a plain
//         <span> with no class" would break it silently, so the assertion is
//         on childElementCount, not on the class.
// ---------------------------------------------------------------------------
describe('FootRegion', () => {
  const host = () => {
    const el = document.createElement('footer')
    document.body.appendChild(el)
    return el
  }

  it('ON: wraps its children in one named region element', () => {
    const { container } = render(
      <FootRegion on name="instruments"><span className="a">A</span><span className="b">B</span></FootRegion>,
      { container: host() },
    )
    const region = container.querySelector('.foot-region')
    expect(region).not.toBeNull()
    expect(region.className).toBe('foot-region foot-region-instruments')
    expect(region.dataset.testid).toBe('foot-region-instruments')
    // The children are INSIDE it, and are the only things inside it.
    expect([...region.children].map((c) => c.className)).toEqual(['a', 'b'])
    // ...and the region is the footer's only child: nothing leaked out beside it.
    expect(container.childElementCount).toBe(1)
  })

  it('OFF: contributes no element, so the un-regioned footer is unchanged', () => {
    const children = <><span className="a">A</span><span className="b">B</span></>
    const off = render(<FootRegion on={false} name="instruments">{children}</FootRegion>, { container: host() })
    expect(off.container.querySelector('.foot-region')).toBeNull()
    // Byte-identity with rendering the same children with no wrapper at all.
    const bare = render(<>{children}</>, { container: host() })
    expect(off.container.innerHTML).toBe(bare.container.innerHTML)
    expect(off.container.childElementCount).toBe(2)
  })

  it('OFF is the default posture for a falsy gate, never a truthy-ish one', () => {
    // `on` reaches this from App.jsx as `Boolean(studioGround) && drafting`,
    // so undefined/null/0 must all mean OFF — a region that appeared on a
    // non-drafting surface would put a wrapper in the old shell's footer.
    for (const gate of [undefined, null, 0, '']) {
      const r = render(<FootRegion on={gate} name="docs"><span className="a">A</span></FootRegion>, { container: host() })
      expect(r.container.querySelector('.foot-region')).toBeNull()
    }
  })
})

describe('CockpitStatus', () => {
  it('clears board moves and resumes coordinates on the canvas', async () => {
    const ground = document.createElement('div')
    ground.innerHTML = '<div class="viewer-canvas"><canvas></canvas></div><div class="studio-ground-board"></div>'
    document.body.appendChild(ground)
    const viewer = { unproject: vi.fn((x, y) => ({ x: x / 10, y: -y / 10 })) }
    const { unmount } = render(<CockpitStatus ground={ground} viewerRef={{ current: viewer }} canvasSelector=".viewer-canvas" />)
    try {
      const [x, y] = screen.getByTestId('cockpit-status').querySelectorAll('.cockpit-coord b')
      const canvas = ground.querySelector('canvas')
      const board = ground.querySelector('.studio-ground-board')
      move(board, 100, 50)
      await nextFrame()
      expect(x.textContent).toBe('·')
      expect(y.textContent).toBe('·')
      expect(viewer.unproject).not.toHaveBeenCalled()
      move(canvas, 200, 80)
      await nextFrame()
      expect(x.textContent).toBe('20.00')
      expect(y.textContent).toBe('-8.00')
      expect(viewer.unproject).toHaveBeenCalledTimes(1)
      move(board, 100, 50)
      move(board, 110, 60)
      await nextFrame()
      expect(x.textContent).toBe('·')
      expect(y.textContent).toBe('·')
      expect(viewer.unproject).toHaveBeenCalledTimes(1)
    } finally {
      unmount()
      ground.remove()
    }
  })

  it('writes unprojected cursor coordinates and scale at frame rate, clears on leave, tears down', async () => {
    const ground = document.createElement('div')
    document.body.appendChild(ground)
    const viewer = {
      unproject: vi.fn((x, y) => ({ x: x / 10, y: -y / 10 })),
      getPose: vi.fn(() => ({ zoom: 1, worldPerPixel: 0.25 })),
    }
    const { unmount } = render(
      <CockpitStatus ground={ground} viewerRef={{ current: viewer }} shown={{ polylines: [1, 2, 3], layers: [1] }} selectedHandle="A1" />,
    )
    const status = screen.getByTestId('cockpit-status')
    const [x, y] = status.querySelectorAll('.cockpit-coord b')
    expect(x.textContent).toBe('—')
    expect(status.textContent).toContain('3 entities · 1 layers')
    expect(status.textContent).toContain('sel A1')

    move(ground, 123.4, 50)
    move(ground, 200, 80)
    await nextFrame()
    // Coalesced to one write per frame, on the LAST position.
    expect(viewer.unproject).toHaveBeenCalledTimes(1)
    expect(viewer.unproject).toHaveBeenCalledWith(200, 80)
    expect(x.textContent).toBe('20.00')
    expect(y.textContent).toBe('-8.00')
    expect(status.querySelector('.cockpit-scale b').textContent).toBe('1px = 0.25u')

    leave(ground)
    expect(x.textContent).toBe('·')
    expect(y.textContent).toBe('·')

    unmount()
    move(ground, 1, 1)
    await nextFrame()
    expect(viewer.unproject).toHaveBeenCalledTimes(1)
    ground.remove()
  })

  it('reads "no selection" with nothing selected and holds "—" without a viewer', async () => {
    const ground = document.createElement('div')
    document.body.appendChild(ground)
    render(<CockpitStatus ground={ground} viewerRef={{ current: null }} />)
    move(ground, 5, 5)
    await nextFrame()
    const status = screen.getByTestId('cockpit-status')
    expect(status.querySelector('.cockpit-coord b').textContent).toBe('—')
    expect(status.textContent).toContain('no selection')
    ground.remove()
  })
})
