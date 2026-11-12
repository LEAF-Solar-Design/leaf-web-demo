import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RunsView from './RunsView.jsx'
import { createValueStore } from './valueStore.js'
import { config } from '../../api.js'
import { addBlock, connectPipe, emptyGraph, updateSettings } from '../canvas/graphDoc.js'
import { registry } from '../canvas/registry.js'

const surface = vi.hoisted(() => ({ current: null }))
vi.mock('../useLabSurfaceStatus.js', () => ({ useLabSurfaceStatus: () => surface.current }))
afterEach(() => { cleanup(); surface.current = null; vi.restoreAllMocks() })
const seed = () => {
  const store = createValueStore()
  store.upsertRun('r', { state: 'running', graph_revision: 4 })
  store.apply('r', { seq: 0, kind: 'value', block_id: 'count', payload: { result: { data: 3, as_of: Date.now() - 120000 } } })
  return store
}
const evidenceRun = (confirmation = true) => {
  const store = createValueStore()
  const digest = 'a'.repeat(64), parent = 'b'.repeat(64), rows = 'c'.repeat(64)
  const report = { schema: 'leafhft.block-suite-result.v1', suite_sha256: 'd'.repeat(64), rows_sha256: rows,
    candidates: [{ child_min_k: 1, select_tickets: 7, select_total_c: -0.0000000000001 }],
    chosen: confirmation ? { child_min_k: 1, select_tickets: 7 } : null,
    verdict: confirmation ? 'fail' : 'no_candidate', reasons: ['selection and confirmation remain distinct'],
    confirm_report: confirmation ? { verdict: 'insufficient', reasons: ['too few confirmation tickets'],
      parent: { select: { n_tickets: 7 }, confirm: { n_tickets: 2 } },
      child: { select: { n_tickets: 7 }, confirm: { n_tickets: 2 } },
      calibration: { parent: { n: 0, mean_claimed_prob: null, win_rate: null },
        child: { n: 2, mean_claimed_prob: 0.5000000000000001, win_rate: 0 } },
      confirm_comparison: { paired_ci95_c: [null, null], child_minus_parent_mean_c: -0.0000000000001 },
      confirm_tickets: { parent: [], child: [{ market_id: 'synthetic-final-ticket' }] },
      protocol: { draws: 10, seed: 7 }, splits: { select: { rows: 30 }, confirm: { rows: 20 } } } : null }
  const evidence = { policy: { schema: 'leaf.lab-corpus-policy.v1', version: 1, digest,
    parent_suite_sha256: parent, parameters: { min_select_tickets: 0 },
    lineage: { doc_id: 'saved-composition', graph_revision: 2, graph_sha256: 'e'.repeat(64) } },
    policy_digest: digest, evaluated_suite_sha256: report.suite_sha256,
    context: { corpus: { material_id: 'synthetic-corpus', sha256: rows },
      suite: { material_id: 'synthetic-suite', sha256: parent } },
    row_count: 80, rows_sha256: rows, result_sha256: 'f'.repeat(64),
    run_id: 'evaluation', job_id: 'durable-evaluation', worker_id: 'evaluator', attempt: 2, evaluation: report }
  store.upsertRun('evaluation', { state: 'running', graph_revision: 2 })
  store.apply('evaluation', { seq: 0, kind: 'value', block_id: 'corpus', payload: { result: { data: JSON.stringify(evidence) } } })
  store.apply('evaluation', { seq: 1, kind: 'finished', payload: {} })
  return { store, evidence }
}

describe('Runs view', () => {
  it('displays complete policy and evaluation evidence without rounding an insufficient outcome into success', () => {
    const { store, evidence } = evidenceRun()
    render(<RunsView store={store} mock />)
    const section = within(screen.getByRole('region', { name: 'Corpus evaluation evidence' }))
    expect(section.getByText(`Policy digest: ${evidence.policy_digest}`)).toBeVisible()
    expect(section.getByText(`Parent suite digest: ${evidence.policy.parent_suite_sha256}`)).toBeVisible()
    expect(section.getByText('Policy version: 1')).toBeVisible()
    expect(section.getByText('Verdict: fail')).toBeVisible()
    expect(section.getByText('Confirmation verdict: insufficient')).toBeVisible()
    const field = label => section.getByRole('heading', { name: label }).nextElementSibling
    expect(JSON.parse(field('Policy and graph lineage').textContent)).toEqual(evidence.policy)
    expect(JSON.parse(field('Selection evidence').textContent)).toEqual({
      candidates: evidence.evaluation.candidates, chosen: evidence.evaluation.chosen })
    expect(JSON.parse(field('Calibration').textContent)).toEqual(evidence.evaluation.confirm_report.calibration)
    expect(JSON.parse(field('Uncertainty').textContent)).toEqual(evidence.evaluation.confirm_report.confirm_comparison)
    expect(field('Confirmation reasons')).toHaveTextContent('too few confirmation tickets')
    expect(field('Input identities')).toHaveTextContent('synthetic-corpus')
    expect(field('Input identities')).toHaveTextContent('durable-evaluation')
    fireEvent.click(section.getByText('Complete evaluation evidence'))
    expect(field('Evaluation report')).toBeVisible()
    expect(JSON.parse(field('Evaluation report').textContent)).toEqual(evidence.evaluation)
  })
  it('keeps missing confirmation, calibration and uncertainty explicit after completion', () => {
    const { store } = evidenceRun(false)
    render(<RunsView store={store} mock />)
    const section = within(screen.getByRole('region', { name: 'Corpus evaluation evidence' }))
    expect(section.getByText('Verdict: no_candidate')).toBeVisible()
    expect(section.getByText('Confirmation verdict: Unavailable (not evaluated)')).toBeVisible()
    for (const label of ['Confirmation evidence', 'Calibration', 'Uncertainty']) {
      expect(section.getByRole('heading', { name: label }).nextElementSibling).toHaveTextContent('Unavailable (not evaluated)')
    }
    expect(section.queryByText('Verdict: pass')).toBeNull()
  })
  it('rejects invalid composition edits while retaining the last composition and completed evidence', () => {
    const { store, evidence } = evidenceRun()
    const manifest = registry['constant@1.0.0']
    let graph = addBlock(emptyGraph(), manifest)
    graph = updateSettings(graph, graph.blocks[0].id, { value: 3 })
    const previous = JSON.stringify(graph)
    const previousRun = store.getRun('evaluation')
    expect(() => { graph = updateSettings(graph, graph.blocks[0].id, { value: 'invalid' }) }).toThrow()
    expect(() => { graph = connectPipe(graph, {
      from: { node: graph.blocks[0].id, port: 'result' }, to: { node: graph.blocks[0].id, port: 'missing' },
    }) }).toThrow()
    expect(JSON.stringify(graph)).toBe(previous)
    expect(store.getRun('evaluation')).toBe(previousRun)
    render(<RunsView store={store} mock />)
    expect(screen.getByText(`Policy digest: ${evidence.policy_digest}`)).toBeVisible()
    expect(screen.getByText('Confirmation verdict: insufficient')).toBeVisible()
  })
  it('shows execution failure as an explicit output error', () => {
    const store = createValueStore()
    store.upsertRun('failed-evaluation', { state: 'running' })
    store.apply('failed-evaluation', { seq: 0, kind: 'value', block_id: 'corpus',
      payload: { result: { data: null, error: { code: 'lab_corpus_failed' } } } })
    render(<RunsView store={store} mock />)
    expect(screen.getByText('Output error: {"code":"lab_corpus_failed"}')).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Corpus evaluation evidence' })).toBeNull()
  })
  it('says the worker status is unavailable instead of offline when the store read failed', () => {
    surface.current = { worker: { state: 'unavailable', reason: 'lab_store_unavailable' } }
    render(<RunsView store={createValueStore()} />)
    expect(screen.getByText('Lab worker status is unavailable right now.')).toBeVisible()
    expect(screen.queryByText('Worker offline')).toBeNull()
    expect(screen.queryByText('Worker paused')).toBeNull()
    expect(screen.queryByText('Worker status unavailable')).toBeNull()
    expect(screen.queryByText(/^Queue age/)).toBeNull()
  })
  it('offers Resume for an auth-expired run and retries its registered stream once', () => {
    const store = seed(), retry = vi.fn()
    store.setStreamControl('r', { retry, stop: vi.fn() })
    const view = render(<RunsView store={store} mock />)
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    act(() => store.setTransport('r', { status: 'auth_expired' }))
    expect(screen.getByText('Sign in again, then resume')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(retry).toHaveBeenCalledTimes(1)
    act(() => store.setTransport('r', { status: 'connected' }))
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    view.unmount()
  })
  it('renders worker offline, paused, transport lag and stale source as separate indicators', () => {
    surface.current = { worker: { paused: true, workers: [{ heartbeat_age_s: 61 }], queue_age_max_s: 17 } }
    const store = seed(); store.setTransport('r', { status: 'connected', lagMs: 12 })
    render(<RunsView store={store} />)
    expect(screen.getByText('Worker offline')).toBeVisible()
    expect(screen.getByText('Worker paused')).toBeVisible()
    expect(screen.getByText('Stale source')).toBeVisible()
    expect(screen.getByText('Queue age: 17s')).toBeVisible()
    expect(screen.getByText('Stream: connected · lag: 12 ms')).toBeVisible()
    expect(screen.getByText('Source age: 120s')).toBeVisible()
  })
  it('does not confuse an offline worker with fresh source data or healthy transport', () => {
    surface.current = { worker: { paused: false, workers: [], queue_age_max_s: 0 } }
    const store = seed()
    store.apply('r', { seq: 1, kind: 'value', block_id: 'count', payload: { result: { data: 4, as_of: Date.now() } } })
    render(<RunsView store={store} />)
    expect(screen.getByText('Worker offline')).toBeVisible()
    expect(screen.queryByText('Stale source')).toBeNull()
    expect(screen.queryByText('Worker paused')).toBeNull()
  })
  it('keeps a stale source visible when a worker is online at the heartbeat boundary', () => {
    surface.current = { worker: { paused: false, workers: [{ heartbeat_age_s: 60 }] } }
    render(<RunsView store={seed()} />)
    expect(screen.queryByText('Worker offline')).toBeNull()
    expect(screen.getByText('Stale source')).toBeVisible()
  })
  it('posts cancel only once while the request is pending or accepted', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ cancel_requested: true }) })
    render(<RunsView store={seed()} fetchImpl={fetchImpl} mock />)
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    fireEvent.click(cancel); fireEvent.click(cancel)
    await screen.findByText('Cancellation requested')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/runs/r/cancel`)
    expect(fetchImpl.mock.calls[0][1].method).toBe('POST')
    expect(cancel).toBeDisabled()
  })
  it('shows recovered gaps, bounded summaries and event/drop counts with newest runs first', () => {
    const store = createValueStore({ maxEvents: 1 })
    store.upsertRun('older', { state: 'finished', graph_revision: 1 })
    store.upsertRun('newer', { state: 'running', graph_revision: 2 })
    store.addGap('newer', { recovered: true })
    store.apply('newer', { seq: 0, kind: 'value', block_id: 'block', payload: { out: 'x'.repeat(500) } })
    store.apply('newer', { seq: 1, kind: 'value', block_id: 'block', payload: { out: 'x'.repeat(500) } })
    render(<RunsView store={store} mock />)
    expect(screen.getByText('Recovered from a snapshot after a gap')).toBeVisible()
    expect(screen.getAllByRole('article')[0]).toHaveAccessibleName('Run newer')
    const run = within(screen.getByRole('article', { name: 'Run newer' }))
    expect(run.getByText('Events: 1 · Dropped: 1')).toBeVisible()
    expect(run.getByText(/^out:/).textContent).toHaveLength(205)
  })
  it('reads worker details when the shared hook supplies only posture', async () => {
    surface.current = { state: 'on', posture: 'local' }
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ worker: { paused: true, workers: [] } }) })
    render(<RunsView store={createValueStore()} fetchImpl={fetchImpl} />)
    await screen.findByText('Worker paused')
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/status`)
  })
  it('updates an individual block from the shared store', async () => {
    const store = seed(); render(<RunsView store={store} mock />)
    act(() => store.apply('r', { seq: 1, kind: 'value', block_id: 'count', payload: { result: { data: 99, as_of: Date.now() } } }))
    await waitFor(() => expect(screen.getByText(/result:.*99/)).toBeVisible())
  })
})
