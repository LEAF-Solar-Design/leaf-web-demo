// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fromBrokerJob, validateBuildRecord } from '../lib/buildQueue.js'
import JobRail from './JobRail.jsx'

afterEach(cleanup)

const doneJob = Object.freeze({
  job_id: 'job-verified',
  tool: 'count-by-layer',
  status: 'complete',
  created_at: 1725400000,
})

const terminalRecord = fromBrokerJob({
  ...doneJob,
  receipts: [{ kind: 'terminal', ref: 'receipts/job-verified/receipt.json', at: 1725400001 }],
})

describe('JobRail build-record reconciliation', () => {
  it('enriches one live broker row with its terminal receipt without duplicating it', () => {
    const { container } = render(
      <JobRail mock jobs={[doneJob]} builds={[terminalRecord]} currentJob={null} />,
    )

    const cards = container.querySelectorAll('.bq-card')
    expect(cards).toHaveLength(1)
    expect(cards[0].getAttribute('data-verified')).toBe('1')
    expect(cards[0].querySelector('.bq-receipts')?.textContent).toContain('1 receipt')
  })

  it('keeps a broker build that is absent from the recent-jobs list', () => {
    const { container } = render(
      <JobRail mock jobs={[]} builds={[terminalRecord]} currentJob={null} />,
    )

    expect(container.querySelectorAll('.bq-card')).toHaveLength(1)
    expect(container.querySelector('.bq-card')?.getAttribute('data-lane')).toBe('broker')
  })

  it('does not let an older build poll replace a newer job state', () => {
    const runningJob = { ...doneJob, status: 'running' }
    const { container } = render(
      <JobRail mock jobs={[runningJob]} builds={[terminalRecord]} currentJob={null} />,
    )

    const card = container.querySelector('.bq-card')
    expect(card?.getAttribute('data-state')).toBe('running')
    expect(card?.getAttribute('data-verified')).toBe('0')
  })
})

// W6-E01: the rail says when its builds feed is stale or paused by sign-in.
// Queued and done only: a running card's elapsed tail reads the clock, which
// would make two renders differ for a reason that is not the feed.
const fleetRecord = (id, state) => validateBuildRecord({
  id, lane: 'fleet', state, title: `task ${id}`, requested_by: null, started: 1725400000000 + id.length,
  elapsed_ms: null, estimate_ms: null, cost_usd: null, receipts: [],
  terminal: { verified: false, promoted: false }, actions: [], status: { word: state, tint: 'ok', detail: null },
})
const FEED_BUILDS = [fleetRecord('a', 'queued'), fleetRecord('bb', 'done'), terminalRecord]
const STALE_TEXT = 'Build list may be out of date: the last refresh failed.'
const AUTH_TEXT = 'Build updates are paused until you sign in again.'

const cardKeys = (container) => [...container.querySelectorAll('.bq-card')]
  .map((c) => `${c.getAttribute('data-lane')}|${c.getAttribute('data-state')}|${c.querySelector('.rail-tool')?.textContent}`)

describe('JobRail build feed notes', () => {
  it('E01 row8 no buildFeed prop renders no .rail-feed and no [data-feed-dropped]', () => {
    const { container } = render(<JobRail mock jobs={[doneJob]} builds={FEED_BUILDS} currentJob={null} />)
    expect(container.querySelector('.rail-feed')).toBeNull()
    expect(container.querySelector('[data-feed-dropped]')).toBeNull()
    const bare = container.innerHTML
    cleanup()
    for (const status of ['live', 'idle']) {
      const again = render(
        <JobRail mock jobs={[doneJob]} builds={FEED_BUILDS} currentJob={null} buildFeed={{ status, dropped: 0, onRetry: vi.fn() }} />,
      )
      expect(again.container.innerHTML).toBe(bare)
      cleanup()
    }
  })

  it('E01 row9 stale renders the sentence and Retry calls onRetry once', () => {
    const onRetry = vi.fn()
    const { container } = render(
      <JobRail mock jobs={[]} builds={FEED_BUILDS} currentJob={null} buildFeed={{ status: 'stale', dropped: 0, onRetry }} />,
    )
    const note = container.querySelector('.rail-note.rail-feed[role="status"][data-feed="stale"]')
    expect(note).not.toBeNull()
    expect(note.textContent).toContain(STALE_TEXT)
    const button = note.querySelector('button.chip-act')
    expect(button.getAttribute('type')).toBe('button')
    expect(button.textContent).toBe('Retry')
    fireEvent.click(button)
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(container.querySelectorAll('.rail-feed')).toHaveLength(1)
    // Placed directly above the ledger.
    expect(note.nextElementSibling?.classList.contains('rail-ledger')).toBe(true)
  })

  it('E01 row10 auth renders its sentence and Resume calls onRetry once', () => {
    const onRetry = vi.fn()
    const { container } = render(
      <JobRail mock jobs={[]} builds={FEED_BUILDS} currentJob={null} buildFeed={{ status: 'auth', dropped: 0, onRetry }} />,
    )
    const note = container.querySelector('.rail-note.rail-feed[role="status"][data-feed="auth"]')
    expect(note).not.toBeNull()
    expect(note.textContent).toContain(AUTH_TEXT)
    expect(container.querySelector('[data-feed="stale"]')).toBeNull()
    const button = note.querySelector('button.chip-act')
    expect(button.textContent).toBe('Resume')
    fireEvent.click(button)
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('E01 row11 dropped 2 renders the plural sentence, dropped 1 the singular', () => {
    const plural = render(
      <JobRail mock jobs={[]} builds={FEED_BUILDS} currentJob={null} buildFeed={{ status: 'live', dropped: 2, onRetry: vi.fn() }} />,
    )
    const many = plural.container.querySelector('.rail-note[data-feed-dropped="2"]')
    expect(many?.textContent).toBe('2 build records could not be read and are not shown.')
    expect(plural.container.querySelector('.rail-feed')).toBeNull()
    cleanup()
    const single = render(
      <JobRail mock jobs={[]} builds={FEED_BUILDS} currentJob={null} buildFeed={{ status: 'stale', dropped: 1, onRetry: vi.fn() }} />,
    )
    const one = single.container.querySelector('.rail-note[data-feed-dropped="1"]')
    expect(one?.textContent).toBe('1 build record could not be read and is not shown.')
    expect(single.container.querySelector('[data-feed="stale"]')).not.toBeNull()
  })

  it('E01 row12 spine mode renders no feed element even when stale', () => {
    const { container } = render(
      <JobRail mock jobs={[]} builds={FEED_BUILDS} currentJob={null} spine onExpand={vi.fn()}
        buildFeed={{ status: 'stale', dropped: 3, onRetry: vi.fn() }} />,
    )
    expect(container.querySelector('[data-spine="true"]')).not.toBeNull()
    expect(container.querySelector('.rail-feed')).toBeNull()
    expect(container.querySelector('[data-feed]')).toBeNull()
    expect(container.querySelector('[data-feed-dropped]')).toBeNull()
    expect(container.textContent).not.toContain(STALE_TEXT)
  })

  it('E01 row13 a stale feed keeps every card that the same props render without it', () => {
    const without = render(<JobRail mock jobs={[doneJob]} builds={FEED_BUILDS} currentJob={null} />)
    const expected = cardKeys(without.container)
    expect(expected.length).toBeGreaterThan(1)
    cleanup()
    for (const status of ['stale', 'auth']) {
      const withFeed = render(
        <JobRail mock jobs={[doneJob]} builds={FEED_BUILDS} currentJob={null} buildFeed={{ status, dropped: 1, onRetry: vi.fn() }} />,
      )
      expect(cardKeys(withFeed.container)).toEqual(expected)
      cleanup()
    }
  })
})
