/**
 * IosSurface. Card D-2 acceptance oracle:
 *   Renders setup status strictly from the contract; every state (ready,
 *   in-progress, unavailable, never-configured) has a distinct truthful
 *   view; no state fabricates progress. With ios_surface off: dormant
 *   placeholder.
 *
 * The contract is D-1's leaf.ios-ship-surface.v1 (server-validated in
 * server/routers/ios_surface.py): readiness = { healthy, launchable }
 * booleans, build_stage from the published 14-word vocabulary or null,
 * receipt_id, reported_at. The four views are derivations of those fields —
 * no invented state/detail/progress fields exist anywhere in this suite.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import IosSurface from './IosSurface.jsx'

afterEach(cleanup)

function contractWith(readiness, buildStage = null) {
  return {
    schema: 'leaf.ios-ship-surface.v1',
    project_id: 'proj-1',
    revision: 'rev-1',
    reported_at: '2026-08-19T18:00:00+00:00',
    readiness,
    build_stage: buildStage,
    receipt_id: null,
  }
}

describe('ios_surface flag off: dormant placeholder', () => {
  it('renders a neutral placeholder and no readiness detail, even when a contract is supplied', () => {
    render(<IosSurface enabled={false} contract={contractWith({ healthy: true, launchable: true })} />)
    expect(screen.getByLabelText('iOS readiness')).toHaveAttribute('data-state', 'dormant')
    expect(screen.queryByText(/ready/i)).not.toBeInTheDocument()
  })

  it('stays dormant with no contract prop at all', () => {
    render(<IosSurface enabled={false} />)
    expect(screen.getByLabelText('iOS readiness')).toHaveAttribute('data-state', 'dormant')
  })
})

describe('each derived state has a distinct truthful view', () => {
  it('ready: healthy && launchable', () => {
    render(<IosSurface enabled contract={contractWith({ healthy: true, launchable: true }, 'RECEIPT')} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'ready')
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('in-progress: healthy && !launchable, signal is the contract build_stage word', () => {
    render(<IosSurface enabled contract={contractWith({ healthy: true, launchable: false }, 'MAC_ALLOCATED')} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'in-progress')
    expect(screen.getByText(/Setting up — Mac allocated/)).toBeInTheDocument()
  })

  it('in-progress with a null build_stage: plain label, no invented stage text', () => {
    render(<IosSurface enabled contract={contractWith({ healthy: true, launchable: false }, null)} />)
    expect(screen.getByLabelText('iOS readiness')).toHaveAttribute('data-state', 'in-progress')
    expect(screen.getByText('Setting up')).toBeInTheDocument()
    expect(screen.queryByText(/—/)).not.toBeInTheDocument()
  })

  it('unavailable: healthy === false wins regardless of launchable', () => {
    render(<IosSurface enabled contract={contractWith({ healthy: false, launchable: true }, 'BUILT')} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'unavailable')
    expect(el).toHaveAttribute('role', 'alert')
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
  })

  it('never-configured: no contract published (null)', () => {
    render(<IosSurface enabled contract={null} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'never-configured')
    expect(screen.getByText('Not yet configured')).toBeInTheDocument()
  })

  it('all four states render distinctly', () => {
    const seen = new Set()
    for (const [readiness, stage] of [
      [{ healthy: true, launchable: true }, null],
      [{ healthy: true, launchable: false }, 'BUILT'],
      [{ healthy: false, launchable: false }, null],
    ]) {
      const { unmount } = render(<IosSurface enabled contract={contractWith(readiness, stage)} />)
      seen.add(screen.getByLabelText('iOS readiness').getAttribute('data-state'))
      unmount()
    }
    const { unmount } = render(<IosSurface enabled contract={null} />)
    seen.add(screen.getByLabelText('iOS readiness').getAttribute('data-state'))
    unmount()
    expect(seen).toEqual(new Set(['ready', 'in-progress', 'unavailable', 'never-configured']))
  })
})

describe('no fabrication', () => {
  it('never renders a percentage: the contract has no progress field to show', () => {
    for (const [readiness, stage] of [
      [{ healthy: true, launchable: false }, 'UPLOADED'],
      [{ healthy: true, launchable: true }, 'RECEIPT'],
      [{ healthy: false, launchable: false }, null],
    ]) {
      const { unmount } = render(<IosSurface enabled contract={contractWith(readiness, stage)} />)
      expect(screen.queryByText(/%/)).not.toBeInTheDocument()
      unmount()
    }
  })

  it('a malformed contract (readiness missing booleans) renders nothing rather than a guessed state', () => {
    for (const bad of [
      contractWith({ healthy: true }),
      contractWith({ launchable: true }),
      contractWith('healthy'),
      contractWith(undefined),
    ]) {
      const { container, unmount } = render(<IosSurface enabled contract={bad} />)
      expect(container).toBeEmptyDOMElement()
      unmount()
    }
  })

  it('renders strictly from props: no fetch is issued', () => {
    const calls = []
    const realFetch = globalThis.fetch
    globalThis.fetch = (...args) => { calls.push(args); return Promise.reject(new Error('no')) }
    try {
      const { unmount } = render(<IosSurface enabled contract={contractWith({ healthy: true, launchable: true })} />)
      unmount()
    } finally {
      globalThis.fetch = realFetch
    }
    expect(calls).toEqual([])
  })
})
