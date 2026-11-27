import { webcrypto } from 'node:crypto'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { canonicalJSON } from '../contract.js'
import { sha256Hex } from '../runtime/sha256.js'
import HistoryView from './HistoryView.jsx'
import { createLabArtifactsClient } from './labArtifactsClient.js'

const auth = vi.hoisted(() => ({ logout: new Set(), stored: new Set() }))
vi.mock('../../auth.js', async importOriginal => ({
  ...await importOriginal(),
  subscribeBeforeLogout: listener => { auth.logout.add(listener); return () => auth.logout.delete(listener) },
  subscribeTokenStored: listener => { auth.stored.add(listener); return () => auth.stored.delete(listener) },
}))

const hash = value => sha256Hex(canonicalJSON(value))
function bundle(doc = 'example', revision = 1, value = 1) {
  const graph = { schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: {}, label: value }
  const identity = { tenant_id: 'tenant', doc_id: doc, revision, kind: 'graph', sha256: hash(graph) }
  const run = { ...identity, run_id: 'run-' + revision, graph_revision: revision, graph_sha256: identity.sha256 }
  const context = { policy: { block_id: 'evaluate' }, corpus: { material_id: 'corpus' }, suite: { material_id: 'suite' } }
  const parts = {
    identity, graph, layout: null,
    policy_reference: { graph_sha256: identity.sha256, blocks: [], compiled: [] },
    evaluations: [{ run, events: [], complete_results: {
      ['job-' + revision]: { context, result_sha256: sha256Hex(JSON.stringify({ value }) + '\n'), result: { value } },
    } }],
  }
  const digests = Object.fromEntries(Object.entries(parts).map(([key, entry]) => [key, hash(entry)]))
  return { schema: 'leaf.lab-bundle.v1', parts, manifest: { algorithm: 'sha256', parts: digests } }
}
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })

function mount(exportResponse) {
  const fetchImpl = vi.fn(async (url, request) => {
    const path = new URL(url, 'https://example.test').pathname
    expect(request.method).toBe('GET')
    if (path.endsWith('/head')) return response({ revision: 4 })
    if (path.endsWith('/revisions')) return response({ revisions: [] })
    if (path.endsWith('/export')) return exportResponse(path)
    throw new Error('Unexpected request: ' + path)
  })
  const client = createLabArtifactsClient({ fetchImpl })
  render(<HistoryView client={client} />)
  fireEvent.click(screen.getByText('Compare exported revisions'))
  return { client, fetchImpl }
}
function choose(side, value, spacing) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, spacing)
  const file = new File([text], 'revision.json', { type: 'application/json' })
  fireEvent.change(screen.getByLabelText(side + ' export'), { target: { files: [file] } })
  return text
}
const digests = () => screen.getByRole('table', { name: 'Exported revision digests' })
const partRow = label => within(digests()).getByRole('row', { name: new RegExp('^' + label + ' ') })

beforeEach(() => vi.stubGlobal('crypto', webcrypto))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('compares byte hashes and canonical digests separately, with producer lineage', async () => {
  const value = bundle()
  const { client } = mount(() => response(value))
  const before = choose('Before', value)
  const after = choose('After', value, 2)
  await screen.findByText(/No canonical parts changed/)
  expect(partRow('File bytes').textContent).toBe('File bytes' + sha256Hex(before) + sha256Hex(after) + 'Changed')
  expect(partRow('Graph').textContent).toMatch(/Same$/)
  const lineage = screen.getByRole('table', { name: 'Producer lineage' })
  expect(within(lineage).getAllByText('Matches server-verified result')).toHaveLength(2)
  expect(within(lineage).getByText('Uploaded result bytes match declared digest')).toBeTruthy()
  expect(within(lineage).getByText('Uploaded result bytes differ from declared digest')).toBeTruthy()
  expect(within(lineage).getByRole('rowheader', { name: 'evaluate / corpus / suite' })).toBeTruthy()
  expect(client.currentDocId()).toBe('strategy')
  expect(client.state.openedBundle).toBeNull()
})

it.each([
  ['numeric spelling', 1, '{"value":1.0}'],
  ['Unicode escapes', 'é🌱', '{"value":"\\u00e9\\ud83c\\udf31"}'],
  ['escaped delimiters', { nested: ['result', '}],"result":\\'] }, '{"value": {"nested":["result","}],\\"result\\":\\\\"]}}'],
])('keeps %s in original producer bytes', async (_, resultValue, producerText) => {
  const value = bundle('example', 1, resultValue)
  const evaluation = value.parts.evaluations[0]
  evaluation.complete_results['job-1'].result_sha256 = sha256Hex(producerText + '\n')
  value.manifest.parts.evaluations = hash(value.parts.evaluations)
  const standard = JSON.stringify(value)
  const original = standard.replace('"result":' + JSON.stringify({ value: resultValue }), '"result":' + producerText)
  expect(JSON.parse(original)).toEqual(value)
  mount(() => response(value))
  choose('Before', original)
  choose('After', standard)
  await screen.findByText(/No canonical parts changed/)
  const [, before, after] = within(screen.getByRole('table', { name: 'Producer lineage' })).getAllByRole('row')[1].cells
  expect(before).toHaveTextContent('Uploaded result bytes match declared digest')
  expect(after).toHaveTextContent('Uploaded result bytes differ from declared digest')
  expect(screen.getAllByText('Matches server-verified result')).toHaveLength(2)
})

it('reports changed graph and evaluations without restoring the canvas', async () => {
  const before = bundle('example', 1, 10), after = bundle('example', 2, 20)
  const { client } = mount(path => response(path.endsWith('/1/export') ? before : after))
  choose('Before', before)
  choose('After', after)
  await screen.findByText(/Canonical parts changed/)
  expect(partRow('Graph').textContent).toMatch(/Changed$/)
  expect(partRow('Evaluations').textContent).toMatch(/Changed$/)
  expect(partRow('Layout').textContent).toMatch(/Same$/)
  expect(client.state.restoredGraph).toBeNull()
  expect(client.state.head).toBe(4)
})

it('clears prior results on access denial and blocks invalid manifests before requesting access', async () => {
  const value = bundle(), blocked = bundle('other')
  const { fetchImpl } = mount(path => path.includes('/other/') ? response({}, 403) : response(value))
  choose('Before', value)
  choose('After', value)
  await screen.findByText(/No canonical parts changed/)
  choose('After', blocked)
  expect(screen.queryByRole('table')).toBeNull()
  expect((await screen.findByRole('alert')).textContent).toMatch(/After export: You no longer have access/)
  expect(screen.queryByText(/other r1/)).toBeNull()
  const calls = fetchImpl.mock.calls.length
  value.parts.evaluations[0].events.push({ tenant_id: 'tenant', run_id: 'run-1' })
  choose('After', value)
  expect((await screen.findByRole('alert')).textContent).toMatch(/integrity check failed/)
  expect(fetchImpl).toHaveBeenCalledTimes(calls)
})

it('drops late authorization responses after file replacement and account reset', async () => {
  const value = bundle(), old = bundle('old'), stale = bundle('stale')
  let resolveOld, resolveStale
  const pendingOld = new Promise(resolve => { resolveOld = resolve })
  const pendingStale = new Promise(resolve => { resolveStale = resolve })
  const { fetchImpl } = mount(path => path.includes('/old/') ? pendingOld : path.includes('/stale/') ? pendingStale : response(value))
  choose('Before', value)
  choose('After', old)
  await waitFor(() => expect(fetchImpl.mock.calls.some(([url]) => url.includes('/old/'))).toBe(true))
  choose('After', value)
  await screen.findByText(/No canonical parts changed/)
  await act(async () => resolveOld(response(old)))
  expect(digests()).toBeTruthy()
  expect(screen.queryByText(/old r1/)).toBeNull()
  choose('After', stale)
  await waitFor(() => expect(fetchImpl.mock.calls.some(([url]) => url.includes('/stale/'))).toBe(true))
  await act(async () => { for (const listener of auth.logout) listener(); resolveStale(response(stale)) })
  expect(screen.queryByRole('table')).toBeNull()
  expect(screen.queryByText(/export: .* r1/)).toBeNull()
})
