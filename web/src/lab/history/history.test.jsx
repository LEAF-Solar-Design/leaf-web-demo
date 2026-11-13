import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import { config } from '../../api.js'
import { humanizeError } from '../../errorHumanize.js'
import HistoryView from './HistoryView.jsx'
import LabSaveBar from './LabSaveBar.jsx'
import LabDiffView from './LabDiffView.jsx'
import { createLabArtifactsClient, graphLayouts, SAVED_REVISION_EVENT, verifyLabBundle } from './labArtifactsClient.js'
import { canonicalJSON } from '../contract.js'

const graph = setting => ({ schema: 'leaf.lab-graph.v1', blocks: [
  { id: 'field-1', registry: { id: 'field', version: '1.0.0' }, settings: { field: setting } },
], pipes: [], outputs: {} })
const first = graph('price')
const second = { ...graph('size'), pipes: [{ from: { node: 'source', port: 'rows' }, to: { node: 'field-1', port: 'value' } }] }
const changes = [
  { op: 'replace', path: '/blocks/0/settings/field', before: 'price', after: 'size' },
  { op: 'replace', path: '/pipes', before: [], after: second.pipes },
]
const copy = value => JSON.parse(JSON.stringify(value))
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => copy(data) })
const hash = value => createHash('sha256').update(canonicalJSON(value)).digest('hex')
function exportFixture() {
  const identity = { tenant_id: 'test-tenant', doc_id: 'strategy', revision: 1, parent_revision: null,
    kind: 'graph', author: 'tester', created_at: 1800000001, sha256: hash(first) }
  const report = { candidates: [{ setting: 'price', score: 2 }, { setting: 'size', score: 3 }],
    confirm_report: { parent_policy_digest: hash({ field: 'price' }), child_policy_digest: hash({ field: 'size' }),
      calibration: { parent: { n: 20 }, child: { n: 20 } } }, reasons: ['complete report'] }
  const run = { tenant_id: identity.tenant_id, doc_id: identity.doc_id, run_id: 'evaluation-1',
    graph_revision: 1, graph_sha256: identity.sha256, state: 'finished' }
  const parts = { identity, graph: copy(first),
    layout: { schema: 'leaf.lab-layout.v1', positions: { 'field-1': { x: 12, y: -34 } } },
    policy_reference: { graph_sha256: identity.sha256,
      blocks: first.blocks.map(block => ({ id: block.id, registry: block.registry, settings_sha256: hash(block.settings) })),
      compiled: [{ run_id: run.run_id, block_id: 'field-1', ...report.confirm_report }] },
    evaluations: [{ run, events: Array.from({ length: 1001 }, (_, seq) => ({ tenant_id: identity.tenant_id,
      run_id: run.run_id, seq, kind: seq === 1000 ? 'finished' : 'value', payload: { result: { data: seq } } })),
    complete_results: { 'job-1': { result: report } } }] }
  const bundle = { schema: 'leaf.lab-bundle.v1', parts,
    manifest: { algorithm: 'sha256', parts: Object.fromEntries(Object.entries(parts).map(([key, value]) => [key, hash(value)])) } }
  const fetchImpl = vi.fn(async url => {
    if (url.endsWith('/revisions/1/export')) return response(bundle)
    if (url.endsWith('/head')) return response({ ...identity, revision: 2, published_revision: 1 })
    if (url.endsWith('/revisions')) return response({ revisions: [identity, { ...identity, revision: 2 }] })
    throw new Error(`Unexpected request ${url}`)
  })
  return { bundle, fetchImpl, client: createLabArtifactsClient({ fetchImpl }) }
}
const blobText = blob => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(reader.result)
  reader.onerror = reject
  reader.readAsText(blob)
})
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function ledger() {
  const rows = []
  const response = (status, data) => ({ ok: status < 400, status, json: async () => copy(data) })
  const append = document => {
    const revision = rows.length + 1
    const row = { revision, parent_revision: revision === 1 ? null : revision - 1,
      author: 'tester', created_at: 1800000000 + revision,
      sha256: (JSON.stringify(document) === JSON.stringify(first) ? '1' : '2').repeat(64), graph: copy(document) }
    rows.push(row)
    return row
  }
  const fetchImpl = vi.fn(async (url, init) => {
    const path = new URL(url, 'https://example.test').pathname.split('/').slice(5).join('/')
    const body = init.body ? JSON.parse(init.body) : null
    if (init.method === 'POST') {
      if (body.expected_head !== (rows.at(-1)?.revision ?? null)) return response(409, { detail: 'stale_expected_head' })
      if (path === 'save') return response(200, append(body.graph))
      if (path === 'restore') return response(200, append(rows[body.revision - 1].graph))
      if (path === 'publish') return response(200, { receipt_id: 'receipt-3', new_head: rows.length, sha256: rows.at(-1).sha256 })
    }
    if (path === 'head') return rows.length ? response(200, rows.at(-1)) : response(404, { detail: 'lab_document_not_found' })
    // The real list route supplies revision metadata, not graphs.
    if (path === 'revisions') return response(200, { revisions: rows.map(({ graph: unused, ...row }) => row) })
    if (path.startsWith('revisions/')) return response(200, rows[Number(path.split('/')[1]) - 1])
    if (path === 'diff') return response(200, { before: 1, after: 2, changes })
    throw new Error(`Unexpected request ${url}`)
  })
  return { rows, append, fetchImpl, client: createLabArtifactsClient({ fetchImpl, now: () => 1800000000000 }) }
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('Strategy revision ledger', () => {
  it('downloads the selected editable revision, full evaluation and digest manifest as JSON', async () => {
    const { bundle, client, fetchImpl } = exportFixture()
    let downloaded, filename
    const createObjectURL = vi.fn(blob => { downloaded = blob; return 'blob:lab-export' })
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { filename = this.download })
    render(<HistoryView client={client} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Export', exact: true })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Export revision'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Export', exact: true }))
    await screen.findByText('Exported r1.')
    const exported = JSON.parse(await blobText(downloaded))
    expect(exported).toEqual(bundle)
    expect(downloaded.type).toBe('application/json')
    expect(filename).toBe('lab-strategy-r1.json')
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:lab-export'), { timeout: 2000 })
    for (const [part, value] of Object.entries(exported.parts)) expect(exported.manifest.parts[part]).toBe(hash(value))
    expect(exported.parts.evaluations[0].events).toHaveLength(1001)
    expect(exported.parts.evaluations[0].complete_results['job-1'].result.candidates).toHaveLength(2)
    expect(fetchImpl.mock.calls.find(([url]) => url.endsWith('/revisions/1/export'))[1]).toMatchObject({
      method: 'GET', credentials: 'same-origin', redirect: 'error', headers: { 'X-Tenant-Id': config.tenant },
    })
    expect(client.state.events).toEqual([])
  })

  it('reopens a downloaded bundle on a fresh client with identities, layout and evaluations intact', async () => {
    const { bundle, client: exporter } = exportFixture()
    const exported = await exporter.exportBundle('strategy', 1)
    const { client, fetchImpl } = exportFixture()
    render(<HistoryView client={client} />)
    await waitFor(() => expect(screen.getByLabelText('Reopen bundle')).toBeEnabled())
    fetchImpl.mockClear()
    const input = screen.getByLabelText('Reopen bundle')
    fireEvent.change(input, { target: { files: [new File([JSON.stringify(exported.bundle)], 'saved.json', { type: 'application/json' })] } })
    await waitFor(() => expect(client.state.restoredGraph).toEqual(first))
    expect(client.currentDocId()).toBe(bundle.parts.identity.doc_id)
    expect(client.state.openedBundle).toEqual(bundle)
    expect(client.state.lastSaved).toMatchObject({ revision: 1, sha256: hash(first), graph: first })
    expect(client.state.head).toBe(2)
    expect(client.state.published).toBe(1)
    expect(await graphLayouts.get(client.state.restoredGraph).load()).toEqual(bundle.parts.layout)
    expect(fetchImpl.mock.calls.map(([url]) => url.split('/').at(-1))).toEqual(['export', 'head'])
    expect(fetchImpl.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true)
    const edited = copy(client.state.restoredGraph)
    edited.blocks[0].settings.field = 'edited after reopen'
    act(() => client.takeGraph(edited))
    expect(client.state.restoredGraph.blocks[0].settings.field).toBe('edited after reopen')
    expect(client.state.openedBundle.parts.graph).toEqual(first)
  })

  it.each(['identity', 'graph', 'layout', 'policy_reference', 'evaluations'])('visibly refuses a tampered %s part before requests', async part => {
    const { bundle, client, fetchImpl } = exportFixture()
    bundle.parts[part] = part === 'evaluations' ? [] : { ...bundle.parts[part], tampered: true }
    render(<HistoryView client={client} />)
    await waitFor(() => expect(screen.getByLabelText('Reopen bundle')).toBeEnabled())
    fetchImpl.mockClear()
    fireEvent.change(screen.getByLabelText('Reopen bundle'), { target: { files: [new File([JSON.stringify(bundle)], 'tampered.json')] } })
    expect(await screen.findByText('Reopen failed: Bundle integrity check failed. No graph was opened.')).toBeVisible()
    expect(client.state.restoredGraph).toBeNull()
    expect(client.state.openedBundle).toBeNull()
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('rejects a missing digest, a mismatched graph identity and malformed JSON', async () => {
    const { bundle, client, fetchImpl } = exportFixture()
    const missing = copy(bundle)
    delete missing.manifest.parts.layout
    expect(() => verifyLabBundle(missing)).toThrow('integrity check failed')
    const mismatched = copy(bundle)
    mismatched.parts.identity.sha256 = hash({ other: true })
    mismatched.manifest.parts.identity = hash(mismatched.parts.identity)
    expect(() => verifyLabBundle(mismatched)).toThrow('integrity check failed')
    expect(await client.reopenBundle('{')).toMatchObject({ ok: false, detail: expect.stringContaining('integrity check failed') })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('shows revoked export authorization without downloading or changing the graph', async () => {
    const { client, fetchImpl } = exportFixture()
    const download = vi.fn()
    vi.stubGlobal('URL', class extends URL { static createObjectURL = download })
    render(<HistoryView client={client} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Export', exact: true })).toBeEnabled())
    fetchImpl.mockResolvedValueOnce(response({ detail: 'lab_export_forbidden' }, 403))
    fireEvent.click(screen.getByRole('button', { name: 'Export', exact: true }))
    expect(await screen.findByText('Export failed: You no longer have access to this revision.')).toBeVisible()
    expect(download).not.toHaveBeenCalled()
    expect(client.state.restoredGraph).toBeNull()
  })

  it('reauthorizes reopen and rejects a different tenant identity even with valid digests', async () => {
    const { bundle, client, fetchImpl } = exportFixture()
    fetchImpl.mockResolvedValueOnce(response({ detail: 'lab_export_forbidden' }, 403))
    expect(await client.reopenBundle(JSON.stringify(bundle))).toMatchObject({ ok: false, status: 403 })
    expect(client.state.restoredGraph).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const foreign = copy(bundle)
    foreign.parts.identity.tenant_id = 'another-tenant'
    foreign.parts.evaluations = []
    foreign.manifest.parts.identity = hash(foreign.parts.identity)
    foreign.manifest.parts.evaluations = hash(foreign.parts.evaluations)
    expect(await client.reopenBundle(JSON.stringify(foreign))).toMatchObject({ ok: false, detail: expect.stringContaining('integrity check failed') })
    expect(client.state.restoredGraph).toBeNull()
  })

  it('drops pending export and reopen when the document or graph operation changes', async () => {
    const { bundle, client, fetchImpl } = exportFixture()
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    const exporting = client.exportBundle('strategy', 1)
    client.setDocId('other')
    pending.resolve(response(bundle))
    expect(await exporting).toMatchObject({ ok: false })
    const reopening = deferred()
    fetchImpl.mockImplementationOnce(() => reopening.promise)
    const opening = client.reopenBundle(JSON.stringify(bundle))
    client.takeGraph(second)
    reopening.resolve(response(bundle))
    expect(await opening).toMatchObject({ ok: false })
    expect(client.state.restoredGraph).toEqual(second)
    expect(client.state.openedBundle).toBeNull()
  })

  it('exports a revision without evaluations or layout and drops a response after an account change', async () => {
    const { bundle, client, fetchImpl } = exportFixture()
    bundle.parts.evaluations = []
    bundle.parts.layout = null
    bundle.parts.policy_reference.compiled = []
    for (const key of ['evaluations', 'layout', 'policy_reference']) bundle.manifest.parts[key] = hash(bundle.parts[key])
    expect(await client.exportBundle('strategy', 1)).toEqual({ ok: true, bundle })
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    const exporting = client.exportBundle('strategy', 1)
    const tenant = config.tenant
    try {
      config.tenant = 'another-tenant'
      pending.resolve(response(bundle))
      expect(await exporting).toMatchObject({ ok: false })
    } finally { config.tenant = tenant }
    expect(client.state.openedBundle).toBeNull()
  })

  it('gets and puts layouts at the digest path using the shared tenant request helper', async () => {
    const sha256 = 'a'.repeat(64)
    const layout = { schema: 'leaf.lab-layout.v1', positions: { 'field-1': { x: 12, y: -34 } } }
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response({ layout }))
      .mockResolvedValueOnce({ ok: true, status: 204, json: async () => { throw new SyntaxError('Empty body') } })
    const client = createLabArtifactsClient({ fetchImpl })
    expect(await client.getLayout('strategy', sha256)).toEqual(layout)
    expect(await client.putLayout('strategy', sha256, layout)).toBe(true)
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      `${config.apiBase}/api/lab/docs/strategy/layout/${sha256}`,
      `${config.apiBase}/api/lab/docs/strategy/layout/${sha256}`,
    ])
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: 'GET', credentials: 'same-origin', redirect: 'error',
      headers: { 'X-Tenant-Id': config.tenant } })
    expect(fetchImpl.mock.calls[0][1]).not.toHaveProperty('body')
    expect(fetchImpl.mock.calls[1][1]).toMatchObject({ method: 'PUT', body: JSON.stringify(layout),
      headers: { 'X-Tenant-Id': config.tenant, 'content-type': 'application/json' } })
  })

  it('returns null and false for failed layout requests without throwing', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Offline'))
    const client = createLabArtifactsClient({ fetchImpl })
    await expect(client.getLayout('strategy', 'a'.repeat(64))).resolves.toBeNull()
    await expect(client.putLayout('strategy', 'a'.repeat(64), {})).resolves.toBe(false)
    fetchImpl.mockResolvedValue(response({ code: 'lab_layout_unknown_revision' }, 400))
    expect(await client.getLayout('strategy', 'a'.repeat(64))).toBeNull()
    expect(await client.putLayout('strategy', 'a'.repeat(64), {})).toBe(false)
    fetchImpl.mockResolvedValue(response({ layout: null }))
    expect(await client.getLayout('strategy', 'a'.repeat(64))).toBeNull()
    fetchImpl.mockClear()
    expect(await client.getLayout('../other', 'a'.repeat(64))).toBeNull()
    expect(await client.putLayout('../other', 'a'.repeat(64), {})).toBe(false)
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('P1-a captures the live edit after Save started before taking theirs', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    await client.head('strategy')
    append(graph('server'))
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    const take = vi.fn()
    const view = render(<LabSaveBar document={second} ready client={client} onTakeTheirs={take} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    const edited = graph('edited during save')
    view.rerender(<LabSaveBar document={edited} ready client={client} onTakeTheirs={take} />)
    await act(async () => pending.resolve(response({ detail: 'stale' }, 409)))
    await screen.findByText('Someone saved r2 while you edited.')
    expect(screen.getByRole('button', { name: 'Restore my edits' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    await screen.findByText('Loaded r2. Your edits are kept.')
    fireEvent.click(screen.getByRole('button', { name: 'Restore my edits' }))
    expect(take.mock.calls.at(-1)[0]).toEqual(edited)
  })

  it('P1-b orders late writes and drops first-generation A responses after A to B to A', async () => {
    const { client, fetchImpl } = ledger()
    const late = deferred()
    fetchImpl.mockImplementationOnce(() => late.promise)
    const save = client.save('strategy', first, null)
    fetchImpl.mockResolvedValueOnce(response({ revision: 4, graph: second }))
    await client.restore('strategy', 1, 3)
    late.resolve(response({ revision: 2, graph: first }))
    await save
    expect(client.state.head).toBe(4)
    expect(client.state.restoredGraph).toEqual(second)
    const old = deferred(), oldRestore = deferred(), oldPublish = deferred(), oldSave = deferred()
    fetchImpl.mockImplementationOnce(() => old.promise)
    const head = client.head('strategy')
    fetchImpl.mockImplementationOnce(() => oldRestore.promise)
    const restore = client.restore('strategy', 1, 4)
    fetchImpl.mockImplementationOnce(() => oldPublish.promise)
    const publish = client.publish('strategy', 4)
    fetchImpl.mockImplementationOnce(() => oldSave.promise)
    const firstGenerationSave = client.save('strategy', first, 4)
    const dispatch = vi.spyOn(globalThis, 'dispatchEvent')
    client.setDocId('B'); client.setDocId('strategy')
    const latest = deferred()
    fetchImpl.mockImplementationOnce(() => latest.promise)
    const read = client.head('strategy')
    old.resolve(response({ revision: 9 }))
    oldRestore.resolve(response({ revision: 10, graph: first }))
    oldPublish.resolve(response({ new_head: 10 }))
    oldSave.resolve(response({ revision: 11 }))
    await Promise.all([head, restore, publish, firstGenerationSave])
    expect(client.state.head).toBeNull()
    expect(client.state.restoredGraph).toBeNull()
    expect(client.state.published).toBeNull()
    expect(dispatch.mock.calls.some(([event]) => event.type === SAVED_REVISION_EVENT && event.detail.doc_id === 'strategy' && event.detail.revision === 11)).toBe(true)
    latest.resolve(response({ revision: 5 }))
    await read
    expect(client.state.head).toBe(5)
  })

  it('P1-c drops Take theirs when the document or graph operation changes', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    append(second)
    await client.save('strategy', first, 1)
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    const take = vi.fn()
    render(<LabSaveBar document={first} ready client={client} onTakeTheirs={take} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    act(() => client.setDocId('B'))
    await act(async () => pending.resolve(response({ revision: 2, graph: second })))
    expect(take).not.toHaveBeenCalled()
    expect(screen.queryByText(/Loaded r2/)).toBeNull()
    act(() => client.setDocId('strategy'))
    await act(async () => { await client.save('strategy', first, 1) })
    const replaced = deferred()
    fetchImpl.mockImplementationOnce(() => replaced.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    await act(async () => { await client.restore('strategy', 1, 2) })
    await act(async () => replaced.resolve(response({ revision: 2, graph: second })))
    expect(take).not.toHaveBeenCalled()
    expect(client.state.restoredGraph).toEqual(first)
  })

  it('P2-a drops older head and revisions reads and pre-conflict head responses', async () => {
    const { client, fetchImpl, append } = ledger()
    const older = deferred(), newer = deferred()
    fetchImpl.mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise)
    const firstRead = client.head('strategy'), secondRead = client.head('strategy')
    newer.resolve(response({ revision: 3 })); await secondRead
    older.resolve(response({ revision: 2 })); await firstRead
    expect(client.state.head).toBe(3)
    const oldList = deferred(), newList = deferred()
    fetchImpl.mockImplementationOnce(() => oldList.promise).mockImplementationOnce(() => newList.promise)
    const list1 = client.revisions('strategy'), list2 = client.revisions('strategy')
    newList.resolve(response({ revisions: [{ revision: 3, author: 'new' }] })); await list2
    oldList.resolve(response({ revisions: [{ revision: 3, author: 'old' }] })); await list1
    expect(client.state.revisions[0].author).toBe('new')
    const preConflict = deferred()
    fetchImpl.mockImplementationOnce(() => preConflict.promise)
    const read = client.head('strategy')
    for (let n = 0; n < 4; n += 1) append(first)
    await client.save('strategy', second, 3)
    preConflict.resolve(response({ revision: 2 })); await read
    expect(client.state.head).toBe(4)
    expect(client.state.conflict.serverHead).toBe(4)
  })

  it('P2-b puts mine back on the canvas before Keep mine sends its save', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    append(graph('server'))
    await client.save('strategy', second, 1)
    let canvas = second
    const take = value => { canvas = value }
    render(<LabSaveBar document={second} ready client={client} onTakeTheirs={take} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    await screen.findByText('Loaded r2. Your edits are kept.')
    expect(canvas).toEqual(graph('server'))
    fetchImpl.mockImplementationOnce(async (url, init) => {
      expect(canvas).toEqual(second)
      expect(JSON.parse(init.body)).toMatchObject({ graph: second, expected_head: 2 })
      return response({ revision: 3, graph: second })
    })
    fireEvent.click(screen.getByRole('button', { name: 'Keep mine' }))
    await screen.findByText('Saved r3')
    expect(canvas).toEqual(second)
  })

  it('Keep mine captures edits made while Save was pending before restoring and retrying', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    await client.head('strategy')
    append(graph('server'))
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    const take = vi.fn()
    const view = render(<LabSaveBar document={first} ready client={client} onTakeTheirs={take} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    expect(JSON.parse(fetchImpl.mock.calls.at(-1)[1].body)).toMatchObject({ graph: first, expected_head: 1 })
    view.rerender(<LabSaveBar document={second} ready client={client} onTakeTheirs={take} />)
    await act(async () => pending.resolve(response({ detail: 'stale' }, 409)))
    await screen.findByText('Someone saved r2 while you edited.')
    fetchImpl.mockImplementationOnce(async (url, init) => {
      expect(take).toHaveBeenCalledWith(second)
      expect(JSON.parse(init.body)).toMatchObject({ graph: second, expected_head: 2 })
      return response({ revision: 3, graph: second })
    })
    fireEvent.click(screen.getByRole('button', { name: 'Keep mine' }))
    await screen.findByText('Saved r3')
    expect(take).toHaveBeenCalledTimes(1)
    expect(take).toHaveBeenCalledWith(second)
  })

  it('Keep mine restores the captured draft after Take theirs replaces the live document', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    const server = graph('server')
    append(server)
    await client.save('strategy', first, 1)
    const take = vi.fn()
    const view = render(<LabSaveBar document={second} ready client={client} onTakeTheirs={take} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    await screen.findByText('Loaded r2. Your edits are kept.')
    expect(take).toHaveBeenLastCalledWith(server)
    view.rerender(<LabSaveBar document={server} ready client={client} onTakeTheirs={take} />)
    // A repeated Take theirs must also leave the first captured draft intact.
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Keep mine' })).toBeEnabled())
    expect(take).toHaveBeenLastCalledWith(server)
    expect(client.state.conflict.mine).toEqual(second)
    fireEvent.click(screen.getByRole('button', { name: 'Keep mine' }))
    expect(take).toHaveBeenLastCalledWith(second)
    await screen.findByText('Saved r3')
    const body = JSON.parse(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/save')).at(-1)[1].body)
    expect(body).toEqual({ graph: second, expected_head: 2 })
  })

  it('Keep mine twice saves the same captured draft even if the live document changes', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    append(graph('server'))
    await client.save('strategy', first, 1)
    const take = vi.fn()
    const view = render(<LabSaveBar document={second} ready client={client} onTakeTheirs={take} />)
    fetchImpl.mockResolvedValueOnce(response({ detail: 'unavailable' }, 503))
    fireEvent.click(screen.getByRole('button', { name: 'Keep mine' }))
    await screen.findByText('Save failed: Something went wrong. Try again.')
    expect(client.state.conflict).toMatchObject({ mine: second, captured: true })
    view.rerender(<LabSaveBar document={graph('later edit')} ready client={client} onTakeTheirs={take} />)
    fireEvent.click(screen.getByRole('button', { name: 'Keep mine' }))
    await screen.findByText('Saved r3')
    expect(take.mock.calls.map(([document]) => document)).toEqual([second, second])
    const bodies = fetchImpl.mock.calls.filter(([url]) => url.endsWith('/save')).slice(-2).map(([, init]) => JSON.parse(init.body))
    expect(bodies).toEqual([
      { graph: second, expected_head: 2 },
      { graph: second, expected_head: 2 },
    ])
  })

  it('P2-c reconciles restore and publish conflicts and reloads revisions before retry', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    append(second)
    expect(await client.restore('strategy', 1, 1)).toMatchObject({ conflict: true, head: 2 })
    expect(client.state.head).toBe(2)
    expect(client.state.revisions.map(row => row.revision)).toEqual([1, 2])
    await client.restore('strategy', 1, client.state.head)
    expect(JSON.parse(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/restore')).at(-1)[1].body).expected_head).toBe(2)
    append(second)
    expect(await client.publish('strategy', client.state.head)).toMatchObject({ conflict: true, head: 4 })
    expect(client.state.head).toBe(4)
    expect(client.state.revisions.map(row => row.revision)).toEqual([1, 2, 3, 4])
    expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/revisions'))).toHaveLength(2)
  })

  it('P2-d retains the known head after a failed conflict lookup and offers retry', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    await client.head('strategy')
    append(second)
    fetchImpl.mockResolvedValueOnce(response({ detail: 'stale', head: 2 }, 409)).mockResolvedValueOnce(response({ detail: 'unavailable' }, 503))
    render(<LabSaveBar document={second} ready client={client} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    const retry = await screen.findByRole('button', { name: 'Head unknown, retry' })
    expect(client.state.head).toBe(1)
    expect(client.state.conflict.serverHead).toBe(2)
    expect(screen.getByRole('button', { name: 'Keep mine' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Take theirs' })).toBeEnabled()
    const count = fetchImpl.mock.calls.length
    fireEvent.click(retry)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Head unknown, retry' })).toBeNull())
    expect(fetchImpl.mock.calls.length).toBe(count + 1)
    expect(fetchImpl.mock.calls.at(-1)[0]).toMatch(/\/head$/)
    expect(client.state.head).toBe(2)
  })

  it('P2-f resets selection, comparison and status and ignores A list completion in B', async () => {
    const { client, append, fetchImpl } = ledger()
    append(first); append(second)
    render(<HistoryView client={client} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Publish r2' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Before revision'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('After revision'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Compare', exact: true }))
    await screen.findByText('Compared revisions.')
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    act(() => globalThis.dispatchEvent(new CustomEvent(SAVED_REVISION_EVENT, { detail: { doc_id: 'strategy' } })))
    act(() => client.setDocId('B'))
    await waitFor(() => expect(screen.queryByText('Loading history…')).toBeNull())
    expect(screen.getByLabelText('Before revision')).toHaveValue('')
    expect(screen.getByLabelText('After revision')).toHaveValue('')
    expect(screen.queryByText('Compared revisions.')).toBeNull()
    expect(screen.queryByRole('list', { name: 'Revision changes' })).toBeNull()
    await act(async () => pending.resolve(response({ revisions: [{ revision: 99, created_at: 1800000000 }] })))
    expect(screen.queryByTestId('vh-row-v99')).toBeNull()
  })

  it('P2-g Load older requests before the oldest revision and merges without duplicates', async () => {
    const row = revision => ({ revision, created_at: 1800000000 + revision })
    const fetchImpl = vi.fn(async url => response(url.endsWith('/head') ? { revision: 4 }
      : { revisions: url.includes('before=3') ? [row(2), row(3)] : [row(3), row(4)] }))
    const client = createLabArtifactsClient({ fetchImpl })
    render(<HistoryView client={client} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load older' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Load older' }))
    await screen.findByTestId('vh-row-v2')
    expect(fetchImpl.mock.calls.at(-1)[0]).toMatch(/revisions\?before=3$/)
    expect(client.state.revisions.map(item => item.revision)).toEqual([2, 3, 4])
    expect(screen.getAllByTestId('vh-row-v3')).toHaveLength(1)
  })

  it('P2-h renders registry-only block list changes and nested registry and title changes', () => {
    const next = copy(first)
    next.blocks[0].registry = { id: 'count', version: '2.0.0' }
    const view = render(<LabDiffView changes={[{ op: 'replace', path: '/blocks', before: first.blocks, after: next.blocks }]} />)
    expect(screen.getByText('Changed registry on field-1: field@1.0.0 -> count@2.0.0')).toBeVisible()
    view.rerender(<LabDiffView beforeGraph={first} afterGraph={next} changes={[
      { op: 'replace', path: '/blocks/0/registry/id', before: 'field', after: 'count' },
      { op: 'replace', path: '/blocks/0/title', before: 'Old', after: 'New' },
    ]} />)
    expect(screen.getByText('Changed registry on field-1: field@1.0.0 -> count@2.0.0')).toBeVisible()
    expect(screen.getByText('Changed title on field-1')).toBeVisible()
  })

  it('P3-a shows initial and refresh loading and disables cached actions until completion', async () => {
    const { client, append, fetchImpl } = ledger()
    append(first); append(second)
    const initial = deferred()
    fetchImpl.mockImplementationOnce(() => initial.promise)
    render(<HistoryView client={client} />)
    expect(screen.getByText('Loading history…')).toBeVisible()
    await act(async () => initial.resolve(response({ revisions: copy(ledgerRows()) })))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Publish r2' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Before revision'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('After revision'), { target: { value: '2' } })
    const pending = deferred()
    fetchImpl.mockImplementationOnce(() => pending.promise)
    act(() => globalThis.dispatchEvent(new CustomEvent(SAVED_REVISION_EVENT, { detail: { doc_id: 'strategy' } })))
    expect(screen.getByText('Loading history…')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Compare', exact: true })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Restore', exact: true })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Publish r2' })).toBeDisabled()
    await act(async () => pending.resolve(response({ revisions: ledgerRows() })))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Compare', exact: true })).toBeEnabled())
    expect(screen.getByRole('button', { name: 'Restore', exact: true })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Publish r2' })).toBeEnabled()
    function ledgerRows() { return [1, 2].map(revision => ({ revision, created_at: 1800000000 + revision })) }
  })

  it('loads only the diff route when graphs are disabled', async () => {
    const { client, append, fetchImpl } = ledger()
    append(first)
    append(second)
    const result = await client.diff('strategy', 1, 2, { graphs: false })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/docs/strategy/diff?before=1&after=2`)
    expect(result).toEqual({ ok: true, before: 1, after: 2, changes })
    expect(result).not.toHaveProperty('beforeGraph')
    expect(result).not.toHaveProperty('afterGraph')
  })

  it('loads both compared graphs by default with three requests', async () => {
    const { client, append, fetchImpl } = ledger()
    append(first)
    append(second)
    const result = await client.diff('strategy', 1, 2)
    expect(fetchImpl).toHaveBeenCalledTimes(3)
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      `${config.apiBase}/api/lab/docs/strategy/diff?before=1&after=2`,
      `${config.apiBase}/api/lab/docs/strategy/revisions/1`,
      `${config.apiBase}/api/lab/docs/strategy/revisions/2`,
    ])
    expect(result).toEqual({ ok: true, before: 1, after: 2, changes, beforeGraph: first, afterGraph: second })
  })

  it('saves r1/r2, compares settings and pipes, restores r1 as r3 and publishes through the drawer primitive', async () => {
    const { client, fetchImpl } = ledger()
    const events = []
    const listener = event => events.push(event.detail)
    globalThis.addEventListener(SAVED_REVISION_EVENT, listener)
    const base = config.apiBase
    const token = localStorage.getItem('leaf.jwt')
    config.apiBase = 'https://studio.example.test'
    localStorage.setItem('leaf.jwt', 'test-token')
    try {
      await client.head('strategy')
      const save = render(<LabSaveBar document={first} ready client={client} />)
      fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
      await waitFor(() => expect(client.state.head).toBe(1))
      save.rerender(<LabSaveBar document={second} ready client={client} />)
      fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
      await waitFor(() => expect(client.state.head).toBe(2))
      expect(screen.getByText('Saved r2 · 22222222')).toBeVisible()
      expect(screen.getByText('Save p50 0 ms · p99 0 ms')).toBeVisible()
      const saves = fetchImpl.mock.calls.filter(([url]) => url.endsWith('/save'))
      expect(saves.map(([, init]) => JSON.parse(init.body).expected_head)).toEqual([null, 1])
      render(<HistoryView client={client} />)
      await screen.findByTestId('vh-row-v2')
      expect(within(screen.getByTestId('vh-row-v1')).getByText('artifact')).toBeVisible()
      fireEvent.change(screen.getByRole('combobox', { name: 'Before revision' }), { target: { value: '1' } })
      fireEvent.change(screen.getByRole('combobox', { name: 'After revision' }), { target: { value: '2' } })
      fireEvent.click(screen.getByRole('button', { name: 'Compare', exact: true }))
      await screen.findByText('Changed setting field on field-1: price -> size')
      expect(screen.getByText('Added pipe source/rows -> field-1/value')).toBeVisible()
      fireEvent.click(within(screen.getByTestId('vh-row-v1')).getByRole('button', { name: 'Restore', exact: true }))
      fireEvent.click(screen.getByRole('button', { name: 'Restore v1', exact: true }))
      await screen.findByTestId('vh-row-v3')
      expect(within(screen.getByTestId('vh-row-v3')).getByText('head')).toBeVisible()
      expect(client.state.restoredGraph).toEqual(first)
      fireEvent.click(screen.getByRole('button', { name: 'Publish r3', exact: true }))
      await screen.findByText(/receipt-3/)
      expect(within(screen.getByTestId('vh-row-v3')).getByText('Published r3')).toBeVisible()
      expect(events.map(({ kind, revision }) => [kind, revision])).toEqual([['save', 1], ['save', 2], ['restore', 3], ['publish', 3]])
      expect(events[2].sha256).toBe(events[0].sha256)
      expect(events[3].sha256).toBe(events[2].sha256)
      expect(client.state.events).toEqual(events)
      for (const event of events) {
        expect(Object.keys(event).sort()).toEqual(['at', 'doc_id', 'kind', 'revision', 'sha256'])
        expect(event.sha256).toHaveLength(64)
        expect(event.doc_id).toBe('strategy')
      }
      for (const [url, init] of fetchImpl.mock.calls) {
        expect(url.startsWith(`${config.apiBase}/api/lab/docs/strategy/`)).toBe(true)
        expect(url).not.toMatch(/kashbot|leafhft/)
        expect(init).toMatchObject({ credentials: 'same-origin', redirect: 'error', headers: {
          accept: 'application/json', 'content-type': 'application/json', 'X-Tenant-Id': config.tenant, [['Author', 'ization'].join('')]: ['Bearer', 'test-token'].join(' '),
        } })
      }
    } finally {
      globalThis.removeEventListener(SAVED_REVISION_EVENT, listener)
      config.apiBase = base
      if (token === null) localStorage.removeItem('leaf.jwt')
      else localStorage.setItem('leaf.jwt', token)
    }
  })

  it('keeps the restored head and revisions when earlier reads finish, then saves against that head', async () => {
    const { client, fetchImpl, rows } = ledger()
    await client.save('strategy', first, null)
    await client.save('strategy', second, 1)
    const staleHead = copy(rows[1])
    const staleRevisions = copy(rows)
    let resolveHead
    let resolveRevisions
    fetchImpl.mockImplementationOnce(() => new Promise(resolve => { resolveHead = resolve }))
    const head = client.head('strategy')
    fetchImpl.mockImplementationOnce(() => new Promise(resolve => { resolveRevisions = resolve }))
    const revisions = client.revisions('strategy')
    await client.restore('strategy', 1, 2)
    expect(client.state.head).toBe(3)
    resolveHead({ ok: true, json: async () => staleHead })
    resolveRevisions({ ok: true, json: async () => ({ revisions: staleRevisions }) })
    expect(await head).toMatchObject({ ok: true, revision: 2 })
    expect((await revisions).revisions).toHaveLength(2)
    expect(client.state.head).toBe(3)
    expect(client.state.revisions.map(row => row.revision)).toEqual([1, 2, 3])
    await client.save('strategy', second, client.state.head)
    const body = JSON.parse(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/save')).at(-1)[1].body)
    expect(body.expected_head).toBe(3)
    expect(client.state.head).toBe(4)
  })

  it('keeps the restored head when an earlier save conflict finishes, then saves against that head', async () => {
    const { client, fetchImpl, append, rows } = ledger()
    await client.save('strategy', first, null)
    await client.save('strategy', second, 1)
    append(graph('server'))
    const staleHead = copy(rows[2])
    let resolveHead
    fetchImpl.mockImplementationOnce(async () => ({ ok: false, status: 409, json: async () => ({ detail: 'stale_expected_head' }) }))
    fetchImpl.mockImplementationOnce(() => new Promise(resolve => { resolveHead = resolve }))
    const save = client.save('strategy', second, client.state.head)
    await waitFor(() => expect(resolveHead).toBeTypeOf('function'))
    await client.restore('strategy', 1, 3)
    expect(client.state.head).toBe(4)
    expect(client.state.conflict).toBeNull()
    resolveHead({ ok: true, json: async () => staleHead })
    expect(await save).toMatchObject({ ok: false, conflict: true, head: 3, status: 409 })
    expect(client.state.head).toBe(4)
    expect(client.state.conflict).toBeNull()
    await client.save('strategy', second, client.state.head)
    const body = JSON.parse(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/save')).at(-1)[1].body)
    expect(body.expected_head).toBe(4)
    expect(client.state.head).toBe(5)
  })

  it('sets head and conflict from an ordinary save conflict', async () => {
    const { client, append } = ledger()
    await client.save('strategy', first, null)
    append(graph('server'))
    expect(await client.save('strategy', second, client.state.head)).toMatchObject({ ok: false, conflict: true, head: 2, status: 409 })
    expect(client.state.head).toBe(2)
    expect(client.state.conflict).toEqual({ serverHead: 2, mine: second })
  })

  it('ignores pending head and revisions reads after switching documents', async () => {
    const { client, fetchImpl, append } = ledger()
    const row = append(first)
    let resolveHead
    let resolveRevisions
    fetchImpl.mockImplementationOnce(() => new Promise(resolve => { resolveHead = resolve }))
    const head = client.head('strategy')
    fetchImpl.mockImplementationOnce(() => new Promise(resolve => { resolveRevisions = resolve }))
    const revisions = client.revisions('strategy')
    client.setDocId('other')
    resolveHead({ ok: true, json: async () => ({ ...row, published_revision: 1 }) })
    resolveRevisions({ ok: true, json: async () => ({ revisions: [row] }) })
    expect(await head).toMatchObject({ ok: true, revision: 1 })
    expect((await revisions).revisions).toEqual([row])
    expect(client.currentDocId()).toBe('other')
    expect(client.state.head).toBeNull()
    expect(client.state.published).toBeNull()
    expect(client.state.revisions).toEqual([])
  })

  it('sets head and published from an ordinary head read', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => ({ revision: 3, published_revision: 2 }) }))
    const client = createLabArtifactsClient({ fetchImpl })
    expect(await client.head('strategy')).toEqual({ ok: true, revision: 3, published_revision: 2 })
    expect(client.state.head).toBe(3)
    expect(client.state.published).toBe(2)
  })

  it('restores Published r3 in the save bar and drawer on a fresh head read', async () => {
    const row = { revision: 3, parent_revision: 2, author: 'tester', created_at: 1800000003,
      sha256: '1'.repeat(64), graph: first }
    const { graph: unused, ...metadata } = row
    const fetchImpl = vi.fn(async url => ({ ok: true, json: async () => url.endsWith('/head')
      ? { ...row, published_revision: 3 } : { revisions: [metadata] } }))
    const client = createLabArtifactsClient({ fetchImpl })
    expect(client.state.published).toBeNull()
    render(<><LabSaveBar document={first} ready client={client} /><HistoryView client={client} /></>)
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Save Strategy' })).getByText('Published r3')).toBeVisible())
    expect(within(await screen.findByTestId('vh-row-v3')).getByText('Published r3')).toBeVisible()
    expect(client.state.events).toEqual([])
    expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/head'))).toHaveLength(1)
    expect(fetchImpl.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true)
  })

  it('keeps mine byte-identical and retries against the conflict head', async () => {
    const { client, append, fetchImpl } = ledger()
    await client.save('strategy', first, null)
    await client.head('strategy')
    append(graph('server'))
    const bytes = JSON.stringify(second)
    render(<LabSaveBar document={second} ready client={client} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    await screen.findByText('Someone saved r2 while you edited.')
    fireEvent.click(screen.getByRole('button', { name: 'Keep mine' }))
    await waitFor(() => expect(client.state.head).toBe(3))
    const body = JSON.parse(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/save')).at(-1)[1].body)
    expect(body.expected_head).toBe(2)
    expect(JSON.stringify(body.graph)).toBe(bytes)
    expect(JSON.stringify(second)).toBe(bytes)
    expect(client.state.conflict).toBeNull()
  })

  it('takes theirs while retaining mine for recovery', async () => {
    const { client, append } = ledger()
    await client.save('strategy', first, null)
    await client.head('strategy')
    const server = graph('server')
    append(server)
    const bytes = JSON.stringify(second)
    const onTakeTheirs = vi.fn()
    render(<LabSaveBar document={second} ready client={client} onTakeTheirs={onTakeTheirs} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    await screen.findByText('Someone saved r2 while you edited.')
    fireEvent.click(screen.getByRole('button', { name: 'Take theirs' }))
    await waitFor(() => expect(onTakeTheirs).toHaveBeenCalledWith(server))
    await screen.findByText('Loaded r2. Your edits are kept.')
    expect(JSON.stringify(client.state.conflict.mine)).toBe(bytes)
    fireEvent.click(screen.getByRole('button', { name: 'Restore my edits' }))
    expect(JSON.stringify(onTakeTheirs.mock.calls.at(-1)[0])).toBe(bytes)
    expect(JSON.stringify(second)).toBe(bytes)
  })

  it('refuses document ids before any request and maps HTTP errors', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 413, json: async () => ({ detail: 'lab_body_too_large' }) }))
    const client = createLabArtifactsClient({ fetchImpl })
    expect(() => client.setDocId('../x')).toThrow('Enter a valid Document ID.')
    for (const id of ['../x', 'strategy\n', '', 'x'.repeat(129)]) {
      await expect(client.save(id, first, null)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.head(id)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.revisions(id)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.revision(id, 1)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.exportBundle(id, 1)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.diff(id, 1, 2)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.restore(id, 1, 2)).rejects.toThrow('Enter a valid Document ID.')
      await expect(client.publish(id, 1)).rejects.toThrow('Enter a valid Document ID.')
    }
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(await client.save('strategy', first, null)).toEqual({ ok: false, status: 413, detail: 'This document is too large to save.' })
    expect(client.state.events).toEqual([])
  })

  it('maps a capacity 413 and a bare 413 to plain sentences', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response({ detail: 'lab_capacity_exceeded' }, 413))
      .mockResolvedValueOnce(response({}, 413))
      .mockResolvedValueOnce(response({ detail: 'lab_store_unavailable' }, 503))
    const client = createLabArtifactsClient({ fetchImpl })
    expect(await client.save('strategy', first, null)).toEqual({ ok: false, status: 413, detail: 'This save is over the Lab storage limit.' })
    expect(await client.save('strategy', first, null)).toEqual({ ok: false, status: 413, detail: 'This document is too large to save.' })
    expect(await client.save('strategy', first, null)).toEqual({ ok: false, status: 503, detail: humanizeError('lab_store_unavailable') })
  })

  it('bounds event history and notifies subscribers until they unsubscribe', async () => {
    const { client } = ledger()
    const listener = vi.fn()
    const unsubscribe = client.subscribe(listener)
    for (let revision = 0; revision < 22; revision += 1) await client.save('strategy', first, revision || null)
    expect(client.state.events).toHaveLength(20)
    expect(client.state.saveTimes).toHaveLength(20)
    expect(client.state.events[0].revision).toBe(3)
    unsubscribe()
    listener.mockClear()
    await client.head('strategy')
    expect(listener).not.toHaveBeenCalled()
  })

  it('disables Save for an unready draft and shows measured save percentiles', async () => {
    let time = 1000
    let count = 0
    const client = createLabArtifactsClient({ now: () => time, fetchImpl: async () => {
      count += 1
      time += count * 10
      return { ok: true, json: async () => ({ revision: count, sha256: 'a'.repeat(64), graph: first }) }
    } })
    client.state.headLoaded = true
    const view = render(<LabSaveBar document={first} ready={false} client={client} />)
    expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeDisabled()
    view.rerender(<LabSaveBar document={null} ready client={client} />)
    expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeDisabled()
    view.rerender(<LabSaveBar document={first} ready client={client} />)
    for (let revision = 1; revision <= 3; revision += 1) {
      fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
      await waitFor(() => expect(client.state.head).toBe(revision))
    }
    expect(screen.getByText('Save p50 20 ms · p99 30 ms')).toBeVisible()
  })

  it('loads only when shown and reloads after saved-revision events', async () => {
    const { client, fetchImpl } = ledger()
    const view = render(<HistoryView hidden client={client} />)
    expect(fetchImpl).not.toHaveBeenCalled()
    view.rerender(<HistoryView client={client} />)
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2))
    await act(async () => { await client.save('strategy', first, null) })
    await screen.findByTestId('vh-row-v1')
    expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/revisions'))).toHaveLength(2)
  })

  it('renders semantic changes without serializing payloads', () => {
    render(<LabDiffView changes={[
      { op: 'add', path: '/blocks/1', after: { id: 'count', registry: { id: 'count' }, secret: 'DO-NOT-RENDER' } },
      { op: 'remove', path: '/blocks/0', before: first.blocks[0] },
      { op: 'remove', path: '/pipes/0', before: second.pipes[0] },
      { op: 'replace', path: '/outputs/answer', before: {}, after: { secret: 'DO-NOT-RENDER' } },
      { op: 'replace', path: '/unknown', after: { secret: 'DO-NOT-RENDER' } },
    ]} />)
    expect(screen.getByText('Added block count (count)')).toBeVisible()
    expect(screen.getByText('Removed block field-1 (field)')).toBeVisible()
    expect(screen.getByText('Removed pipe source/rows -> field-1/value')).toBeVisible()
    expect(screen.getByText('Changed output answer')).toBeVisible()
    expect(screen.getByText('replace /unknown')).toBeVisible()
    expect(screen.queryByText(/DO-NOT-RENDER/)).not.toBeInTheDocument()
  })
})
