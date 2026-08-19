/**
 * IosSurface. Card D-2 acceptance oracle:
 *   Renders setup status strictly from the contract; every state (ready,
 *   in-progress, unavailable, never-configured) has a distinct truthful
 *   view; no state fabricates progress. With ios_surface off: dormant
 *   placeholder.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import IosSurface from './IosSurface.jsx'

afterEach(cleanup)

describe('ios_surface flag off: dormant placeholder', () => {
  it('renders a neutral placeholder and no readiness detail, even when a contract is supplied', () => {
    render(<IosSurface enabled={false} readiness={{ state: 'ready', detail: 'build 42 signed' }} />)
    expect(screen.getByLabelText('iOS readiness')).toHaveAttribute('data-state', 'dormant')
    expect(screen.queryByText(/build 42 signed/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/ready/i)).not.toBeInTheDocument()
  })

  it('stays dormant with no readiness prop at all', () => {
    render(<IosSurface enabled={false} />)
    expect(screen.getByLabelText('iOS readiness')).toHaveAttribute('data-state', 'dormant')
  })
})

describe('each contract state has a distinct truthful view', () => {
  it('ready: shows the ready label and the server detail verbatim', () => {
    render(<IosSurface enabled readiness={{ state: 'ready', detail: 'build 42 signed and installed' }} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'ready')
    expect(screen.getByText('Ready')).toBeInTheDocument()
    expect(screen.getByText('build 42 signed and installed')).toBeInTheDocument()
  })

  it('in-progress: shows the setting-up label and the server detail verbatim', () => {
    render(<IosSurface enabled readiness={{ state: 'in-progress', detail: 'provisioning signing certificate' }} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'in-progress')
    expect(screen.getByText('provisioning signing certificate')).toBeInTheDocument()
  })

  it('unavailable: shows the unavailable label and the server-given reason', () => {
    render(<IosSurface enabled readiness={{ state: 'unavailable', detail: 'build pipeline offline' }} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'unavailable')
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
    expect(screen.getByText('build pipeline offline')).toBeInTheDocument()
  })

  it('never-configured: shows the not-yet-configured label with no fabricated detail', () => {
    render(<IosSurface enabled readiness={{ state: 'never-configured' }} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'never-configured')
    expect(screen.getByText('Not yet configured')).toBeInTheDocument()
  })

  it('the four known states render four different data-state markers (no shared/ambiguous view)', () => {
    const states = ['ready', 'in-progress', 'unavailable', 'never-configured']
    const rendered = states.map((state) => {
      const { container } = render(<IosSurface enabled readiness={{ state }} />)
      const marker = container.querySelector('[data-state]').getAttribute('data-state')
      cleanup()
      return marker
    })
    expect(new Set(rendered).size).toBe(states.length)
  })
})

describe('no state fabricates progress', () => {
  it('in-progress with no progress field shows the plain label and no percentage or progress bar', () => {
    render(<IosSurface enabled readiness={{ state: 'in-progress' }} />)
    expect(screen.getByText('Setting up')).toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
    expect(document.querySelector('progress')).not.toBeInTheDocument()
    expect(document.querySelector('[role="progressbar"]')).not.toBeInTheDocument()
  })

  it('in-progress with a real contract progress number echoes exactly that number, not a guess', () => {
    render(<IosSurface enabled readiness={{ state: 'in-progress', progress: 42 }} />)
    expect(screen.getByText(/42% complete/)).toBeInTheDocument()
  })

  it('ready, unavailable, and never-configured never render a percentage even if the field is present', () => {
    for (const state of ['ready', 'unavailable', 'never-configured']) {
      const { unmount } = render(<IosSurface enabled readiness={{ state, progress: 77 }} />)
      expect(screen.queryByText(/77%/)).not.toBeInTheDocument()
      unmount()
    }
  })
})

describe('the contract is the only source of truth', () => {
  it('renders nothing when no readiness contract has been read yet', () => {
    const { container } = render(<IosSurface enabled readiness={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a state the contract never defined, rather than guessing', () => {
    const { container } = render(<IosSurface enabled readiness={{ state: 'mid-flight-invented-state' }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
