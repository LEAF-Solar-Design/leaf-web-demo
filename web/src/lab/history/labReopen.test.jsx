import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GraphView from '../canvas/GraphView.jsx'
import { addBlock, emptyGraph } from '../canvas/graphDoc.js'
import { registry } from '../canvas/registry.js'
import { setLayoutEngineLoader } from '../canvas/dependencyLayout.js'
import { createHostProvider } from '../host/hostProvider.js'
import { createLabArtifactsClient, GRAPH_DOC_KEY } from './labArtifactsClient.js'

const elkImport = vi.hoisted(() => vi.fn())
const authLifecycle = vi.hoisted(() => ({ logouts: new Set(), stored: new Set() }))
vi.mock('../../auth.js', async importOriginal => ({
  ...await importOriginal(),
  subscribeBeforeLogout: fn => { authLifecycle.logouts.add(fn); return () => authLifecycle.logouts.delete(fn) },
  subscribeTokenStored: fn => { authLifecycle.stored.add(fn); return () => authLifecycle.stored.delete(fn) },
}))
vi.mock('elkjs/lib/elk.bundled.js', () => {
  elkImport()
  throw new Error('Real ELK is forbidden under Vitest')
})
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: point => point, getNodes: () => [], fitView: () => {} }),
  ReactFlow: ({ nodes, edges, nodeTypes, edgeTypes, onNodeClick, onNodesChange, onConnect, onEdgesChange, children }) => <div data-testid="flow">
    {nodes.length > 1 && <button onClick={() => onConnect({ source: nodes[0].id, sourceHandle: 'result', target: nodes[1].id, targetHandle: 'value' })}>Connect first pair</button>}
    {edges.length > 0 && <button onClick={() => onEdgesChange([{ type: 'remove', id: edges[0].id }])}>Delete first pipe</button>}
    {nodes.map(node => { const Card = nodeTypes[node.type]; return <div key={node.id} data-testid={node.id} data-position={JSON.stringify(node.position)}>
      <button onClick={() => onNodeClick(null, node)}>Select {node.id}</button>
      <button onClick={() => onNodesChange([{ type: 'position', id: node.id, position: { x: 777, y: 888 } }])}>Move {node.id}</button>
      <Card {...node} />
    </div> })}<span data-testid="pipe-count">{edges.length}</span>
    {edges.map(edge => { const Pipe = edgeTypes[edge.type]; return <div key={edge.id} data-testid={`budget-${edge.id}`}>
      {JSON.stringify(edge.data.budget)}<Pipe {...edge} />
    </div> })}{children}
  </div>,
  Background: () => null, Controls: () => null, Handle: props => <span data-testid="handle" aria-label={props['aria-label']} />,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))

const now = 1800000000000
const rows = [{ source: 'markets', as_of: now, data: { price: 42, marker: 'CANARY-5e1d' } }]
const pipe = (from, port, to) => ({ from: { node: from, port }, to: { node: to, port: 'value' } })
function graphOf(provider, ids) {
  const entries = { ...registry, ...provider.entries() }
  let document = emptyGraph()
  for (const id of ids) document = addBlock(document, entries[`${id}@1.0.0`])
  document.blocks = document.blocks.map(block => ({ ...block, settings: block.registry.id === 'host-source'
    ? { source: 'markets' } : block.registry.id === 'field' ? { field: 'price' } : block.settings }))
  document.pipes = document.blocks.slice(1).map((block, index) => pipe(document.blocks[index].id,
    index === 0 ? 'rows' : 'result', block.id))
  const last = document.blocks.at(-1)
  document.outputs = { answer: { node: last.id, port: ids.length === 1 ? 'rows' : 'result' } }
  return document
}
const hostProvider = () => {
  const provider = createHostProvider({ framed: true })
  provider.announce(rows)
  return provider
}
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })
function fakeStorage(value) {
  const values = new Map(value === undefined ? [] : [[GRAPH_DOC_KEY, value]])
  return { getItem: key => values.get(key) ?? null, setItem: (key, next) => values.set(key, next) }
}
const rememberedStorage = () => fakeStorage(JSON.stringify({ doc_id: 'roof-plan_2' }))
const paths = fetchImpl => fetchImpl.mock.calls.map(([url]) => new URL(url, 'https://example.test').pathname)
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function savedFixture(head) {
  const provider = hostProvider()
  const graph = graphOf(provider, ['host-source', 'count'])
  const fetchImpl = vi.fn(async url => {
    const path = new URL(url, 'https://example.test').pathname
    if (path === '/api/lab/docs/roof-plan_2/head') return head ? head.promise : response({ revision: 3 })
    if (path === '/api/lab/docs/roof-plan_2/revisions/3') return response({ revision: 3, graph })
    throw new Error(`Unexpected request ${url}`)
  })
  const client = createLabArtifactsClient({ storage: rememberedStorage(), fetchImpl })
  return { client, fetchImpl, provider, graph }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(now)
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  setLayoutEngineLoader(() => ({
    layout: graph => Promise.resolve({ ...graph, children: graph.children.map((node, index) => ({ ...node, x: index * 50, y: 0 })) }),
    terminateWorker: vi.fn(),
  }))
})
afterEach(async () => {
  cleanup()
  await act(async () => {})
  expect(elkImport).not.toHaveBeenCalled()
  setLayoutEngineLoader()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('remembered graph document', () => {
  it('remembers a real document switch for the next client', () => {
    const storage = fakeStorage()
    const client = createLabArtifactsClient({ storage })
    client.setDocId('roof-plan_2')
    expect(storage.getItem(GRAPH_DOC_KEY)).toBe('{"doc_id":"roof-plan_2"}')
    const next = createLabArtifactsClient({ storage })
    expect(next.currentDocId()).toBe('roof-plan_2')
    expect(next.rememberedDocId()).toBe('roof-plan_2')
  })

  it('remembers a successful save of the default document', async () => {
    const storage = fakeStorage()
    const client = createLabArtifactsClient({ storage, fetchImpl: async () => response({ revision: 1 }) })
    await client.save('strategy', emptyGraph(), null)
    expect(storage.getItem(GRAPH_DOC_KEY)).toBe('{"doc_id":"strategy"}')
  })

  it.each(['{', '{"doc_id":"a b"}', 'a'.repeat(600), 'null', '[]'])('ignores malformed stored value %s', value => {
    const client = createLabArtifactsClient({ storage: fakeStorage(value) })
    expect(client.currentDocId()).toBe('strategy')
    expect(client.rememberedDocId()).toBeNull()
  })

  it('ignores storage read and write failures', () => {
    const storage = fakeStorage()
    storage.getItem = () => { throw new Error('Storage blocked') }
    storage.setItem = () => { throw new Error('Storage blocked') }
    const client = createLabArtifactsClient({ storage })
    expect(client.currentDocId()).toBe('strategy')
    expect(client.rememberedDocId()).toBeNull()
    client.setDocId('roof-plan_2')
    expect(client.currentDocId()).toBe('roof-plan_2')
  })

  it('does not fetch without storage', async () => {
    const fetchImpl = vi.fn()
    const client = createLabArtifactsClient({ fetchImpl })
    expect(client.rememberedDocId()).toBeNull()
    expect(await client.reopenRemembered()).toEqual({ state: 'none' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('loads the remembered head and graph exactly once', async () => {
    const { client, fetchImpl, graph } = savedFixture()
    const result = await client.reopenRemembered()
    expect(result).toEqual({ state: 'ready', docId: 'roof-plan_2', revision: 3, graph, token: expect.any(Object) })
    expect(client.isCurrentOperation(result.token)).toBe(true)
    expect(paths(fetchImpl)).toEqual(['/api/lab/docs/roof-plan_2/head', '/api/lab/docs/roof-plan_2/revisions/3'])
    expect(await client.reopenRemembered()).toEqual({ state: 'none' })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it.each([404, 200])('treats an absent head (%s) as empty without requesting a revision', async status => {
    const fetchImpl = vi.fn(async () => response({ revision: null }, status))
    const client = createLabArtifactsClient({ storage: rememberedStorage(), fetchImpl })
    expect(await client.reopenRemembered()).toEqual({ state: 'empty' })
    expect(paths(fetchImpl)).toEqual(['/api/lab/docs/roof-plan_2/head'])
  })

  it('does not fetch after switching away from the remembered document', async () => {
    const { client, fetchImpl } = savedFixture()
    client.setDocId('another')
    expect(await client.reopenRemembered()).toEqual({ state: 'none' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('lets a newer graph operation supersede a pending head', async () => {
    const head = deferred()
    const { client, fetchImpl } = savedFixture(head)
    const pending = client.reopenRemembered()
    client.beginGraphOperation()
    head.resolve(response({ revision: 3 }))
    expect(await pending).toEqual({ state: 'superseded' })
    expect(paths(fetchImpl)).toEqual(['/api/lab/docs/roof-plan_2/head'])
  })

  it('reports head and missing-graph failures', async () => {
    const fetchImpl = vi.fn(async () => response({ detail: 'Offline' }, 503))
    const client = createLabArtifactsClient({ storage: rememberedStorage(), fetchImpl })
    expect(await client.reopenRemembered()).toEqual({ state: 'failed', detail: expect.any(String) })
    const missing = createLabArtifactsClient({ storage: rememberedStorage(), fetchImpl: async () => response({ revision: 3 }) })
    expect(await missing.reopenRemembered()).toEqual({ state: 'failed', detail: 'No saved graph.' })
  })
})

describe('GraphView reopens saved work', () => {
  it.each(['logout', 'stored'])('does not apply a pending reopen after explicit logout (%s)', async mode => {
    const head = deferred()
    const { client, provider } = savedFixture(head)
    const take = vi.spyOn(client, 'takeGraph')
    render(<GraphView client={client} provider={provider} initialDocument={emptyGraph()} />)
    await waitFor(() => expect(client.currentDocId()).toBe('roof-plan_2'))
    act(() => {
      for (const listener of authLifecycle.logouts) listener()
      if (mode === 'stored') for (const listener of authLifecycle.stored) listener()
    })
    await act(async () => head.resolve(response({ revision: 3 })))
    await waitFor(() => expect(client.state.head).toBe(3))
    await act(async () => {})
    expect(take).not.toHaveBeenCalled()
    expect(client.state.restoredGraph).toBeNull()
    expect(screen.queryByText('Reopened roof-plan_2 r3')).not.toBeInTheDocument()
  })

  it('puts the saved graph on a pristine canvas, including under StrictMode', async () => {
    const { client, provider, graph } = savedFixture()
    render(<StrictMode><GraphView client={client} provider={provider} initialDocument={emptyGraph()} /></StrictMode>)
    expect(await screen.findByText('Reopened roof-plan_2 r3')).toHaveAttribute('role', 'status')
    expect(client.state.restoredGraph).toEqual(graph)
    for (const block of graph.blocks) expect(await screen.findByTestId(block.id)).toBeVisible()
  })

  it('preserves edits made while the remembered head is loading', async () => {
    const head = deferred()
    const { client, provider } = savedFixture(head)
    const take = vi.spyOn(client, 'takeGraph')
    render(<GraphView client={client} provider={provider} initialDocument={emptyGraph()} />)
    const palette = await screen.findByRole('region', { name: 'Strategy palette' })
    // Wait for the empty canvas to establish its baseline before editing.
    await act(async () => {})
    fireEvent.click(within(palette).getByRole('button', { name: 'Count', exact: true }))
    expect(await screen.findByTestId('count-1')).toBeVisible()
    await act(async () => head.resolve(response({ revision: 3 })))
    expect(await screen.findByText('Saved graph roof-plan_2 r3 was not reopened because the canvas has edits. Choose Open to load it.')).toHaveAttribute('role', 'status')
    expect(take).not.toHaveBeenCalled()
    expect(client.state.restoredGraph).toBeNull()
    await waitFor(() => expect(screen.queryByTestId('host-source-1')).toBeNull())
  })
})
