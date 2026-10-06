// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fromBrokerJob, validateBuildRecord } from '../lib/buildQueue.js'
import JobRail from './JobRail.jsx'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

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

describe('JobRail failed-job retry', () => {
  const failedJob = { ...doneJob, job_id: 'job-failed', status: 'failed' }

  it('shows Retry only on failed jobs and calls the handler once per click with that job', () => {
    const onRetryJob = vi.fn()
    const onSelectJob = vi.fn()
    const jobs = ['submitted', 'queued', 'running', 'complete'].map((status) => ({
      ...doneJob, job_id: `job-${status}`, status,
    }))
    const failedRecord = { ...fromBrokerJob(failedJob), actions: [] }
    const { container } = render(
      <JobRail mock jobs={[...jobs, failedJob]} builds={[failedRecord]}
        onRetryJob={onRetryJob} onSelectJob={onSelectJob} />,
    )
    const retries = container.querySelectorAll('button[data-action="retry"]')
    expect(retries).toHaveLength(1)
    expect(retries[0].textContent).toBe('Retry')
    expect(retries[0].closest('.bq-card').getAttribute('data-state')).toBe('failed')
    for (const count of [1, 2]) {
      fireEvent.click(retries[0])
      expect(onRetryJob).toHaveBeenCalledTimes(count)
      expect(onRetryJob.mock.calls[count - 1]).toEqual([failedJob])
      expect(onRetryJob.mock.calls[count - 1][0]).toBe(failedJob)
    }
    expect(onSelectJob).not.toHaveBeenCalled()
  })

  it('renders no Retry without a function handler', () => {
    for (const onRetryJob of [undefined, null, true]) {
      const { container, unmount } = render(
        <JobRail mock jobs={[failedJob]} currentJob={failedJob} onRetryJob={onRetryJob} />,
      )
      expect(container.querySelectorAll('.bq-card')).toHaveLength(1)
      expect(container.querySelector('[data-action="retry"]')).toBeNull()
      unmount()
    }
  })

  it('supports the current-session failed row without duplicating it when the list catches up', () => {
    const onRetryJob = vi.fn()
    const { container, rerender } = render(
      <JobRail mock jobs={[]} currentJob={failedJob} onRetryJob={onRetryJob} />,
    )
    fireEvent.click(container.querySelector('[data-action="retry"]'))
    expect(onRetryJob).toHaveBeenCalledTimes(1)
    expect(onRetryJob).toHaveBeenLastCalledWith(failedJob)
    rerender(<JobRail mock jobs={[failedJob]} currentJob={failedJob} onRetryJob={onRetryJob} />)
    expect(container.querySelectorAll('[data-action="retry"]')).toHaveLength(1)
    fireEvent.click(container.querySelector('[data-action="retry"]'))
    expect(onRetryJob).toHaveBeenCalledTimes(2)
    expect(onRetryJob).toHaveBeenLastCalledWith(failedJob)
  })
})

describe('JobRail loading presentation', () => {
  it('delays the inferred first-fetch skeleton until 200 ms', () => {
    vi.useFakeTimers()
    const { container } = render(<JobRail jobs={[]} />)
    expect(container.querySelector('.rail-ske')).toBeNull()
    expect(container.querySelector('.rail-empty')).toBeNull()
    act(() => vi.advanceTimersByTime(199))
    expect(container.querySelector('.rail-ske')).toBeNull()
    act(() => vi.advanceTimersByTime(1))
    expect(container.querySelectorAll('.skeleton-row')).toHaveLength(2)
    act(() => vi.advanceTimersByTime(3800))
    expect(container.querySelector('.rail-ske')).toBeNull()
    expect(container.querySelector('.rail-empty')).not.toBeNull()
  })

  it('never flashes a skeleton for a fetch that finishes under 200 ms', () => {
    vi.useFakeTimers()
    const { container, rerender } = render(<JobRail jobs={[]} loading />)
    act(() => vi.advanceTimersByTime(199))
    expect(container.querySelector('.rail-ske')).toBeNull()
    rerender(<JobRail jobs={[]} loading={false} />)
    act(() => vi.advanceTimersByTime(1000))
    expect(container.querySelector('.rail-ske')).toBeNull()
    expect(container.querySelector('.rail-empty')).not.toBeNull()
  })

  it('keeps a shown skeleton visible for the shared minimum display duration', () => {
    vi.useFakeTimers()
    const { container, rerender } = render(<JobRail jobs={[]} loading />)
    act(() => vi.advanceTimersByTime(200))
    expect(container.querySelector('.rail-ske')).not.toBeNull()
    act(() => vi.advanceTimersByTime(50))
    rerender(<JobRail jobs={[]} loading={false} />)
    act(() => vi.advanceTimersByTime(349))
    expect(container.querySelector('.rail-ske')).not.toBeNull()
    expect(container.querySelector('.rail-empty')).toBeNull()
    act(() => vi.advanceTimersByTime(1))
    expect(container.querySelector('.rail-ske')).toBeNull()
    expect(container.querySelector('.rail-empty')).not.toBeNull()
  })

  it('keeps cached job, current-session and build rows mounted at 60% opacity during refetch', () => {
    vi.useFakeTimers()
    const props = {
      jobs: [doneJob],
      currentJob: { ...doneJob, job_id: 'job-current' },
      builds: [terminalRecord, { ...terminalRecord, id: 'job-older' }],
    }
    const { container, rerender } = render(<JobRail {...props} loading={false} />)
    const cards = [...container.querySelectorAll('.bq-card')]
    expect(cards).toHaveLength(3)
    expect(container.querySelector('.rail-ledger').style.opacity).toBe('')
    rerender(<JobRail {...props} loading />)
    expect(getComputedStyle(container.querySelector('.rail-ledger')).opacity).toBe('0.6')
    act(() => vi.advanceTimersByTime(10000))
    expect(container.querySelector('.rail-ske')).toBeNull()
    expect(container.querySelector('.rail-empty')).toBeNull()
    expect([...container.querySelectorAll('.bq-card')]).toEqual(cards)
    cards.forEach((card, index) => expect(container.querySelectorAll('.bq-card')[index]).toBe(card))
    rerender(<JobRail {...props} loading={false} />)
    expect(container.querySelector('.rail-ledger').style.opacity).toBe('')
  })
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
