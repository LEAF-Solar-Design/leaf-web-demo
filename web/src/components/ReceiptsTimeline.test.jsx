/**
 * ReceiptsTimeline: the timeline must never claim more than the receipts say.
 *
 * The load-bearing assertions are the two honest states. An EMPTY timeline and
 * an UNAVAILABLE source must render as different sentences, because collapsing
 * them would tell a reader "nothing ran" when the truth is "nobody could look".
 * The rest pins the row rendering: newest first, the kind chip, sha8, and a
 * link only for an http(s) URL.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import ReceiptsTimeline, { safeHref, sha8 } from './ReceiptsTimeline.jsx'

afterEach(cleanup)

// Fixture rows in the exact shape GET /api/receipts returns.
const ROWS = [
  {
    kind: 'gate-proof',
    ref: 'tree:4f1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c',
    at: '2026-08-30T11:02:00Z',
    sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678',
    summary: 'gate-proof-4f1c2d3e (12 KiB)',
    url: 'https://github.com/example/repo/actions/runs/321',
  },
  {
    kind: 'prewarm-relay',
    ref: 'pr:988',
    at: '2026-09-01T09:14:00Z',
    sha: 'ffeeddccbbaa99887766554433221100aabbccdd',
    summary: 'prewarm-relay-receipt-pr-988 (3 KiB)',
    url: 'https://github.com/example/repo/actions/runs/654',
  },
]

describe('ReceiptsTimeline rows', () => {
  it('renders every row newest first with its kind chip and sha8', () => {
    render(<ReceiptsTimeline rows={ROWS} scope="tree:4f1c2d3e" />)
    const list = screen.getByTestId('receipts-rows')
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    // Newest (2026-09-01) first, even though the fixture is oldest-first.
    expect(within(items[0]).getByTestId('receipt-kind').textContent).toBe('Prewarm relay')
    expect(within(items[1]).getByTestId('receipt-kind').textContent).toBe('Gate proof')
    expect(within(items[0]).getByTestId('receipt-sha').textContent).toBe('ffeeddcc')
    expect(within(items[1]).getByTestId('receipt-sha').textContent).toBe('a1b2c3d4')
  })

  it('links only an http(s) url and renders nothing clickable otherwise', () => {
    render(
      <ReceiptsTimeline
        rows={[
          { ...ROWS[0], url: 'https://example.com/run' },
          { ...ROWS[1], at: '2026-08-01T00:00:00Z', url: 'javascript:alert(1)' },
        ]}
      />,
    )
    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0].getAttribute('href')).toBe('https://example.com/run')
  })

  it('never invents a summary or a sha it was not given', () => {
    render(<ReceiptsTimeline rows={[{ kind: 'job', ref: 'job:j1', at: '', sha: '', summary: '', url: '' }]} />)
    expect(screen.getByText('No summary was recorded.')).toBeTruthy()
    expect(screen.queryByTestId('receipt-sha')).toBeNull()
    expect(screen.queryByRole('link')).toBeNull()
  })
})

describe('ReceiptsTimeline honest states', () => {
  it('says nothing exists yet, and predicts nothing, when every source answered empty', () => {
    render(<ReceiptsTimeline rows={[]} unavailable={[]} scope="pr:1" />)
    const empty = screen.getByTestId('receipts-empty')
    expect(empty.textContent).toContain('No receipt exists for this scope yet')
    expect(screen.queryByTestId('receipts-unavailable')).toBeNull()
  })

  it('names the source and the reason when a source could not be read', () => {
    render(
      <ReceiptsTimeline
        rows={[]}
        unavailable={[
          {
            source: 'github-artifacts',
            reason: 'source_unavailable',
            detail: 'LEAF_PLATFORM_PR_TOKEN is not configured on this deployment',
          },
        ]}
        scope="pr:988"
      />,
    )
    const blocked = screen.getByTestId('receipts-unavailable')
    expect(blocked.textContent).toContain('github-artifacts')
    expect(blocked.textContent).toContain('is not configured on this deployment')
    // An unreadable source is NOT an empty timeline: the two sentences never swap.
    expect(screen.queryByTestId('receipts-empty')).toBeNull()
  })

  it('shows rows and the unavailable note together when only one source failed', () => {
    render(
      <ReceiptsTimeline
        rows={[ROWS[0]]}
        unavailable={[{ source: 'reconciler', reason: 'source_unreachable', detail: '' }]}
      />,
    )
    expect(screen.getAllByRole('listitem').length).toBeGreaterThan(0)
    expect(screen.getByTestId('receipts-unavailable').textContent).toContain('did not answer')
    expect(screen.queryByTestId('receipts-empty')).toBeNull()
  })

  it('says a source was skipped for load rather than calling it empty', () => {
    // The server refuses over its inflight cap instead of holding a slot. That
    // is "we did not look", never "nothing happened".
    render(
      <ReceiptsTimeline
        rows={[]}
        unavailable={[{ source: 'github-artifacts', reason: 'source_busy', detail: '' }]}
      />,
    )
    expect(screen.getByTestId('receipts-unavailable').textContent)
      .toContain('already in flight')
    expect(screen.queryByTestId('receipts-empty')).toBeNull()
  })

  it('does not claim emptiness while a read is still in flight', () => {
    render(<ReceiptsTimeline rows={[]} unavailable={[]} loading />)
    expect(screen.getByTestId('receipts-loading')).toBeTruthy()
    expect(screen.queryByTestId('receipts-empty')).toBeNull()
  })

  it('survives a malformed rows payload without rendering a fabricated row', () => {
    render(<ReceiptsTimeline rows={[null, undefined, 'nope']} unavailable={null} />)
    expect(screen.queryByTestId('receipts-rows')).toBeNull()
    expect(screen.getByTestId('receipts-empty')).toBeTruthy()
  })
})

describe('sha8 and safeHref', () => {
  it('returns an empty string rather than padding or inventing a sha', () => {
    expect(sha8('A1B2C3D4E5F6')).toBe('a1b2c3d4')
    expect(sha8('abc')).toBe('')
    expect(sha8('not-a-sha-at-all')).toBe('')
    expect(sha8(null)).toBe('')
    expect(sha8(12345678)).toBe('')
  })

  it('accepts only absolute http(s) urls', () => {
    expect(safeHref('https://example.com/a')).toBe('https://example.com/a')
    expect(safeHref('http://example.com/a')).toBe('http://example.com/a')
    expect(safeHref('javascript:alert(1)')).toBeNull()
    expect(safeHref('data:text/html,<b>')).toBeNull()
    expect(safeHref('/relative')).toBeNull()
    expect(safeHref('')).toBeNull()
  })
})
