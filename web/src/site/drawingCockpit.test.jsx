/**
 * The drawing cockpit (W4b): view snaps drive the Viewer's ref surface and
 * fail quietly without one; the status readout is a rAF DOM write from the
 * ground's pointer traffic, formatted stably, cleared on leave, and torn
 * down on unmount.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { CockpitStatus, FootRegion, ViewCluster, formatCoordinate, formatScale, zoomViewer } from './DrawingCockpit.jsx'

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
