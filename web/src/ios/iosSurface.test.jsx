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
import { DeviceGround } from './DeviceGround.jsx'

afterEach(cleanup)

it('I1 row8 absent ship preserves the existing surface and device render', () => {
  const contract = contractWith({ healthy: true, launchable: false }, 'MAC_ALLOCATED')
  const view = render(<IosSurface enabled contract={contract} />)
  const html = view.container.innerHTML
  expect(screen.getByText('iOS app')).toBeInTheDocument()
  expect(screen.getByText('Setting up — Mac allocated')).toBeInTheDocument()
  expect(screen.queryByTestId('ios-stage-ladder')).not.toBeInTheDocument()
  view.rerender(<IosSurface enabled contract={contract} ship={undefined} />)
  expect(view.container.innerHTML).toBe(html)
  view.unmount()
  const ground = render(<DeviceGround active enabled contract={contract} revision="r1" />)
  const groundHtml = ground.container.innerHTML
  expect(screen.getByText('TestFlight lane')).toBeInTheDocument()
  expect(screen.getByText('Mounted Apple readiness')).toBeInTheDocument()
  expect(screen.getByText('Approved revision')).toBeInTheDocument()
  expect(screen.getByTestId('device-state')).toHaveTextContent(/^Setting up$/)
  expect(screen.queryByTestId('ios-stage-ladder')).not.toBeInTheDocument()
  ground.rerender(<DeviceGround active enabled contract={contract} revision="r1" ship={undefined} />)
  expect(ground.container.innerHTML).toBe(groundHtml)
})

it('I1 row9 renders setup, provider progress, receipt and launch reason from ship', () => {
  const ship = { phase: 'setup-required', readiness: { setupState: 'no-approved-revision', setupAction: 'Approve revision r1' }, execution: null, receipt: null }
  const view = render(<IosSurface enabled ship={ship} onLaunch={() => {}} />)
  expect(view.container.querySelector('[data-rung="source"]')).toHaveAttribute('data-state', 'missing')
  expect(screen.getByText('Setup required: Approve revision r1')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Launch TestFlight build' })).toBeDisabled()
  expect(screen.getByText('Complete the reported setup action before launching.')).toBeInTheDocument()
  const running = { ...ship, phase: 'running', readiness: { setupState: 'none' }, execution: { status: 'running', stage: 'BUILT', updated_at: '2026-09-17T12:00:00Z' } }
  view.rerender(<IosSurface enabled ship={running} />)
  expect(screen.getByText('BUILT')).toBeInTheDocument()
  expect(screen.getByText('2026-09-17T12:00:00Z')).toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  const succeeded = { ...running, phase: 'succeeded', execution: { status: 'succeeded' }, receipt: { kind: 'leaf.ios-testflight-receipt.v1', receipt_id: 'receipt1', bundle_identifier: 'com.leaf.test' } }
  view.rerender(<IosSurface enabled ship={succeeded} />)
  expect(screen.getByText('succeeded')).toBeInTheDocument()
  expect(screen.getByText('com.leaf.test')).toBeInTheDocument()
  view.unmount()
  const ground = render(<DeviceGround active enabled ship={ship} onLaunch={() => {}} />)
  expect(screen.getByText('Build and delivery status')).toBeInTheDocument()
  expect(screen.getByTestId('ios-stage-ladder')).toBeInTheDocument()
  expect(ground.container.querySelector('[data-rung="source"]')).toHaveAttribute('data-state', 'missing')
})

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
