import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readTestTradeEvidence, stepStats } from './evidence.js'
import LatencyWaterfall from './LatencyWaterfall.jsx'
import TestTradePanel from './TestTradePanel.jsx'

afterEach(cleanup)
const stamps = ['book_seen', 'decision', 'money_check', 'send', 'exchange_accepted', 'fill', 'result']
const segments = duration => stamps.slice(0, 6).map((from, index) => ({ from, to: stamps[index + 1], measurement: 'measured', duration_ns: String(duration) }))
const receipt = (overrides = {}) => ({ kind: 'receipt', receipt_id: 'receipt-1', status: 'filled', mode: 'Live', source_sha: 'a'.repeat(40),
  requested_at: '2026-10-26T12:00:00Z', finished_at: '2026-10-26T12:00:01Z',
  fill: { fill_count: '0.25', price_cents: 42, fee_cents: 2, cost_cents: 44 }, segments: segments(1000000), ...overrides })
const provider = (values, flags = {}) => ({ rowsOf: vi.fn(() => values.map(data => ({ data, ...flags }))) })

describe('Test trade host evidence', () => {
  it('deduplicates receipt rows by newest requested time regardless of row order', () => {
    const host = { rowsOf: vi.fn(() => [{ data: receipt(), stale: true },
      { data: receipt({ status: 'old', requested_at: '2026-10-25T12:00:00Z' }) }]) }
    const evidence = readTestTradeEvidence(host)
    expect(host.rowsOf).toHaveBeenCalledWith('evidence')
    expect(evidence.receipts).toHaveLength(1)
    expect(evidence.receipts[0]).toMatchObject({ status: 'filled', mode: 'live', fill: { fill_count: '0.25', price_cents: 42 } })
    expect(evidence.stale).toBe(true)
    expect(evidence.receipts[0].segments).toHaveLength(6)
  })

  it('keeps the most advanced update when rows share a receipt id and requested time', () => {
    const pending = receipt({ status: 'pending', finished_at: null, fill: null, segments: null })
    const filled = receipt()
    for (const rows of [[filled, pending], [pending, filled]]) {
      const evidence = readTestTradeEvidence(provider(rows))
      expect(evidence.receipts).toHaveLength(1)
      expect(evidence.receipts[0]).toMatchObject({ status: 'filled', fill: { fill_count: '0.25' } })
    }
    const evidence = readTestTradeEvidence(provider([{ ...filled, status: 'first' }, { ...filled, status: 'second' }]))
    expect(evidence.receipts[0].status).toBe('first')
  })

  it('never throws for missing, malformed or throwing providers', () => {
    for (const host of [undefined, {}, { rowsOf: () => { throw Error('unavailable') } },
      { rowsOf: () => null }, provider([null, {}, 3, { kind: 'receipt' }, { kind: 'histogram' }])]) {
      expect(readTestTradeEvidence(host)).toMatchObject({ receipts: [], histograms: [] })
    }
  })

  it('skips malformed histogram rows without losing valid evidence and preserves fill strings', () => {
    const histogram = { kind: 'histogram', metric: 'book-result', p50_ns: '1000', p99_ns: '2000',
      budget_ns: { p50: '1500', p99: '2500' }, over_budget: { p50: false, p99: false },
      count: 1, observed_count: 1, receipt_id: 'receipt-1' }
    const result = readTestTradeEvidence(provider([null, { kind: 'receipt' }, receipt(),
      { ...histogram, budget_ns: '1500' }, { ...histogram, over_budget: false },
      { ...histogram, p50_ns: 1000 }, { ...histogram, count: -1 }, histogram]))
    expect(result.receipts).toHaveLength(1)
    expect(result.receipts[0].fill.fill_count).toBe('0.25')
    expect(result.histograms).toHaveLength(1)
    for (const fill_count of ['1', '0', '0.2500']) {
      expect(readTestTradeEvidence(provider([receipt({ fill: { fill_count, price_cents: 42, fee_cents: -1, cost_cents: 41 } })])).receipts[0].fill.fill_count).toBe(fill_count)
    }
    expect(readTestTradeEvidence(provider([receipt({ fill: { ...receipt().fill, fill_count: 1 } })])).receipts[0].fill).toBeNull()
  })

  it('keeps receipt-only snapshot compatibility for older embedded hosts', () => {
    expect(readTestTradeEvidence(provider([{ receipts: [receipt()], histograms: [] }])).receipts[0])
      .toMatchObject({ receipt_id: 'receipt-1', status: 'filled' })
  })

  it('caps receipts and histograms and reports truncation', () => {
    const histogram = { kind: 'histogram', metric: 'book-result', p50_ns: '1000', p99_ns: '2000', budget_ns: { p50: '3000', p99: '4000' },
      count: 2, observed_count: 3, over_budget: { p50: false, p99: false }, receipt_id: 'receipt-1' }
    const result = readTestTradeEvidence(provider([...Array.from({ length: 25 }, (_, index) => receipt({ receipt_id: String(index) })), ...Array(100).fill(histogram)]))
    expect(result.receipts).toHaveLength(20)
    expect(result.histograms).toHaveLength(96)
    expect(result.truncated).toBe(true)
  })

  it('preserves host freshness flags even when row data is malformed', () => {
    expect(readTestTradeEvidence(provider([null], { stale: true, truncated: true })))
      .toEqual({ receipts: [], histograms: [], stale: true, truncated: true })
    expect(readTestTradeEvidence({ rowsOf: () => [{ data: null, stale: true }, { data: { kind: 'controller' }, truncated: true }, { data: receipt() }] }))
      .toMatchObject({ stale: true, truncated: true })
  })

  it('retains the newest twenty even when they occur after the first two hundred receipts', () => {
    const receipts = Array.from({ length: 225 }, (_, index) => receipt({ receipt_id: String(index), requested_at: index }))
    const result = readTestTradeEvidence(provider(receipts))
    expect(result.receipts.map(item => item.receipt_id)).toEqual(Array.from({ length: 20 }, (_, index) => String(224 - index)))
    expect(result.truncated).toBe(true)
  })

  it.each(['\n', '\r', '\u2028', '\u2029'])('rejects bot stamps and source hashes with a trailing line terminator %j', suffix => {
    const result = readTestTradeEvidence(provider([receipt({
      source_sha: `${'a'.repeat(40)}${suffix}`,
      segments: segments(1).map((segment, index) => index === 0 ? { ...segment, duration_ns: `1${suffix}` } : segment),
    })]))
    expect(result.receipts[0]).toMatchObject({ status: 'filled', source_sha: null, segments: null })
  })

  it('orders receipts newest first before taking twenty and retains separate histogram budgets', () => {
    const receipts = Array.from({ length: 25 }, (_, index) => receipt({ receipt_id: String(index), requested_at: index }))
    const histogram = { metric: 'book-result', p50_ns: '1000', p99_ns: '2000', budget_ns: { p50: '3000', p99: '4000' },
      count: 2, observed_count: 3, over_budget: { p50: false, p99: true }, receipt_id: '24' }
    const result = readTestTradeEvidence(provider([...receipts, { kind: 'histogram', ...histogram }]))
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
      expect(readTestTradeEvidence(provider([receipt({ segments: bad })])).receipts[0])
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
    const result = readTestTradeEvidence(provider([receipt({ segments: timing })]))
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
    const histogram = { kind: 'histogram', metric: 'book-result', p50_ns: '1000000', p99_ns: '2000000', budget_ns: { p50: '1500000', p99: '1750000' },
      count: 2, observed_count: 3, over_budget: { p50: false, p99: true }, receipt_id: 'receipt-1' }
    render(<TestTradePanel provider={provider([receipt(), receipt({ receipt_id: 'candidate', mode: 'Candidate', source_sha: 'b'.repeat(40) }),
      histogram], { stale: true, truncated: true })} />)
    expect(screen.getByText('stale')).toBeVisible()
    expect(screen.getByText('truncated')).toBeVisible()
    expect(screen.getByText(/Fill price 42 cents · Fee 2 cents · Cost 44 cents/)).toBeVisible()
    expect(screen.getByText('Fill count 0.25')).toBeVisible()
    expect(screen.getByRole('list', { name: 'Bot-stamped latency waterfall' })).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'History' }))
    expect(screen.getByText(/filled · live · 2026/)).toBeVisible()
    expect(screen.getAllByText(/2 measured samples/)).toHaveLength(6)
    fireEvent.click(screen.getByRole('tab', { name: 'Live vs Candidate' }))
    expect(within(screen.getByRole('region', { name: 'Live' })).getByText('Source aaaaaaaa')).toBeVisible()
    expect(within(screen.getByRole('region', { name: 'Candidate' })).getByText('Source bbbbbbbb')).toBeVisible()
    expect(screen.getAllByText('Fill count 0.25')).toHaveLength(2)
    expect(screen.getByText('Each run was separately approved.')).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'Budgets' }))
    expect(screen.getByText('p50 1 ms · p99 2 ms')).toBeVisible()
    expect(screen.getByText(/Budget p50 1.5 ms · p99 1.75 ms · over budget/)).toBeVisible()
    expect(screen.getByText(/p50 within budget · p99 over budget/)).toBeVisible()
  })

  it('updates same-provider evidence when its revision changes', () => {
    let receipts = []
    const host = { rowsOf: () => receipts.map(data => ({ data })) }
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
