// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SurfaceFrame from '../site/SurfaceFrame.jsx'
import ConnectionBanner from './ConnectionBanner.jsx'

let online

beforeEach(() => {
  online = true
  vi.spyOn(navigator, 'onLine', 'get').mockImplementation(() => online)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function connection(value) {
  act(() => {
    online = value
    window.dispatchEvent(new Event(value ? 'online' : 'offline'))
  })
}

describe('ConnectionBanner', () => {
  it('renders the offline NT2 condition with an amber square dot and no dismiss control', () => {
    online = false
    const { getByRole } = render(<ConnectionBanner />)
    const banner = getByRole('status')
    expect(banner.textContent).toBe('Offline. Leaf Automation reconnects on its own.')
    const dot = banner.querySelector('.dot.square')
    expect(dot).not.toBeNull()
    expect(dot.getAttribute('aria-hidden')).toBe('true')
    expect(dot.style.width).toBe('6px')
    expect(dot.style.height).toBe('6px')
    expect(dot.style.borderRadius).toBe('1.5px')
    expect(dot.style.background).toBe('var(--status-warning)')
    expect(banner.querySelector('.pulse, .live')).toBeNull()
    expect(banner.hasAttribute('aria-live')).toBe(false)
    expect(banner.querySelector('button, [role="button"], a')).toBeNull()
    expect(banner.textContent).not.toMatch(/saved/i)
  })

  it('renders nothing while online', () => {
    const { container } = render(<ConnectionBanner />)
    expect(container.innerHTML).toBe('')
  })

  it('appears on disconnect and clears itself on reconnect, including repeated reconnects', () => {
    const { container, queryByRole } = render(<ConnectionBanner />)
    connection(false)
    expect(queryByRole('status')).not.toBeNull()
    connection(true)
    expect(container.innerHTML).toBe('')
    connection(false)
    expect(container.querySelectorAll('[role="status"]')).toHaveLength(1)
    connection(true)
    expect(container.innerHTML).toBe('')
  })

  it('clears an initially offline render on reconnect', () => {
    online = false
    const { container } = render(<ConnectionBanner />)
    connection(true)
    expect(container.innerHTML).toBe('')
  })

  it('removes the connection listeners when unmounted', () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const { unmount } = render(<ConnectionBanner />)
    const listeners = add.mock.calls.filter(([event]) => event === 'online' || event === 'offline')
    expect(listeners).toHaveLength(2)
    unmount()
    for (const [event, listener] of listeners) {
      expect(remove).toHaveBeenCalledWith(event, listener)
    }
  })

  it.each(['console', 'stage'])('mounts one banner below the %s header and preserves the online markup', (scene) => {
    const headerRef = (node) => {
      if (node) node.getBoundingClientRect = () => ({ bottom: 48, left: 0, right: window.innerWidth })
    }
    const shell = scene === 'console'
      ? <div className="app"><header className="top" ref={headerRef}>Header</header><main>Workspace</main></div>
      : <div className="stage-root"><nav className="tc-product-nav" ref={headerRef}>Header</nav><main>Workspace</main></div>
    const { container } = render(<SurfaceFrame scene={scene} activeSurface="cad">{shell}</SurfaceFrame>)
    const onlineMarkup = container.innerHTML
    expect(container.querySelector('[data-testid="connection-banner"]')).toBeNull()
    connection(false)
    const banners = container.querySelectorAll('[data-testid="connection-banner"]')
    expect(banners).toHaveLength(1)
    expect(banners[0].style.position).toBe('fixed')
    expect(banners[0].style.top).toBe('48px')
    connection(true)
    expect(container.innerHTML).toBe(onlineMarkup)
  })
})
