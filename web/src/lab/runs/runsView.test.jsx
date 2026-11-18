import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import RunsView from './RunsView.jsx'
import { createValueStore } from './valueStore.js'
import { config } from '../../api.js'
import { notifyActiveScopeChanged } from '../../auth/activeScope.js'
import { canonicalJSON } from '../contract.js'
import { addBlock, connectPipe, emptyGraph, updateSettings } from '../canvas/graphDoc.js'
import { registry } from '../canvas/registry.js'

const surface = vi.hoisted(() => ({ current: null }))
const authority = vi.hoisted(() => ({ headers: {}, account: 'account-one' }))
vi.mock('../useLabSurfaceStatus.js', () => ({ useLabSurfaceStatus: () => surface.current }))
vi.mock('../../api.js', async importOriginal => ({ ...await importOriginal(), authHeaders: () => authority.headers }))
vi.mock('../../auth/activeScope.js', async importOriginal => ({ ...await importOriginal(), readActiveScope: () => authority.account }))
const originalConfig = { apiBase: config.apiBase, tenant: config.tenant }
afterEach(() => {
  cleanup(); surface.current = null; authority.headers = {}; authority.account = 'account-one'
  Object.assign(config, originalConfig)
  vi.restoreAllMocks(); vi.useRealTimers()
})
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
const signPlan = plan => {
  const { digest: unused, ...unsigned } = plan
  return { ...unsigned, digest: createHash('sha256').update(canonicalJSON(unsigned)).digest('hex') }
}
const freezePlan = value => {
  if (value && typeof value === 'object') { Object.values(value).forEach(freezePlan); Object.freeze(value) }
  return value
}
const recoveryFixture = (nodes, runId = 'source/run ?', revision = 7) => {
  const blocks = nodes.map(([block_id, disposition, reason_category, status = disposition === 'retained' ? 'complete' : 'failed'], index) => ({
    block_id, disposition, reason_category, idempotency_key: `lab:${runId}:${block_id}:corpus:v1`,
    job_id: status === null ? null : `authoritative-job-${index}`, status,
    receipt_sha256: status === null ? null : 'b'.repeat(64),
  }))
  const selected = blocks.filter(block => block.disposition === 'selected').map(block => block.block_id)
  const unresolved = blocks.filter(block => block.disposition === 'unresolved').map(block => block.block_id)
  return signPlan({ schema: 'leaf.lab-corpus-recovery-plan.v1',
    source: { run_id: runId, doc_id: 'original-document', graph_revision: revision, graph_sha256: 'a'.repeat(64), state: 'finished' },
    blocks, selected_failed_block_ids: selected,
    retained_completed_block_ids: blocks.filter(block => block.disposition === 'retained').map(block => block.block_id),
    unresolved_block_ids: unresolved,
    draft_graph: selected.length && !unresolved.length ? { schema: 'leaf.lab-graph.v1',
      blocks: selected.map(id => ({ id, registry: { id: 'corpus-evaluator', version: '1.0.0', code_sha256: 'c'.repeat(64) },
        settings: { project_id: 'synthetic-project' } })), pipes: [], outputs: {} } : null,
    execution_allowed: false, confirmation_required: true })
}
const smallRecoveryPlan = freezePlan(recoveryFixture([
  ['failed-timeout', 'selected', 'timeout'], ['completed-pass', 'retained', 'completed'],
  ['completed-verdict-fail', 'retained', 'completed'],
]))
const largeRecoveryPlan = freezePlan(recoveryFixture([
  ['timeout-node', 'selected', 'timeout'], ['resource-node', 'selected', 'resource_exhausted'],
  ['completed-pass', 'retained', 'completed'], ['completed-verdict-fail', 'retained', 'completed'],
  ['invalid-node', 'unresolved', 'invalid_input'], ['missing-node', 'unresolved', 'missing_job', null],
  ['running-node', 'unresolved', 'job_nonterminal', 'running'], ['cleanup-node', 'unresolved', 'cleanup_unconfirmed'],
  ['access-node', 'unresolved', 'access_revoked'], ['runtime-node', 'unresolved', 'runtime_unavailable'],
  ['verification-node', 'unresolved', 'verification_required'], ['<img src=x onerror=alert(1)>', 'unresolved', 'unknown_failure'],
]))
const recoveryStore = plan => {
  const store = createValueStore()
  store.upsertRun(plan.source.run_id, { state: plan.source.state, graph_revision: plan.source.graph_revision })
  return store
}
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const recoveryResponse = (plan, { text = JSON.stringify(plan), status = 200, media = 'application/json', gate } = {}) => {
  const reader = { read: vi.fn().mockImplementationOnce(async () => {
    if (gate) await gate.promise
    return { done: false, value: new TextEncoder().encode(text) }
  }).mockResolvedValue({ done: true }), cancel: vi.fn().mockResolvedValue(), releaseLock: vi.fn() }
  return { ok: status === 200, status, headers: { get: name => name === 'content-type' ? media : null },
    body: { getReader: () => reader }, reader }
}
const reviewRecovery = () => fireEvent.click(screen.getByRole('button', { name: 'Review recovery options' }))
const expectRecoveryGets = fetchImpl => {
  expect(fetchImpl.mock.calls.length).toBeGreaterThan(0)
  for (const [url, options] of fetchImpl.mock.calls) {
    expect(url).toMatch(/\/recovery-plan$/)
    expect(options.method).toBe('GET')
    expect(options.body).toBeUndefined()
  }
}

describe('Runs recovery options preview', () => {
  it('reviews a frozen three-node plan only on explicit clicks and retains completed verdict fail', async () => {
    authority.headers = { Authorization: 'one' }
    const fetchImpl = vi.fn().mockImplementation(async () => recoveryResponse(smallRecoveryPlan))
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    reviewRecovery(); reviewRecovery()
    const preview = within(await screen.findByRole('region', { name: 'Recovery options preview' }))
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, options] = fetchImpl.mock.calls[0]
    expect(url).toBe(`${config.apiBase}/api/lab/runs/${encodeURIComponent(smallRecoveryPlan.source.run_id)}/recovery-plan`)
    expect(options).toMatchObject({ method: 'GET', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { Authorization: 'one', 'X-Tenant-Id': config.tenant, accept: 'application/json' } })
    expect(preview.getByText('Source document: original-document · revision 7')).toBeVisible()
    expect(preview.getByText(`Graph digest: ${smallRecoveryPlan.source.graph_sha256}`)).toBeVisible()
    expect(preview.getByText(`Plan digest: ${smallRecoveryPlan.digest}`)).toBeVisible()
    for (const [label, ids] of [['Selected failed nodes', smallRecoveryPlan.selected_failed_block_ids],
      ['Retained completed nodes', smallRecoveryPlan.retained_completed_block_ids]]) {
      const group = within(preview.getByRole('region', { name: label }))
      expect(group.getAllByRole('listitem').map(row => row.querySelector('strong').textContent)).toEqual(ids)
    }
    for (const block of smallRecoveryPlan.blocks) expect(preview.getByText(`Job: ${block.job_id} · status: ${block.status}`)).toBeVisible()
    expect(preview.getByText(/including verdict fail/)).toBeVisible()
    expect(preview.getByText('Preparation available for the selected failed nodes.')).toBeVisible()
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(preview.queryByText(/synthetic-project|leaf.lab-graph/)).toBeNull()
    reviewRecovery()
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    await screen.findByRole('region', { name: 'Recovery options preview' })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expectRecoveryGets(fetchImpl)
  })
  it('keeps twelve nodes readable with unresolved explanations and hostile identities as inert text', async () => {
    const mutable = JSON.parse(JSON.stringify(largeRecoveryPlan))
    mutable.source.doc_id = '<svg onload=alert(2)>'
    mutable.blocks[11].job_id = '<script>alert(3)</script>'
    const plan = freezePlan(signPlan(mutable))
    const fetchImpl = vi.fn().mockResolvedValue(recoveryResponse(plan))
    const view = render(<RunsView store={recoveryStore(plan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    const preview = within(await screen.findByRole('region', { name: 'Recovery options preview' }))
    expect(preview.getByText('Preparation blocked: unresolved nodes need review.')).toBeVisible()
    expect(preview.getAllByRole('listitem')).toHaveLength(12)
    for (const [label, disposition] of [['Selected failed nodes', 'selected'], ['Retained completed nodes', 'retained'], ['Unresolved nodes', 'unresolved']]) {
      const group = within(preview.getByRole('region', { name: label }))
      expect(group.getByRole('heading', { name: label })).toBeVisible()
      expect(group.getAllByRole('listitem').map(row => row.querySelector('strong').textContent))
        .toEqual(plan.blocks.filter(block => block.disposition === disposition).map(block => block.block_id))
    }
    for (const reason of ['Input needs correction (invalid_input)', 'Authoritative job missing (missing_job)',
      'Job is still pending or running (job_nonterminal)', 'Failure cause is unverified (unknown_failure)']) {
      expect(preview.getByText(reason)).toBeVisible()
    }
    expect(preview.getByText('Job: Unavailable · status: Unavailable')).toBeVisible()
    expect(preview.getByText('<img src=x onerror=alert(1)>')).toBeVisible()
    expect(preview.getByText('Job: <script>alert(3)</script> · status: failed')).toBeVisible()
    expect(preview.getByText('Source document: <svg onload=alert(2)> · revision 7')).toBeVisible()
    expect(view.container.querySelector('img, script, svg')).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expectRecoveryGets(fetchImpl)
  })
  it('does not offer recovery on running, missing, or local runs', () => {
    const store = seed()
    store.upsertRun('absent', { state: 'missing' })
    store.upsertRun('local', { state: 'finished' })
    const fetchImpl = vi.fn()
    render(<RunsView store={store} fetchImpl={fetchImpl} mock />)
    expect(screen.queryByRole('button', { name: 'Review recovery options' })).toBeNull()
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it.each([
    ['wrong schema', plan => { plan.schema = 'unexpected' }],
    ['executable response', plan => { plan.execution_allowed = true }],
    ['missing confirmation', plan => { plan.confirmation_required = false }],
    ['unexpected execution action', plan => { plan.execute_url = '/api/evaluate' }],
    ['wrong run', plan => { plan.source.run_id = 'another-run' }],
    ['wrong revision', plan => { plan.source.graph_revision = 8 }],
    ['nonterminal source', plan => { plan.source.state = 'running' }],
    ['invalid source digest', plan => { plan.source.graph_sha256 = 'not-a-digest' }],
    ['duplicate selected node', plan => { plan.selected_failed_block_ids.push('failed-timeout') }],
    ['overlapping membership', plan => { plan.unresolved_block_ids.push('completed-pass') }],
    ['missing membership', plan => { plan.retained_completed_block_ids.pop() }],
    ['missing block', plan => { plan.blocks.pop() }],
    ['duplicate block', plan => { plan.blocks.push(plan.blocks[0]) }],
    ['wrong disposition', plan => { plan.blocks[0].disposition = 'retained' }],
    ['wrong job intent', plan => { plan.blocks[0].idempotency_key = 'another-intent' }],
    ['retained failed execution', plan => { plan.blocks[1].status = 'failed' }],
    ['unknown reason', plan => { plan.blocks[0].reason_category = '<script>untrusted</script>' }],
    ['unbounded blocks', plan => { plan.blocks = Array(201).fill(plan.blocks[0]) }],
    ['unbounded membership', plan => { plan.unresolved_block_ids = Array(201).fill('missing') }],
    ['wrong draft selection', plan => { plan.draft_graph.blocks[0].id = 'completed-pass' }],
  ])('refuses a signed but invalid plan: %s', async (unused, change) => {
    const plan = JSON.parse(JSON.stringify(smallRecoveryPlan)); change(plan)
    const fetchImpl = vi.fn().mockResolvedValue(recoveryResponse(signPlan(plan)))
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await screen.findByText('Recovery options unavailable. The plan could not be verified.')
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expectRecoveryGets(fetchImpl)
  })
  it.each(['doc_id', 'graph_sha256'])('checks the stored source %s when available', async field => {
    const store = recoveryStore(smallRecoveryPlan)
    store.upsertRun(smallRecoveryPlan.source.run_id, { [field]: field === 'doc_id' ? 'different-document' : 'c'.repeat(64) })
    const fetchImpl = vi.fn().mockResolvedValue(recoveryResponse(smallRecoveryPlan))
    render(<RunsView store={store} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await screen.findByText('Recovery options unavailable. The plan could not be verified.')
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expectRecoveryGets(fetchImpl)
  })
  it.each(['digest', 'malformed', 'oversized', 'media', 'redirect'])('refuses %s responses safely without retries', async kind => {
    const tampered = { ...smallRecoveryPlan, source: { ...smallRecoveryPlan.source, doc_id: 'tampered-document' } }
    const response = recoveryResponse(kind === 'digest' ? tampered : smallRecoveryPlan, {
      ...(kind === 'malformed' ? { text: 'private server exception: invalid JSON' } : {}),
      ...(kind === 'oversized' ? { text: ' '.repeat(524289) } : {}),
      ...(kind === 'media' ? { media: 'text/html' } : {}),
    })
    if (kind === 'redirect') response.redirected = true
    const fetchImpl = vi.fn().mockResolvedValue(response)
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await screen.findByText('Recovery options unavailable. The plan could not be verified.')
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(screen.queryByText(/private server exception|tampered-document/)).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expectRecoveryGets(fetchImpl)
  })
  it.each([401, 403])('shows safe feedback for %s without exposing the server error body', async status => {
    const response = recoveryResponse(null, { status, text: 'private exception /secret/path credential=never-display' })
    const fetchImpl = vi.fn().mockResolvedValue(response)
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await screen.findByText(status === 401 ? 'Sign in again to review recovery options.' : 'You no longer have access to these recovery options.')
    expect(response.reader.read).not.toHaveBeenCalled()
    expect(screen.queryByText(/private exception|credential=never-display/)).toBeNull()
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expectRecoveryGets(fetchImpl)
  })
  it.each(['account', 'auth', 'api', 'tenant'])('rechecks %s after body completion even without a scope event', async kind => {
    const gate = deferred(), response = recoveryResponse(smallRecoveryPlan, { gate })
    const fetchImpl = vi.fn().mockResolvedValue(response)
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await waitFor(() => expect(response.reader.read).toHaveBeenCalledTimes(1))
    if (kind === 'account') authority.account = 'account-two'
    if (kind === 'auth') authority.headers = { Authorization: 'two' }
    if (kind === 'api') config.apiBase = '/another-api'
    if (kind === 'tenant') config.tenant = 'another-tenant'
    await act(async () => { gate.resolve() })
    await screen.findByText('Run or account context changed. Review recovery options again.')
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expectRecoveryGets(fetchImpl)
  })
  it('clears verified evidence when the account scope changes', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(recoveryResponse(smallRecoveryPlan))
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await screen.findByRole('region', { name: 'Recovery options preview' })
    act(() => { authority.account = 'account-two'; notifyActiveScopeChanged() })
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(screen.getByText('Run or account context changed. Review recovery options again.')).toBeVisible()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('keeps the new account response when an old account body completes later', async () => {
    const gate = deferred(), oldResponse = recoveryResponse(smallRecoveryPlan, { gate })
    const mutable = JSON.parse(JSON.stringify(smallRecoveryPlan))
    mutable.blocks[0].job_id = 'current-authoritative-job'
    const currentPlan = freezePlan(signPlan(mutable))
    const fetchImpl = vi.fn().mockResolvedValueOnce(oldResponse).mockResolvedValueOnce(recoveryResponse(currentPlan))
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await waitFor(() => expect(oldResponse.reader.read).toHaveBeenCalledTimes(1))
    act(() => { authority.account = 'account-two'; notifyActiveScopeChanged() })
    expect(oldResponse.reader.cancel).toHaveBeenCalled()
    reviewRecovery()
    await screen.findByText(`Plan digest: ${currentPlan.digest}`)
    await act(async () => { gate.resolve() })
    expect(screen.getByText('Job: current-authoritative-job · status: failed')).toBeVisible()
    expect(screen.queryByText(`Plan digest: ${smallRecoveryPlan.digest}`)).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expectRecoveryGets(fetchImpl)
  })
  it('invalidates a pending body even if account changes are batched back to the original account', async () => {
    const gate = deferred(), response = recoveryResponse(smallRecoveryPlan, { gate })
    const fetchImpl = vi.fn().mockResolvedValue(response)
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await waitFor(() => expect(response.reader.read).toHaveBeenCalledTimes(1))
    act(() => {
      authority.account = 'account-two'; notifyActiveScopeChanged()
      authority.account = 'account-one'; notifyActiveScopeChanged()
    })
    expect(response.reader.cancel).toHaveBeenCalled()
    await act(async () => { gate.resolve() })
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(screen.getByText('Run or account context changed. Review recovery options again.')).toBeVisible()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it.each(['run', 'revision', 'store'])('keeps only the current plan when bodies finish in reverse order after a %s change', async kind => {
    const gate = deferred(), oldResponse = recoveryResponse(smallRecoveryPlan, { gate })
    const currentPlan = freezePlan(recoveryFixture([['current-node', 'selected', 'timeout']], kind === 'run' ? 'current-run' : smallRecoveryPlan.source.run_id, kind === 'revision' ? 8 : 7))
    const fetchImpl = vi.fn().mockResolvedValueOnce(oldResponse).mockResolvedValueOnce(recoveryResponse(currentPlan))
    const store = recoveryStore(smallRecoveryPlan)
    const view = render(<RunsView store={store} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await waitFor(() => expect(oldResponse.reader.read).toHaveBeenCalledTimes(1))
    if (kind === 'revision') act(() => store.upsertRun(currentPlan.source.run_id, { graph_revision: 8 }))
    else view.rerender(<RunsView store={recoveryStore(currentPlan)} fetchImpl={fetchImpl} mock />)
    expect(oldResponse.reader.cancel).toHaveBeenCalled()
    reviewRecovery()
    await screen.findByRole('region', { name: 'Recovery options preview' })
    expect(screen.getByText('current-node')).toBeVisible()
    await act(async () => { gate.resolve() })
    expect(screen.getByText('current-node')).toBeVisible()
    expect(screen.queryByText('failed-timeout')).toBeNull()
    expect(screen.getByText(`Plan digest: ${currentPlan.digest}`)).toBeVisible()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expectRecoveryGets(fetchImpl)
  })
  it('cancels body reading on unmount and ignores its late completion', async () => {
    const gate = deferred(), response = recoveryResponse(smallRecoveryPlan, { gate })
    const fetchImpl = vi.fn().mockResolvedValue(response)
    const view = render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    await waitFor(() => expect(response.reader.read).toHaveBeenCalledTimes(1))
    view.unmount()
    expect(response.reader.cancel).toHaveBeenCalled()
    await act(async () => { gate.resolve() })
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('aborts a pending request on unmount', async () => {
    const gate = deferred(), fetchImpl = vi.fn().mockReturnValue(gate.promise)
    const view = render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    reviewRecovery()
    const signal = fetchImpl.mock.calls[0][1].signal
    view.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { gate.resolve(recoveryResponse(smallRecoveryPlan)) })
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
  })
  it('bounds a stalled body lifetime and never automatically retries', async () => {
    vi.useFakeTimers()
    const gate = deferred(), response = recoveryResponse(smallRecoveryPlan, { gate })
    const fetchImpl = vi.fn().mockResolvedValue(response)
    render(<RunsView store={recoveryStore(smallRecoveryPlan)} fetchImpl={fetchImpl} mock />)
    await act(async () => { reviewRecovery() })
    expect(response.reader.read).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(10001) })
    expect(screen.getByText('Recovery options unavailable. The plan could not be verified.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Review recovery options' })).toBeEnabled()
    expect(response.reader.cancel).toHaveBeenCalled()
    await act(async () => { gate.resolve(); await vi.advanceTimersByTimeAsync(20000) })
    expect(screen.queryByRole('region', { name: 'Recovery options preview' })).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expectRecoveryGets(fetchImpl)
  })
})

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
