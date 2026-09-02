/**
 * The drawing cockpit (W4b): view snaps drive the Viewer's ref surface and
 * fail quietly without one; the status readout is a rAF DOM write from the
 * ground's pointer traffic, formatted stably, cleared on leave, and torn
 * down on unmount.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { CockpitStatus, ViewCluster, formatCoordinate, formatScale, zoomViewer } from './DrawingCockpit.jsx'

afterEach(cleanup)

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()))
// jsdom's PointerEvent carries no client coordinates; a MouseEvent with the
// pointer type name does, and the hook only reads clientX/clientY.
const move = (el, clientX, clientY) => el.dispatchEvent(new MouseEvent('pointermove', { clientX, clientY, bubbles: true }))
const leave = (el) => el.dispatchEvent(new MouseEvent('pointerleave', { bubbles: false }))

describe('formatting', () => {
  it('coordinates are fixed two-decimal and never "-0.00"', () => {
    expect(formatCoordinate(12.3456)).toBe('12.35')
    expect(formatCoordinate(-0.001)).toBe('0.00')
    expect(formatCoordinate(NaN)).toBe('—')
  })
  it('scale reads as drawing units per pixel, three significant digits', () => {
    expect(formatScale(0.41876)).toBe('1px = 0.419u')
    expect(formatScale(0)).toBe('—')
    expect(formatScale(undefined)).toBe('—')
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

describe('CockpitStatus', () => {
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
    expect(x.textContent).toBe('—')
    expect(y.textContent).toBe('—')

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
