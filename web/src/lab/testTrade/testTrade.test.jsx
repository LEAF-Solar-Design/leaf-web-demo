import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readTestTradeEvidence, stepStats } from './evidence.js'
import LatencyWaterfall from './LatencyWaterfall.jsx'
import TestTradePanel from './TestTradePanel.jsx'

afterEach(cleanup)
const stamps = ['book_seen', 'decision', 'money_check', 'send', 'exchange_accepted', 'fill', 'result']
const segments = duration => stamps.slice(0, 6).map((from, index) => ({ from, to: stamps[index + 1], measurement: 'measured', duration_ns: String(duration) }))
const receipt = (overrides = {}) => ({ receipt_id: 'receipt-1', status: 'filled', mode: 'live', source_sha: 'a'.repeat(40),
  requested_at: '2026-10-26T12:00:00Z', finished_at: '2026-10-26T12:00:01Z',
  fill: { fill_count: 1, price_cents: 42, fee_cents: 2, cost_cents: 44 }, segments: segments(1000000), ...overrides })
const provider = (data, flags = {}) => ({ rowsOf: vi.fn(() => [{ data, ...flags }]) })

describe('Test trade host evidence', () => {
  it('reads the newest evidence snapshot and normalizes bounded summaries', () => {
    const host = { rowsOf: vi.fn(() => [{ data: { receipts: [receipt({ status: 'old' })] } },
      { data: { receipts: [receipt()], histograms: [] }, stale: true }]) }
    const evidence = readTestTradeEvidence(host)
    expect(host.rowsOf).toHaveBeenCalledWith('evidence')
    expect(evidence.receipts[0]).toMatchObject({ status: 'filled', mode: 'live', fill: { price_cents: 42 } })
    expect(evidence.stale).toBe(true)
    expect(evidence.receipts[0].segments).toHaveLength(6)
  })

  it('never throws for missing, malformed or throwing providers', () => {
    for (const host of [undefined, {}, { rowsOf: () => { throw Error('unavailable') } },
      { rowsOf: () => null }, provider(null), provider({ receipts: [null, {}, 3], histograms: [{}] })]) {
      expect(readTestTradeEvidence(host)).toMatchObject({ receipts: [], histograms: [] })
    }
  })

  it('caps receipts and histograms and reports truncation', () => {
    const histogram = { metric: 'book-result', p50_ns: '1000', p99_ns: '2000', budget_ns: '3000',
      count: 2, observed_count: 3, over_budget: false, receipt_id: 'receipt-1' }
    const result = readTestTradeEvidence(provider({ receipts: Array(25).fill(receipt()), histograms: Array(100).fill(histogram) }))
    expect(result.receipts).toHaveLength(20)
    expect(result.histograms).toHaveLength(96)
    expect(result.truncated).toBe(true)
  })

  it('preserves host freshness flags even when the snapshot data is malformed', () => {
    expect(readTestTradeEvidence(provider(null, { stale: true, truncated: true })))
      .toEqual({ receipts: [], histograms: [], stale: true, truncated: true })
    expect(readTestTradeEvidence(provider({ receipts: { length: 21 }, histograms: { length: 97 } })).truncated).toBe(false)
  })

  it('retains the newest twenty even when they occur after the first two hundred receipts', () => {
    const receipts = Array.from({ length: 225 }, (_, index) => receipt({ receipt_id: String(index), requested_at: index }))
    const result = readTestTradeEvidence(provider({ receipts }))
    expect(result.receipts.map(item => item.receipt_id)).toEqual(Array.from({ length: 20 }, (_, index) => String(224 - index)))
    expect(result.truncated).toBe(true)
  })

  it.each(['\n', '\r', '\u2028', '\u2029'])('rejects bot stamps and source hashes with a trailing line terminator %j', suffix => {
    const result = readTestTradeEvidence(provider({ receipts: [receipt({
      source_sha: `${'a'.repeat(40)}${suffix}`,
      segments: segments(1).map((segment, index) => index === 0 ? { ...segment, duration_ns: `1${suffix}` } : segment),
    })] }))
    expect(result.receipts[0]).toMatchObject({ status: 'filled', source_sha: null, segments: null })
  })

  it('orders receipts newest first before taking the latest twenty and accepts numeric histogram stamps', () => {
    const receipts = Array.from({ length: 25 }, (_, index) => receipt({ receipt_id: String(index), requested_at: index }))
    const histogram = { metric: 'book-result', p50_ns: 1000, p99_ns: 2000, budget_ns: 3000,
      count: 2, observed_count: 3, over_budget: false, receipt_id: '24' }
    const result = readTestTradeEvidence(provider({ receipts, histograms: [histogram] }))
    expect(result.receipts).toHaveLength(20)
    expect(result.receipts[0].receipt_id).toBe('24')
    expect(result.receipts.at(-1).receipt_id).toBe('5')
    expect(result.histograms).toEqual([histogram])
  })

  it('keeps summaries but discards an incomplete, reordered or invalid segment chain', () => {
    for (const bad of [null, segments(1).slice(1), segments(1).reverse(),
      ['book_seen', 'decision', 'risk', 'order', 'sent', 'ack'].map((from, index, names) => ({
        from, to: names[index + 1] ?? 'result', measurement: 'measured', duration_ns: '1' })),
      segments(1).map((segment, index) => index === 2 ? { ...segment, duration_ns: 12 } : segment),
      segments(1).map((segment, index) => index === 2 ? { ...segment, duration_ns: null } : segment),
      segments(1).map((segment, index) => index === 2 ? { ...segment, measurement: 'not measured' } : segment),
      segments(1).map((segment, index) => index === 2 ? { ...segment, duration_ns: '-1' } : segment)]) {
      expect(readTestTradeEvidence(provider({ receipts: [receipt({ segments: bad })] })).receipts[0])
        .toMatchObject({ status: 'filled', segments: null })
    }
  })

  it('computes nearest-rank percentiles from bot stamps and counts unmeasured samples', () => {
    const missing = segments(1).map(segment => ({ ...segment, measurement: 'not measured', duration_ns: null }))
    const stats = stepStats([receipt({ segments: segments(1000000) }), receipt({ segments: segments(9000000) }),
      receipt({ segments: segments(3000000) }), receipt({ segments: missing }), receipt({ segments: null })])
    expect(stats).toHaveLength(6)
    expect(stats[0]).toMatchObject({ measuredCount: 3, notMeasuredCount: 1, p50_ms: 3, p99_ms: 9 })
    expect(stepStats([]).map(({ from, to }) => [from, to])).toEqual(stamps.slice(0, 6).map((from, index) => [from, stamps[index + 1]]))
    expect(stepStats([])[0]).toMatchObject({ measuredCount: 0, p50_ms: null, p99_ms: null })
  })

  it('retains not-measured stamps without inventing a measured sample', () => {
    const timing = segments(1).map(segment => ({ ...segment, measurement: 'not measured', duration_ns: null }))
    const result = readTestTradeEvidence(provider({ receipts: [receipt({ segments: timing })] }))
    expect(result.receipts[0].segments).toHaveLength(6)
    expect(stepStats(result.receipts)[0]).toMatchObject({ measuredCount: 0, notMeasuredCount: 1, p50_ms: null })
  })
})

describe('Test trade evidence panel', () => {
  it('shows empty states and the standalone request explanation', () => {
    render(<TestTradePanel />)
    expect(screen.getByText('No Test trades yet')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Request Test trade' })).toBeDisabled()
    expect(screen.getByText('Open the Lab inside the dashboard to place a Test trade')).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'Live vs Candidate' }))
    expect(screen.getByText('No Live run')).toBeVisible()
    expect(screen.getByText('No Candidate run')).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'Budgets' }))
    expect(screen.getByText('No budgets measured yet')).toBeVisible()
  })

  it('renders all four tabs with fills, history, separately approved sides and histogram budgets', () => {
    const histogram = { metric: 'book-result', p50_ns: '1000000', p99_ns: '2000000', budget_ns: '1500000',
      count: 2, observed_count: 3, over_budget: true, receipt_id: 'receipt-1' }
    render(<TestTradePanel provider={provider({ receipts: [receipt(), receipt({ receipt_id: 'candidate', mode: 'candidate', source_sha: 'b'.repeat(40) })],
      histograms: [histogram], truncated_receipts: 1 }, { stale: true })} />)
    expect(screen.getByText('stale')).toBeVisible()
    expect(screen.getByText('truncated')).toBeVisible()
    expect(screen.getByText(/Fill price 42 cents · Fee 2 cents · Cost 44 cents/)).toBeVisible()
    expect(screen.getByRole('list', { name: 'Bot-stamped latency waterfall' })).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'History' }))
    expect(screen.getByText(/filled · live · 2026/)).toBeVisible()
    expect(screen.getAllByText(/2 measured samples/)).toHaveLength(6)
    fireEvent.click(screen.getByRole('tab', { name: 'Live vs Candidate' }))
    expect(within(screen.getByRole('region', { name: 'Live' })).getByText('Source aaaaaaaa')).toBeVisible()
    expect(within(screen.getByRole('region', { name: 'Candidate' })).getByText('Source bbbbbbbb')).toBeVisible()
    expect(screen.getByText('Each run was separately approved.')).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'Budgets' }))
    expect(screen.getByText('p50 1 ms · p99 2 ms')).toBeVisible()
    expect(screen.getByText(/Budget p50 1.5 ms · p99 1.5 ms · over budget/)).toBeVisible()
  })

  it('updates same-provider evidence when its revision changes', () => {
    let receipts = []
    const host = { rowsOf: () => [{ data: { receipts } }] }
    const view = render(<TestTradePanel provider={host} revision={0} />)
    receipts = [receipt()]
    view.rerender(<TestTradePanel provider={host} revision={1} />)
    expect(screen.queryByText('No Test trades yet')).toBeNull()
    expect(screen.getByText(/Status filled/)).toBeVisible()
  })

  it.each([{ ok: true }, { ok: false, reason: 'not_admitted' }])('requests once through the injected callback: %j', async result => {
    const request = vi.fn().mockResolvedValue(result)
    render(<TestTradePanel requestTestTrade={request} />)
    fireEvent.click(screen.getByRole('button', { name: 'Request Test trade' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(result.ok
      ? 'Requested; the dashboard opens its Test trade card' : 'not_admitted'))
    expect(request).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledWith()
  })

  it('draws proportional bars only for measured segments', () => {
    const timing = segments(1000000)
    timing[0] = { ...timing[0], measurement: 'not measured', duration_ns: null }
    const { container } = render(<LatencyWaterfall receipt={receipt({ segments: timing })} />)
    expect(screen.getByText(/book_seen → decision: not measured/)).toBeVisible()
    expect(container.querySelectorAll('[data-waterfall-bar]')).toHaveLength(5)
    expect(container.querySelector('[data-waterfall-bar]').style.width).toBe('20%')
    expect(screen.getAllByText(/1 ms/)).toHaveLength(5)
    for (let index = 1; index < 6; index += 1) {
      expect(screen.getByText(`${stamps[index]} → ${stamps[index + 1]}: 1 ms`)).toBeVisible()
    }
  })
})
