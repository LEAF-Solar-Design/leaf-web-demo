import { useEffect } from 'react'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AfterSavePanel from './AfterSavePanel.jsx'
import { createAfterSaveClient } from './afterSaveClient.js'
import { createHostProvider } from '../host/hostProvider.js'
import GraphView from '../canvas/GraphView.jsx'
import LabWorkbench from '../LabWorkbench.jsx'
import { labArtifactsClient } from './labArtifactsClient.js'
import { addBlock, emptyGraph } from '../canvas/graphDoc.js'
import { registry } from '../canvas/registry.js'

const canvas = vi.hoisted(() => ({ proposals: [] }))
vi.mock('../canvas/LabCanvas.jsx', () => ({ default: function Canvas({ initialDocument, onChange }) {
  const document = initialDocument || emptyGraph()
  useEffect(() => { onChange({ document }) }, [onChange, initialDocument])
  return <button onClick={() => { const draft = { document }; canvas.proposals.push(draft); onChange(draft) }}>Change draft</button>
} }))

const at = 1800000000000
const saved = { revision: 2, sha256: 'saved-sha', at }
const side = { trades: 12, net_after_fees_cents: 123456.5, net_per_dollar_risked: 1.234,
  worst_trade_cents: -9876, without_best_and_worst_cents: 34567,
  costs: { fees_cents: 123, slippage_cents: 456 }, resized: 3, skipped: 4, new_buys: 5 }
const first = addBlock(emptyGraph(), registry['adjust-none@1.0.0'])
const draft = { ...first, blocks: first.blocks.map(block => ({ ...block, settings: { calibration_intercept: 0.77 } })) }
const row = (patch = {}) => ({ state: 'finished', revision: 2, document_sha256: saved.sha256,
  created_at: '2027-01-15T08:00:00Z', window: { start: '2027-01-15T04:00:00Z', end: '2027-01-15T08:00:00Z' },
  coverage: { markets_covered: 8, gaps: 0, tape_hours_covered: 4 },
  candidate: side, live: { ...side, net_after_fees_cents: 120000 },
  deltas: { ...side, net_after_fees_cents: 3456.5 }, latency: { p99_us: 2345 },
  blocks: [{ block_id: draft.blocks[0].id, registry_id: 'adjust-none', triggered: 0, reached: 0 }],
  attribution: [{ trade_id: 'trade-1', reason: 'Resized', candidate_cents: 999, live_cents: 500, delta_cents: 499 }], ...patch })

function fakeArtifactsClient({ state: overrides = {}, ...methods } = {}) {
  const listeners = new Set()
  let operation = 0
  const state = { head: null, published: null, revisions: [], lastSaved: null, conflict: null,
    restoredGraph: null, restoredKey: 0, events: [], saveTimes: [], requestTimes: [],
    headLoaded: true, headLookupFailed: false, ...overrides }
  return {
    state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    update(patch) { Object.assign(this.state, patch); listeners.forEach(listener => listener()) },
    currentDocId: () => 'strategy', currentGeneration: () => 0,
    beginGraphOperation: vi.fn(() => ++operation),
    isCurrentOperation: token => token === operation,
    captureMine: vi.fn(), setDocId: vi.fn(), takeGraph: vi.fn(), save: vi.fn(),
    head: vi.fn(async () => { state.headLoaded = true; return { ok: true, revision: state.head } }),
    revisions: vi.fn().mockResolvedValue({ ok: true, revisions: [] }),
    diff: vi.fn().mockResolvedValue({ ok: true, changes: [{ op: 'replace', path: '/blocks/0/settings/calibration_intercept', before: 0, after: 0.77 }] }),
    revision: vi.fn().mockResolvedValue({ ok: true, graph: draft }),
    restore: vi.fn(), publish: vi.fn(),
    ...methods,
  }
}
function fixture(lastSaved = saved) {
  const provider = createHostProvider({ framed: true })
  const client = fakeArtifactsClient({ state: { lastSaved, head: lastSaved?.revision,
    revisions: [{ revision: 1, graph: first }] } })
  const afterSave = createAfterSaveClient({ provider, artifacts: client, now: () => at + 7000 })
  const announce = (...data) => provider.announce(data.map(value => ({ source: 'practice', data: value, as_of: at })))
  return { provider, client, afterSave, announce }
}
const originalState = { ...labArtifactsClient.state }
afterEach(() => {
  cleanup()
  Object.assign(labArtifactsClient.state, originalState)
  canvas.proposals.length = 0
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('After save read model', () => {
  it('has no result before a save, then reports elapsed pending time and notifies subscribers', () => {
    const { client, afterSave } = fixture(null)
    expect(afterSave.read()).toMatchObject({ saved: null, match: null, status: 'none', elapsed_ms: 0 })
    render(<AfterSavePanel view={afterSave.read()} document={draft} />)
    expect(screen.getByText('Save to run a Practice of the last 4 hours.')).toBeVisible()
    expect(screen.getByRole('region', { name: 'Live versus predicted gap' })).toHaveTextContent('not measured (no matched comparison receipt)')
    expect(screen.queryByText(/Simulated replay, not a verdict/)).toBeNull()
    const listener = vi.fn()
    const unsubscribe = afterSave.subscribe(listener)
    client.update({ lastSaved: saved })
    expect(listener).toHaveBeenCalledTimes(1)
    expect(afterSave.read()).toMatchObject({ status: 'pending', elapsed_ms: 7000 })
    unsubscribe()
    client.update({ lastSaved: null })
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('ignores other hashes and re-reads the newest matching practice row without mutating provider order', () => {
    const { announce, afterSave, provider } = fixture()
    announce(row({ document_sha256: 'other' }))
    expect(afterSave.read()).toMatchObject({ status: 'pending', match: null })
    announce(row(), row({ state: 'running', created_at: '2027-01-15T08:01:00Z' }),
      row({ state: 'failed', created_at: '2027-01-15T07:59:00Z' }))
    expect(afterSave.read().status).toBe('running')
    expect(provider.rowsOf('practice')[0].data.state).toBe('finished')
    announce(row({ state: 'queued' }))
    expect(afterSave.read().status).toBe('pending')
  })

  it('exposes frozen copies for announced sources and an empty frozen array for absent sources', () => {
    const { provider, announce } = fixture()
    const source = row()
    announce(source)
    const rows = provider.rowsOf('practice')
    expect(Object.isFrozen(rows)).toBe(true)
    expect(Object.isFrozen(rows[0])).toBe(true)
    expect(Object.isFrozen(rows[0].data.candidate)).toBe(true)
    expect(provider.rowsOf('practice')[0]).not.toBe(rows[0])
    source.candidate = { trades: 999 }
    expect(rows[0].data.candidate.trades).toBe(12)
    expect(rows[0]).toMatchObject({ as_of: at, stale: false, truncated: false })
    expect(provider.rowsOf('unknown')).toEqual([])
    expect(Object.isFrozen(provider.rowsOf('unknown'))).toBe(true)
  })
})

describe('After save comparison', () => {
  it('outlines a saved block when its registry version changes', async () => {
    const { announce, afterSave, client } = fixture()
    const graph = addBlock(first, registry['adjust-none@1.0.0'])
    graph.blocks[1].registry = { ...graph.blocks[1].registry, version: '2.0.0' }
    client.revision.mockResolvedValue({ ok: true, graph })
    client.diff.mockResolvedValue({ ok: true, changes: [
      { op: 'replace', path: '/blocks/1/registry/version', before: '1.0.0', after: '2.0.0' },
    ] })
    announce(row({ blocks: graph.blocks.map(block => ({ block_id: block.id, triggered: 1 })) }))
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().changed.blockIds).toEqual([graph.blocks[1].id])
    render(<AfterSavePanel view={afterSave.read()} document={graph} />)
    const blocks = within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')
    expect(blocks[0]).not.toHaveAttribute('data-changed')
    expect(blocks[1]).toHaveAttribute('data-changed', 'true')
  })

  it('outlines a whole-block replacement with changed settings', async () => {
    const { afterSave, client } = fixture()
    client.diff.mockResolvedValue({ ok: true, changes: [
      { op: 'replace', path: '/blocks/0', before: first.blocks[0], after: draft.blocks[0] },
    ] })
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().changed.blockIds).toEqual([draft.blocks[0].id])
  })

  it('marks both saved pipe endpoints and the previous node when a pipe target changes', async () => {
    const { afterSave, client } = fixture()
    const graph = addBlock(addBlock(first, registry['adjust-none@1.0.0']), registry['adjust-none@1.0.0'])
    const [a, b, c] = graph.blocks.map(block => block.id)
    graph.pipes = [{ from: { node: a, port: 'result' }, to: { node: c, port: 'value' } }]
    client.revision.mockResolvedValue({ ok: true, graph })
    client.diff.mockResolvedValue({ ok: true, changes: [
      { op: 'replace', path: '/pipes/0/to/node', before: b, after: c },
    ] })
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().changed.blockIds).toEqual([a, b, c])
  })

  it('marks only the remaining endpoint when a block and its pipe are removed', async () => {
    const { afterSave, client } = fixture()
    const before = addBlock(first, registry['adjust-none@1.0.0'])
    const [a, b] = before.blocks.map(block => block.id)
    const graph = { ...before, blocks: [before.blocks[1]], pipes: [] }
    client.revision.mockResolvedValue({ ok: true, graph })
    client.diff.mockResolvedValue({ ok: true, changes: [
      { op: 'replace', path: '/blocks', before: before.blocks, after: graph.blocks },
      { op: 'replace', path: '/pipes', before: [{ from: { node: a, port: 'result' }, to: { node: b, port: 'value' } }], after: [] },
    ] })
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().changed.blockIds).toEqual([b])
  })

  it('shares revision requests and cached failures across clients over the same artifacts instance', async () => {
    for (const failed of [false, true]) {
      const { afterSave, client, provider } = fixture()
      if (failed) client.diff.mockRejectedValue(new Error('Offline'))
      afterSave.read()
      const second = createAfterSaveClient({ provider, artifacts: client })
      const listener = vi.fn()
      const unsubscribe = second.subscribe(listener)
      second.read()
      await act(async () => {})
      expect(listener).toHaveBeenCalledTimes(1)
      expect(second.read().savedDocument).toBe(draft)
      expect(second.read().changed).toEqual(afterSave.read().changed)
      if (failed) expect(second.read().changed).toBeNull()
      unsubscribe()
      const remounted = createAfterSaveClient({ provider, artifacts: client })
      remounted.read()
      await act(async () => {})
      expect(client.diff).toHaveBeenCalledExactlyOnceWith('strategy', 1, 2, { graphs: false })
      expect(client.revision).toHaveBeenCalledExactlyOnceWith('strategy', 2)
    }
  })

  it('retains the saved graph and block list when the diff rejects', async () => {
    const { announce, afterSave, client } = fixture()
    client.diff.mockRejectedValue(new Error('Offline'))
    announce(row())
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().savedDocument).toBe(draft)
    expect(afterSave.read().changed).toBeNull()
    render(<AfterSavePanel view={afterSave.read()} document={emptyGraph()} />)
    expect(screen.getByText('Changed blocks unavailable')).toBeVisible()
    const blocks = within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')
    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toHaveTextContent('Strategy: triggered 0 times in the window, reached 0')
    expect(blocks[0]).not.toHaveAttribute('data-changed')
  })

  it('uses the saved registry title when a titleless draft changes registry', async () => {
    const { announce, afterSave } = fixture()
    const document = { ...draft, blocks: draft.blocks.map(block => ({ ...block, title: '',
      registry: { ...block.registry, id: 'exits-value-vs-hold-v1' } })) }
    announce(row())
    afterSave.read()
    await act(async () => {})
    const mount = render(<AfterSavePanel view={afterSave.read()} document={document} />)
    const blocks = () => within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')
    expect(blocks()[0]).toHaveTextContent('Strategy: triggered 0 times')
    expect(blocks()[0]).not.toHaveTextContent('Cash out')
    const savedDocument = { ...draft, blocks: draft.blocks.map(block => ({ ...block,
      registry: { ...block.registry, version: '2.0.0' } })) }
    mount.rerender(<AfterSavePanel view={{ ...afterSave.read(), savedDocument }} document={document} />)
    expect(blocks()[0]).toHaveTextContent('adjust-none: triggered 0 times')
  })

  it('formats both sides, deltas, timing, changed and unreached blocks, attribution and coverage', async () => {
    const { announce, afterSave } = fixture()
    announce(row())
    afterSave.read()
    await act(async () => {})
    render(<AfterSavePanel view={afterSave.read()} document={draft} />)
    const candidate = screen.getByRole('heading', { name: 'Candidate (simulated)' }).closest('article')
    const live = screen.getByRole('heading', { name: 'Live', exact: true }).closest('article')
    expect(candidate).toHaveAttribute('data-side', 'candidate')
    expect(candidate.style.color).toBe('var(--leaf-color-data-series-4)')
    expect(live.style.color).toBe('var(--leaf-color-data-series-1)')
    expect(within(candidate).getByText('$1,234.57')).toBeVisible()
    expect(within(candidate).getByText('1.23')).toBeVisible()
    expect(within(candidate).getByText('-$98.76')).toBeVisible()
    expect(within(candidate).getByText('Without best and worst Trade: $345.67')).toBeVisible()
    expect(within(live).getByText('$1,200.00')).toBeVisible()
    expect(within(screen.getByRole('heading', { name: 'Deltas' }).closest('article')).getByText('$34.57')).toBeVisible()
    const gap = screen.getByRole('region', { name: 'Live versus predicted gap' })
    expect(gap).toHaveTextContent('Live versus predicted gap: not measured (no matched comparison receipt)')
    expect(within(gap).getByText('Simulated replay, not a verdict · Candidate-minus-Live net delta: $34.57')).toBeVisible()
    expect(gap.compareDocumentPosition(screen.getByRole('heading', { name: 'Deltas' })) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
    expect(screen.getByText('p99 decision time 2.35 ms')).toBeVisible()
    const block = screen.getByText('Strategy: triggered 0 times in the window, reached 0')
    expect(block).toHaveAttribute('data-changed', 'true')
    expect(block.style.outline).toContain('var(--leaf-color-data-series-3)')
    expect(screen.getByText('trade-1: Resized; Candidate $9.99; Live $5.00; Delta $4.99')).toBeVisible()
    expect(screen.getByText(/Simulated, not a verdict\. Window 2027-01-15T04:00:00Z\.\.2027-01-15T08:00:00Z, 8 Markets, 0 gaps\./)).toBeVisible()
  })

  it('bounds attribution to 50 rows and notes truncation while changed blocks are loading', () => {
    const { announce, afterSave, client } = fixture()
    client.state.revisions = []
    announce(row({ attribution: Array.from({ length: 51 }, (_, index) => ({ trade_id: `trade-${index}` })) }))
    render(<AfterSavePanel view={afterSave.read()} document={draft} />)
    expect(within(screen.getByRole('list', { name: 'Per-Trade attribution' })).getAllByRole('listitem')).toHaveLength(50)
    expect(screen.getByText('truncated')).toBeVisible()
    expect(screen.getByText(/triggered 0 times/)).not.toHaveAttribute('data-changed')
  })

  it('outlines only the saved r2 diff block after metadata history loads and later draft edits', async () => {
    const { announce, afterSave, client } = fixture()
    const graph = addBlock(first, registry['adjust-none@1.0.0'])
    client.state.revisions = [{ revision: 1 }, { revision: 2 }]
    client.diff.mockResolvedValue({ ok: true, changes: [
      { op: 'replace', path: '/blocks/1/settings/field', before: 'price', after: 'size' },
    ] })
    client.revision.mockResolvedValue({ ok: true, graph })
    announce(row({ blocks: graph.blocks.map(block => ({ block_id: block.id, triggered: 1, reached: 2 })) }))
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().changed).toEqual({ revision: 2, blockIds: [graph.blocks[1].id] })
    expect(afterSave.read().savedDocument).toBe(graph)
    const mount = render(<AfterSavePanel view={afterSave.read()} document={graph} />)
    const blocks = () => within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')
    expect(blocks()[0]).not.toHaveAttribute('data-changed')
    expect(blocks()[1]).toHaveAttribute('data-changed', 'true')
    const laterDraft = { ...graph, blocks: graph.blocks.map((block, index) => index === 0
      ? { ...block, title: 'Later draft', settings: { field: 'edited again' } } : block) }
    mount.rerender(<AfterSavePanel view={afterSave.read()} document={laterDraft} />)
    expect(blocks()[0]).toHaveTextContent('Later draft')
    expect(blocks()[0]).not.toHaveAttribute('data-changed')
    expect(blocks()[1]).toHaveAttribute('data-changed', 'true')
    mount.rerender(<AfterSavePanel view={afterSave.read()} document={emptyGraph()} />)
    expect(blocks()[1]).toHaveTextContent('Strategy')
    expect(blocks()[1]).toHaveAttribute('data-changed', 'true')
    client.update({ revisions: [{ revision: 2 }] })
    afterSave.read()
    expect(client.diff).toHaveBeenCalledExactlyOnceWith('strategy', 1, 2, { graphs: false })
    expect(client.revision).toHaveBeenCalledExactlyOnceWith('strategy', 2)
  })

  it('outlines every saved block for r1 without requesting a parent diff', async () => {
    const { announce, afterSave, client } = fixture({ ...saved, revision: 1 })
    const graph = addBlock(first, registry['adjust-none@1.0.0'])
    client.revision.mockResolvedValue({ ok: true, graph })
    announce(row({ revision: 1, blocks: graph.blocks.map(block => ({ block_id: block.id, triggered: 0, reached: 0 })) }))
    afterSave.read()
    await act(async () => {})
    expect(afterSave.read().changed).toEqual({ revision: 1, blockIds: graph.blocks.map(block => block.id) })
    render(<AfterSavePanel view={afterSave.read()} document={graph} />)
    for (const block of within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')) {
      expect(block).toHaveAttribute('data-changed', 'true')
    }
    expect(client.diff).not.toHaveBeenCalled()
    expect(client.revision).toHaveBeenCalledExactlyOnceWith('strategy', 1)
  })

  it.each(['diff', 'revision'])('keeps Practice numbers when the %s fetch fails and reports changed blocks unavailable', async request => {
    const { announce, afterSave, client } = fixture()
    client[request].mockRejectedValue(new Error('Offline'))
    announce(row())
    const listener = vi.fn()
    const unsubscribe = afterSave.subscribe(listener)
    afterSave.read()
    await act(async () => {})
    expect(listener).toHaveBeenCalledTimes(1)
    expect(afterSave.read().changed).toBeNull()
    render(<AfterSavePanel view={afterSave.read()} document={draft} />)
    expect(screen.getByText('Changed blocks unavailable')).toBeVisible()
    expect(within(screen.getByRole('heading', { name: 'Candidate (simulated)' }).closest('article')).getByText('$1,234.57')).toBeVisible()
    expect(screen.getByText('p99 decision time 2.35 ms')).toBeVisible()
    expect(screen.getByText(/triggered 0 times/)).not.toHaveAttribute('data-changed')
    afterSave.read()
    expect(client.diff).toHaveBeenCalledTimes(1)
    expect(client.revision).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('shows exact unavailable coverage and reason with no Side statistics', () => {
    const { announce, afterSave } = fixture()
    announce(row({ state: 'unavailable', reason: 'Missing tape', coverage: { markets_covered: 8, gaps: 3 } }))
    render(<AfterSavePanel view={afterSave.read()} document={draft} />)
    expect(screen.getByText('Missing tape')).toBeVisible()
    expect(screen.getByText('No statistics: tape unavailable.')).toBeVisible()
    expect(screen.getByText(/Window 2027-01-15T04:00:00Z\.\.2027-01-15T08:00:00Z, 8 Markets, 3 gaps/)).toBeVisible()
    expect(screen.queryByText('$1,234.57')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Deltas' })).not.toBeInTheDocument()
  })

  it.each([['failed', 'Practice failed: Tape worker stopped'], ['expired', 'Practice request expired.']])('renders %s', (state, text) => {
    const { announce, afterSave } = fixture()
    announce(row({ state, reason: 'Tape worker stopped' }))
    render(<AfterSavePanel view={afterSave.read()} document={draft} />)
    expect(screen.getByText(text)).toBeVisible()
  })
})

describe('Strategy mounts', () => {
  it('ticks pending time after a save and refreshes on a provider revision without leaking results into canvas drafts', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(at)
    const { provider, client, announce } = fixture(null)
    const mount = render(<GraphView initialDocument={draft} provider={provider} revision={0} client={client} />)
    await act(async () => { await vi.dynamicImportSettled() })
    act(() => client.update({ lastSaved: saved }))
    expect(screen.getByText('Practice pending · 0 s')).toBeVisible()
    act(() => vi.advanceTimersByTime(3000))
    expect(screen.getByText('Practice pending · 3 s')).toBeVisible()
    announce(row())
    mount.rerender(<GraphView initialDocument={draft} provider={provider} revision={1} client={client} />)
    expect(screen.getByRole('heading', { name: 'Candidate (simulated)' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Change draft' }))
    const serialized = JSON.stringify(canvas.proposals.at(-1))
    expect(canvas.proposals.at(-1).document).toEqual(draft)
    for (const value of ['123456.5', '-9876', '34567', '1.234', 'net_after_fees_cents']) expect(serialized).not.toContain(value)
  })

  it('shows the shared save bar and comparison in the embedded Strategy mount', async () => {
    Object.assign(labArtifactsClient.state, { lastSaved: saved })
    let callbacks
    const createHostBridge = options => {
      callbacks = options
      return { start: async () => {}, stop() {}, state: {}, proposal: async () => {} }
    }
    render(<LabWorkbench route={{ view: 'graph' }} status={{ state: 'on' }} embedded createHostBridge={createHostBridge} />)
    await act(async () => { await vi.dynamicImportSettled() })
    expect(screen.getByRole('region', { name: 'Save Strategy' })).toBeVisible()
    expect(screen.getByRole('region', { name: 'After save' })).toBeVisible()
    act(() => callbacks.onSource([{ source: 'practice', data: row(), as_of: at }]))
    expect(screen.getByRole('heading', { name: 'Candidate (simulated)' })).toBeVisible()
  })
})
