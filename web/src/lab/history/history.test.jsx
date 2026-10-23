import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { config } from '../../api.js'
import HistoryView from './HistoryView.jsx'
import LabSaveBar from './LabSaveBar.jsx'
import LabDiffView from './LabDiffView.jsx'
import { createLabArtifactsClient, SAVED_REVISION_EVENT } from './labArtifactsClient.js'

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

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('Strategy revision ledger', () => {
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
          accept: 'application/json', 'content-type': 'application/json', 'X-Tenant-Id': config.tenant, Authorization: 'Bearer test-token',
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
    expect(() => client.setDocId('../x')).toThrow('Test: document id refused')
    for (const id of ['../x', 'strategy\n', '', 'x'.repeat(129)]) {
      await expect(client.save(id, first, null)).rejects.toThrow('Test: document id refused')
      await expect(client.head(id)).rejects.toThrow('Test: document id refused')
      await expect(client.revisions(id)).rejects.toThrow('Test: document id refused')
      await expect(client.revision(id, 1)).rejects.toThrow('Test: document id refused')
      await expect(client.diff(id, 1, 2)).rejects.toThrow('Test: document id refused')
      await expect(client.restore(id, 1, 2)).rejects.toThrow('Test: document id refused')
      await expect(client.publish(id, 1)).rejects.toThrow('Test: document id refused')
    }
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(await client.save('strategy', first, null)).toEqual({ ok: false, status: 413, detail: 'lab_body_too_large' })
    expect(client.state.events).toEqual([])
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
