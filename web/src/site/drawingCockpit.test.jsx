/**
 * The drawing cockpit (W4b): view snaps drive the Viewer's ref surface and
 * fail quietly without one; the status readout is a rAF DOM write from the
 * ground's pointer traffic, formatted stably, cleared on leave, and torn
 * down on unmount.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

import { CockpitStatus, FootRegion, StatusToggles, ViewCluster, formatCoordinate, formatScale, zoomViewer } from './DrawingCockpit.jsx'

afterEach(cleanup)

describe('StatusToggles', () => {
  const modes = (detail) => act(() => { window.dispatchEvent(new CustomEvent('cockpit:modes', { detail })) })
  const button = (id) => document.querySelector(`[data-toggle="${id}"]`)
  const reason = 'not in the browser viewer yet'
  const expectDisabled = () => {
    expect(screen.getAllByRole('button').map((el) => el.dataset.toggle)).toEqual(['snap', 'grid', 'ortho', 'polar', 'osnap', 'fullscreen'])
    for (const id of ['snap', 'grid', 'ortho', 'polar', 'osnap']) {
      expect(button(id).disabled).toBe(true)
      expect(button(id).title).toContain(reason)
      expect(button(id).getAttribute('aria-label')).toContain(reason)
      expect(button(id).hasAttribute('aria-pressed')).toBe(false)
    }
  }

  it('starts with the five disabled placeholders in their original order', () => {
    render(<StatusToggles />)
    expectDisabled()
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
    expect(button('ortho').title).toBe('Ortho mode off (F8)')
    expect(button('osnap').title).toBe('Object snap on (F3)')
    expect(button('ortho').getAttribute('aria-label')).toBe('Ortho mode')
    expect(button('osnap').getAttribute('aria-label')).toBe('Object snap')
    expect(['snap', 'grid', 'polar'].map((id) => button(id).outerHTML)).toEqual(placeholders)
    modes({ live: true, ortho: true, osnap: false })
    expect(button('ortho').title).toBe('Ortho mode on (F8)')
    expect(button('osnap').title).toBe('Object snap off (F3)')
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
