import { useEffect, useState } from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GraphView, { LiveValues, useLiveGraph } from './GraphView.jsx'
import LabWorkbench from '../LabWorkbench.jsx'
import { createHostProvider } from '../host/hostProvider.js'
import * as hostProviderModule from '../host/hostProvider.js'
import * as runtime from '../runtime/index.js'
import { config } from '../../api.js'
import { addBlock, emptyGraph } from './graphDoc.js'
import { registry } from './registry.js'
import { setLayoutEngineLoader } from './dependencyLayout.js'
import { createLabArtifactsClient } from '../history/labArtifactsClient.js'
import { labValueStore } from '../runs/valueStore.js'

const elkImport = vi.hoisted(() => vi.fn())
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
const fixedClock = () => 1
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
const cards = () => within(screen.getByRole('region', { name: 'Values' }))
const cardNamed = title => cards().getByRole('heading', { name: title }).closest('article')

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
  vi.useRealTimers()
})

describe('first-click live graph', () => {
  it('links to Test trade evidence preserving the supplied route parameters', async () => {
    const original = window.location.href
    const view = render(<GraphView route={{ view: 'graph', params: { embed: '1', channel: 'retained' } }} />)
    try {
      await screen.findByRole('region', { name: 'Strategy palette' })
      const link = screen.getByRole('link', { name: 'Test trade evidence' })
      expect(link).toHaveAttribute('href', '/app/lab/test-trade?embed=1&channel=retained')
      fireEvent.click(link)
      expect(window.location.pathname).toBe('/app/lab/test-trade')
      expect(window.location.search).toBe('?embed=1&channel=retained')
    } finally {
      view.unmount()
      window.history.replaceState({}, '', original)
    }
  })
  it('F2 renders only the affected memoised block for a value event without rendering GraphView', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: true, json: async () => ({ revision: 4, graph }) }) })
    client.setDocId('render-isolation')
    labValueStore.upsertRun('render-isolation', { state: 'running', doc_id: 'render-isolation', graph_revision: 4 })
    const renderProbe = vi.fn()
    const view = render(<GraphView client={client} provider={provider} initialDocument={graph} renderProbe={renderProbe} />)
    try {
      await waitFor(() => expect(screen.getByRole('button', { name: 'Run saved revision r4' })).toBeEnabled())
      await act(async () => {})
      renderProbe.mockClear()
      act(() => labValueStore.apply('render-isolation', { seq: 0, kind: 'value', block_id: 'count-1', payload: { result: { data: 81 } } }))
      expect(within(cardNamed('Count')).getByText('81')).toBeVisible()
      expect(renderProbe.mock.calls).toEqual([['block:count-1']])
    } finally {
      view.unmount()
      labValueStore.upsertRun('render-isolation', { state: 'finished' })
    }
  })

  it.each(['failed', 'suspended'])('F4 renders a first %s block event without a value', async kind => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: true, json: async () => ({ revision: 4, graph }) }) })
    const runId = `first-error-${kind}`
    client.setDocId(runId)
    labValueStore.upsertRun(runId, { state: 'running', doc_id: runId, graph_revision: 4 })
    const view = render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    try {
      await waitFor(() => expect(screen.getByRole('button', { name: 'Run saved revision r4' })).toBeEnabled())
      act(() => labValueStore.apply(runId, { seq: 0, kind, block_id: 'count-1', payload: { reason: 'worker-refused' } }))
      expect(within(cardNamed('Count')).getByText('error: worker-refused')).toBeVisible()
      expect(cardNamed('Count')).toHaveAttribute('data-value-kind', 'empty')
    } finally {
      view.unmount()
      labValueStore.upsertRun(runId, { state: 'finished' })
    }
  })

  it('F5 serialises concurrent clicks at 19 active runs and admits another after one finishes', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: true, json: async () => ({ revision: 4, graph }) }) })
    client.setDocId('concurrent-runs')
    labValueStore.getRuns().filter(run => run.id !== 'local').forEach(run => labValueStore.upsertRun(run.id, { state: 'finished' }))
    for (let n = 0; n < 19; n++) labValueStore.upsertRun(`concurrent-${n}`, { state: 'running' })
    let accept
    const fetchImpl = vi.fn((url, init) => init.method === 'POST'
      ? new Promise(resolve => { accept = resolve })
      : Promise.resolve({ ok: true, status: 200, body: new ReadableStream({ start() {} }) }))
    vi.stubGlobal('fetch', fetchImpl)
    const view = render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    try {
      const run = await screen.findByRole('button', { name: 'Run saved revision r4' })
      await waitFor(() => expect(run).toBeEnabled())
      act(() => { fireEvent.click(run); fireEvent.click(run) })
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
      expect(run).toBeDisabled()
      await act(async () => accept({ ok: true, status: 202, json: async () => ({ run_id: 'concurrent-19', state: 'queued' }) }))
      await waitFor(() => expect(run).toBeEnabled())
      expect(fetchImpl).toHaveBeenCalledTimes(2)
      act(() => labValueStore.upsertRun('concurrent-0', { state: 'finished' }))
      fireEvent.click(run)
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(3))
      await act(async () => accept({ ok: true, status: 202, json: async () => ({ run_id: 'concurrent-20', state: 'queued' }) }))
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(4))
      for (const id of ['concurrent-19', 'concurrent-20']) {
        expect(labValueStore.getRun(id)).toBeDefined()
        expect(fetchImpl.mock.calls.some(([url]) => url.includes(`runs=${id}&`))).toBe(true)
      }
    } finally {
      view.unmount()
      labValueStore.getRuns().filter(run => run.id.startsWith('concurrent-')).forEach(run => labValueStore.upsertRun(run.id, { state: 'finished' }))
    }
  })

  it('F5 refuses to submit at the hard cap and explains the active run limit', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: true, json: async () => ({ revision: 4, graph }) }) })
    client.setDocId('hard-cap')
    labValueStore.getRuns().filter(run => run.id !== 'local').forEach(run => labValueStore.upsertRun(run.id, { state: 'finished' }))
    for (let n = 0; n < 20; n++) labValueStore.upsertRun(`hard-cap-${n}`, { state: 'running' })
    const fetchImpl = vi.fn(); vi.stubGlobal('fetch', fetchImpl)
    const view = render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    try {
      const run = await screen.findByRole('button', { name: 'Run saved revision r4' })
      await waitFor(() => expect(run).toBeEnabled())
      fireEvent.click(run)
      expect(screen.getByText('Too many active runs (20). Wait for one to finish.')).toBeVisible()
      expect(fetchImpl).not.toHaveBeenCalled()
      expect(run).toBeEnabled()
      for (let n = 0; n < 20; n++) expect(labValueStore.getRun(`hard-cap-${n}`).state).toBe('running')
    } finally {
      view.unmount()
      for (let n = 0; n < 20; n++) labValueStore.upsertRun(`hard-cap-${n}`, { state: 'finished' })
    }
  })

  it.each(['button', 'focus', 'focus-race'])('resumes a 401-paused stream via %s with fresh authority and its cursor', async method => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const runId = `canvas-resume-${method}`
    const client = createLabArtifactsClient({ fetchImpl: vi.fn(async url => ({ ok: true,
      json: async () => url.endsWith('/head') ? { revision: 4 } : { revision: 4, graph } })) })
    client.setDocId(`resume-${method}`)
    const previousToken = localStorage.getItem('leaf.jwt')
    const originalToken = ['fixture', 'before', method].join('-'), renewedToken = ['fixture', 'after', method].join('-')
    localStorage.setItem('leaf.jwt', originalToken)
    labValueStore.apply(runId, { seq: 7, kind: 'value', block_id: 'count-1', payload: { result: { data: 73 } } })
    let resolveUnauthorized
    const fetchImpl = vi.fn().mockResolvedValueOnce({ ok: true, status: 202, json: async () => ({ run_id: runId, state: 'running' }) })
      .mockImplementationOnce(() => method === 'focus-race'
        ? new Promise(resolve => { resolveUnauthorized = resolve }) : Promise.resolve({ ok: false, status: 401 }))
      .mockResolvedValueOnce({ ok: true, status: 200, body: new ReadableStream({ start() {} }) })
    vi.stubGlobal('fetch', fetchImpl)
    const register = vi.spyOn(labValueStore, 'setStreamControl')
    const view = render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    try {
      const run = await screen.findByRole('button', { name: 'Run saved revision r4' })
      await waitFor(() => expect(run).toBeEnabled())
      fireEvent.click(run)
      if (method === 'focus-race') {
        await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2))
        localStorage.setItem('leaf.jwt', renewedToken)
        await act(async () => resolveUnauthorized({ ok: false, status: 401 }))
      }
      expect(await screen.findByText('Sign in again, then resume')).toBeVisible()
      expect(register).toHaveBeenCalledWith(runId, expect.objectContaining({ retry: expect.any(Function), stop: expect.any(Function) }))
      const control = register.mock.calls.find(([id]) => id === runId)[1]
      const retry = vi.spyOn(control, 'retry')
      if (method !== 'focus-race') {
        localStorage.setItem('leaf.jwt', originalToken)
        fireEvent(window, new Event('focus'))
        expect(retry).not.toHaveBeenCalled()
      } else expect(localStorage.getItem('leaf.jwt')).toBe(renewedToken)
      expect(fetchImpl).toHaveBeenCalledTimes(2)
      localStorage.setItem('leaf.jwt', renewedToken)
      if (method === 'button') fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
      else {
        fireEvent(window, new Event('focus'))
        fireEvent(window, new Event('focus'))
      }
      await waitFor(() => expect(labValueStore.getRun(runId).transport.status).toBe('connected'))
      expect(retry).toHaveBeenCalledTimes(1)
      expect(fetchImpl).toHaveBeenCalledTimes(3)
      expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBe(['Bearer', originalToken].join(' '))
      expect(fetchImpl.mock.calls[2][1].headers.Authorization).toBe(['Bearer', renewedToken].join(' '))
      expect(fetchImpl.mock.calls[2][1].headers['Last-Event-ID']).toBe(`${runId}:7`)
      expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
      view.unmount()
      labValueStore.setTransport(runId, { status: 'auth_expired' })
      expect(labValueStore.resume(runId)).toBe(false)
      fireEvent(window, new Event('focus'))
      expect(retry).toHaveBeenCalledTimes(1)
    } finally {
      view.unmount()
      labValueStore.upsertRun(runId, { state: 'finished' })
      if (previousToken === null) localStorage.removeItem('leaf.jwt')
      else localStorage.setItem('leaf.jwt', previousToken)
    }
  })

  it('keeps Run disabled until a saved head has loaded', async () => {
    let resolveHead
    const client = createLabArtifactsClient({ fetchImpl: () => new Promise(resolve => { resolveHead = resolve }) })
    const provider = hostProvider()
    render(<GraphView client={client} provider={provider} initialDocument={graphOf(provider, ['host-source', 'count'])} />)
    await waitFor(() => expect(cardNamed('Count')).toBeVisible())
    expect(screen.getByRole('button', { name: 'Run graph' })).toBeDisabled()
    await act(async () => resolveHead({ ok: false, status: 404, json: async () => ({}) }))
    expect(screen.getByRole('button', { name: 'Run graph' })).toBeDisabled()
  })

  it('shows Save before running and submits nothing when the draft differs from the saved revision', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const saved = { ...graph, outputs: {} }
    const client = createLabArtifactsClient({ fetchImpl: vi.fn(async url => ({ ok: true,
      json: async () => url.endsWith('/head') ? { revision: 3 } : { revision: 3, graph: saved } })) })
    client.setDocId('run-different')
    const fetchImpl = vi.fn(); vi.stubGlobal('fetch', fetchImpl)
    render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    const run = await screen.findByRole('button', { name: 'Run saved revision r3' })
    await waitFor(() => expect(run).toBeEnabled())
    fireEvent.click(run)
    expect(await screen.findByText('Save before running')).toBeVisible()
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('submits the matching saved revision and streams its block values into LiveValues', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: vi.fn(async url => ({ ok: true,
      json: async () => url.endsWith('/head') ? { revision: 4 } : { revision: 4, graph } })) })
    client.setDocId('run-matching')
    const events = [
      { run_id: 'canvas-stream', seq: 0, kind: 'value', block_id: 'count-1', at: now, payload: { result: { data: 73, as_of: now } } },
      { run_id: 'canvas-stream', seq: 1, kind: 'finished', at: now, payload: {} },
    ].map(value => `event: run\ndata: ${JSON.stringify(value)}\n\n`).join('')
    const fetchImpl = vi.fn().mockResolvedValueOnce({ ok: true, status: 202, json: async () => ({ run_id: 'canvas-stream', state: 'queued' }) })
      .mockResolvedValueOnce({ ok: true, status: 200, body: new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode(events)); controller.close()
      } }) })
    vi.stubGlobal('fetch', fetchImpl)
    render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    const run = await screen.findByRole('button', { name: 'Run saved revision r4' })
    await waitFor(() => expect(run).toBeEnabled())
    fireEvent.click(run)
    await waitFor(() => expect(within(cardNamed('Count')).getByText('73')).toBeVisible())
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/runs`)
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ doc_id: 'run-matching', graph_revision: 4 })
    expect(labValueStore.getBlock('canvas-stream', 'count-1').value.result.data).toBe(73)
  })

  it('renders a worker refusal code from a 422', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: vi.fn(async url => ({ ok: true,
      json: async () => url.endsWith('/head') ? { revision: 5 } : { revision: 5, graph } })) })
    client.setDocId('run-refused')
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 422, json: async () => ({ detail: 'lab_graph_host_refused' }) })
    vi.stubGlobal('fetch', fetchImpl)
    render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    const run = await screen.findByRole('button', { name: 'Run saved revision r5' })
    await waitFor(() => expect(run).toBeEnabled())
    fireEvent.click(run)
    expect(await screen.findByText('lab_graph_host_refused')).toBeVisible()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('reads local block displays and budget measurements from the shared local run', async () => {
    const provider = hostProvider(), graph = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: vi.fn().mockResolvedValue({ ok: false, status: 404 }) })
    render(<GraphView client={client} provider={provider} initialDocument={graph} />)
    await waitFor(() => expect(within(cardNamed('Count')).getByText('1')).toBeVisible())
    const original = labValueStore.getBlock('local', 'count-1').value.result
    expect(labValueStore.getRun('local').timing.times.length).toBeGreaterThan(0)
    act(() => labValueStore.setLocal([{ ...original, display: 91 }], labValueStore.getRun('local').timing))
    await waitFor(() => expect(within(cardNamed('Count')).getByText('91')).toBeVisible())
  })

  it.each(['matching', 'other kind', 'other version', 'malformed'])('reads %s host Code evidence through the selected inspector', async scenario => {
    const provider = createHostProvider({ framed: true })
    const digest = 'a'.repeat(64)
    const checks = Object.fromEntries(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus']
      .map(name => [name, { status: 'pass' }]))
    checks.corpus.difference_count = 0
    checks.tests = { status: 'pass', passed: 12, total: 12 }
    checks.speed = { status: 'pass', p99_us: 0 }
    const identity = { kind: scenario === 'other kind' ? 'adjust-none' : 'count',
      version: scenario === 'other version' ? '2.0.0' : '1.0.0' }
    provider.announce([
      { source: 'block-checks', data: { ...identity, checks, digest,
        draftDigest: scenario === 'malformed' ? 'invalid' : digest } },
      { source: 'block-versions', data: { ...identity, versions: [
        { state: 'active', version: 'v2', restart_required: false },
        { state: 'pending', version: 'v3', restart_required: scenario === 'malformed' ? 'yes' : true },
      ] } },
    ])
    const entries = { ...registry, ...provider.entries() }
    const document = addBlock(emptyGraph(), entries['count@1.0.0'])
    const client = createLabArtifactsClient({ fetchImpl: vi.fn().mockResolvedValue({ ok: false, status: 404 }) })
    const view = render(<GraphView client={client} provider={provider} revision={1} initialDocument={document} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Select count-1' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Code' }))
    const code = within(screen.getByRole('region', { name: 'Block code' }))
    const checkRows = within(code.getByRole('region', { name: 'Checks before it can be used' })).getAllByRole('listitem')
    expect(checkRows).toHaveLength(6)
    for (const row of checkRows) expect(row).toHaveAttribute('data-status', ['matching', 'malformed'].includes(scenario) ? 'pass' : 'pending')
    expect(globalThis.document.body).not.toHaveTextContent('request-use')
    if (scenario === 'matching') {
      expect(code.getByText('0 differ')).toBeVisible()
      expect(code.getByText(/v2.*live/)).toBeVisible()
      expect(code.getByText(/v3.*pending restart/)).toBeVisible()
      expect(code.getByText('Takes effect at the next restart')).toBeVisible()
      fireEvent.click(code.getByRole('button', { name: 'Use as a Change' }))
      expect(code.getByText(/python .*request-use/)).toHaveTextContent('request-use --manifest ./studio-block-check.json')
      expect(code.getByText(/Evidence digest:/)).toHaveTextContent(digest)
      act(() => provider.announce([{ source: 'block-checks', data: { ...identity, checks, digest: 'b'.repeat(64), draftDigest: digest } }]))
      view.rerender(<GraphView client={client} provider={provider} revision={2} initialDocument={document} />)
      expect(code.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
      expect(globalThis.document.body).not.toHaveTextContent('request-use')
    } else {
      expect(code.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
      if (scenario === 'malformed') expect(code.getByText('0 differ')).toBeVisible()
      else expect(code.queryByText('0 differ')).not.toBeInTheDocument()
      expect(code.queryByText(/v3.*pending restart/)).not.toBeInTheDocument()
      if (scenario !== 'malformed') expect(code.getByText('No version history supplied by the host.')).toBeVisible()
    }
  })

  it('P2-e hydrates the selected document head before enabling its first Save', async () => {
    let resolveHead
    const head = new Promise(resolve => { resolveHead = resolve })
    const fetchImpl = vi.fn((url, init) => init.method === 'GET' ? head : Promise.resolve({ ok: true,
      json: async () => ({ revision: 8, graph: JSON.parse(init.body).graph }) }))
    const client = createLabArtifactsClient({ fetchImpl })
    client.setDocId('selected')
    const provider = createHostProvider({ framed: true })
    const document = graphOf(hostProvider(), ['host-source', 'count'])
    const view = render(<GraphView client={client} provider={provider} revision={1} initialDocument={document} />)
    await screen.findByRole('region', { name: 'Strategy palette' })
    const save = screen.getByRole('button', { name: 'Save', exact: true })
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(save).toBeDisabled()
    expect(save).not.toHaveAttribute('title', 'Loading head')
    act(() => provider.announce(rows))
    view.rerender(<GraphView client={client} provider={provider} revision={2} initialDocument={document} />)
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/docs/selected/head`)
    expect(save).toBeDisabled()
    expect(save).toHaveAttribute('title', 'Loading head')
    await act(async () => resolveHead({ ok: true, json: async () => ({ revision: 7 }) }))
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await waitFor(() => expect(client.state.head).toBe(8))
    expect(JSON.parse(fetchImpl.mock.calls.find(([url]) => url.endsWith('/save'))[1].body).expected_head).toBe(7)
  })

  it('uses the Studio base, tenant and current bearer token for each source request', async () => {
    const scheme = 'Bearer'
    const auth = token => [scheme, token].join(' ')
    const authHeaderName = ['Author', 'ization'].join('')
    const originalBase = config.apiBase
    const originalToken = localStorage.getItem('leaf.jwt')
    config.apiBase = 'https://studio.example.test'
    localStorage.setItem('leaf.jwt', 'first-token')
    const fetchImpl = vi.fn(async url => ({ ok: true, json: async () =>
      url.endsWith('/api/jobs') ? { jobs: [], as_of: now } : { families: [], as_of: now } }))
    vi.stubGlobal('fetch', fetchImpl)
    const provider = createHostProvider({ framed: false })
    const document = graphOf(provider, ['studio-jobs'])
    document.blocks.push(...graphOf(provider, ['studio-capabilities']).blocks)
    const view = render(<GraphView initialDocument={document} />)
    try {
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(3))
      for (const path of ['/api/jobs', '/api/capabilities']) {
        expect(fetchImpl).toHaveBeenCalledWith(`${config.apiBase}${path}`, expect.objectContaining({
          headers: expect.objectContaining({ accept: 'application/json', 'X-Tenant-Id': config.tenant, [authHeaderName]: auth('first-token') }),
        }))
      }
      await waitFor(() => expect(cardNamed('Jobs')).toBeVisible())
      localStorage.setItem('leaf.jwt', 'renewed-token')
      await act(async () => { globalThis.document.dispatchEvent(new Event('visibilitychange')) })
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(5))
      expect(fetchImpl.mock.calls.slice(3).every(([, init]) => init.headers[authHeaderName] === auth('renewed-token'))).toBe(true)
    } finally {
      view.unmount()
      config.apiBase = originalBase
      if (originalToken === null) localStorage.removeItem('leaf.jwt')
      else localStorage.setItem('leaf.jwt', originalToken)
    }
  })

  it('revalidates a restored draft after its source arrives without another canvas report', () => {
    const provider = createHostProvider({ framed: true })
    const document = graphOf(hostProvider(), ['host-source', 'count'])
    const { result, rerender } = renderHook(({ revision }) => useLiveGraph({ provider, revision, clock: fixedClock }),
      { initialProps: { revision: 1 } })
    const draft = { document, ready: false, problems: ['unknown block host-source@1.0.0'] }
    act(() => result.current.onChange(draft))
    expect(result.current.ready).toBe(false)
    expect(result.current.problems).toContain('unknown block host-source@1.0.0')
    act(() => provider.announce(rows))
    rerender({ revision: 2 })
    expect(result.current.draft).toBe(draft)
    expect(result.current.ready).toBe(true)
    expect(result.current.problems).toEqual([])
    render(<LiveValues values={result.current.values} provider={provider} />)
    expect(within(cardNamed('Count')).getByText('1')).toBeVisible()
  })

  it('observes supplied provider revisions when row values change with the same schema', async () => {
    const provider = hostProvider()
    const document = graphOf(provider, ['host-source', 'latest', 'field'])
    const view = render(<GraphView provider={provider} revision={1} initialDocument={document} />)
    await waitFor(() => expect(within(cardNamed('Field')).getByText('42')).toBeVisible())
    act(() => provider.announce([{ ...rows[0], data: { price: 73, marker: 'updated' } }]))
    view.rerender(<GraphView provider={provider} revision={2} initialDocument={document} />)
    await waitFor(() => expect(within(cardNamed('Field')).getByText('73')).toBeVisible())
    expect(within(cardNamed('Field')).queryByText('42')).toBeNull()
  })

  it('disposes local pollers when switching to a supplied provider', async () => {
    const dispose = vi.fn()
    const start = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(runtime, 'createStudioSources').mockReturnValue({ poll: vi.fn(() => ({ start, dispose })) })
    const document = graphOf(createHostProvider({ framed: false }), ['studio-jobs'])
    const view = render(<GraphView initialDocument={document} />)
    await waitFor(() => expect(start).toHaveBeenCalledOnce())
    view.rerender(<GraphView provider={hostProvider()} initialDocument={document} />)
    expect(dispose).toHaveBeenCalledOnce()
  })

  it('replaces a successful Studio value with a source refusal for primitive rows', async () => {
    let sourceCalls = 0
    const fetchImpl = vi.fn(async url => url.endsWith('/head')
      ? { ok: false, status: 404, json: async () => ({}) }
      : { ok: true, json: async () => ({ jobs: ++sourceCalls === 1 ? [{ a: 7 }] : [123], as_of: now }) })
    vi.stubGlobal('fetch', fetchImpl)
    render(<GraphView initialDocument={graphOf(createHostProvider({ framed: false }), ['studio-jobs'])} />)
    await waitFor(() => expect(within(cardNamed('Jobs')).getByText('7')).toBeVisible())
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    await waitFor(() => expect(within(cardNamed('Jobs')).getByText('error: source-refused')).toBeVisible())
    expect(within(cardNamed('Jobs')).queryByText('7')).toBeNull()
  })

  it('advances source age every second and clears the ticker on unmount', () => {
    vi.spyOn(Date, 'now').mockRestore()
    vi.useFakeTimers()
    vi.setSystemTime(now)
    const provider = hostProvider()
    const { result, unmount } = renderHook(() => useLiveGraph({ provider, clock: fixedClock }))
    act(() => result.current.onChange({ document: graphOf(provider, ['host-source']), ready: true, problems: [] }))
    const view = render(<LiveValues values={result.current.values} provider={provider} />)
    expect(within(cardNamed('Dashboard source')).getByText('as of 0s ago')).toBeVisible()
    const count = result.current.measurements.blocks['host-source-1'].count
    act(() => vi.advanceTimersByTime(1000))
    view.rerender(<LiveValues values={result.current.values} provider={provider} />)
    expect(within(cardNamed('Dashboard source')).getByText('as of 1s ago')).toBeVisible()
    expect(result.current.measurements.blocks['host-source-1'].count).toBe(count)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('renders boolean fields as value cards', async () => {
    const provider = createHostProvider({ framed: true })
    provider.announce([{ source: 'markets', as_of: now, data: { active: true } }])
    const document = graphOf(provider, ['host-source', 'latest', 'field'])
    document.blocks.at(-1).settings.field = 'active'
    render(<GraphView provider={provider} initialDocument={document} />)
    await waitFor(() => expect(within(cardNamed('Field')).getByText('true')).toBeVisible())
    expect(cardNamed('Field')).toHaveAttribute('data-value-kind', 'value')
  })

  it('offers Studio reads and transforms, four inspector tabs, and Save', async () => {
    render(<GraphView />)
    const palette = within(await screen.findByRole('region', { name: 'Strategy palette' }))
    for (const title of ['Jobs', 'Capabilities', 'Count', 'Latest', 'Field']) expect(palette.getByRole('button', { name: title })).toBeVisible()
    fireEvent.click(palette.getByRole('button', { name: 'Count' }))
    for (const name of ['Setup', 'Configure', 'Test', 'Code']) expect(screen.getByRole('tab', { name })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeVisible()
    expect(screen.getByText('Studio sources: jobs, capabilities')).toBeVisible()
  })

  it('remounts the canvas on a ledger restore and on taking or recovering edits', async () => {
    const provider = hostProvider()
    const mine = graphOf(provider, ['host-source', 'latest', 'field'])
    const theirs = graphOf(provider, ['host-source', 'count'])
    const client = createLabArtifactsClient({ fetchImpl: async () => ({ ok: true, json: async () => ({
      revision: 3, graph: theirs, sha256: 'a'.repeat(64), created_at: now / 1000,
    }) }) })
    render(<GraphView client={client} provider={provider} initialDocument={mine} />)
    await waitFor(() => expect(cardNamed('Field')).toBeVisible())
    const originalFlow = screen.getByTestId('flow')
    await act(async () => { await client.restore('strategy', 1, 2) })
    await waitFor(() => expect(cardNamed('Count')).toBeVisible())
    expect(screen.getByTestId('flow')).not.toBe(originalFlow)
    expect(screen.queryByTestId('field-1')).not.toBeInTheDocument()
    act(() => client.takeGraph(mine))
    await waitFor(() => expect(cardNamed('Field')).toBeVisible())
    act(() => client.takeGraph(theirs))
    await waitFor(() => expect(cardNamed('Count')).toBeVisible())
  })

  it('starts exactly one jobs poller on adding Jobs and disposes it on removal', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ jobs: [{ a: 1 }, { a: 2 }], as_of: now }) })
    vi.stubGlobal('fetch', fetchImpl)
    const removeListener = vi.spyOn(document, 'removeEventListener')
    const view = render(<GraphView />)
    const palette = within(await screen.findByRole('region', { name: 'Strategy palette' }))
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([`${config.apiBase}/api/lab/docs/strategy/head`])
    fireEvent.click(palette.getByRole('button', { name: 'Jobs' }))
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'Select studio-jobs-1' }))
    fireEvent.click(screen.getByRole('button', { name: 'Move studio-jobs-1' }))
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl.mock.calls[1][0]).toBe(`${config.apiBase}/api/jobs`)
    fireEvent.click(screen.getByRole('button', { name: 'Remove block' }))
    expect(removeListener).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    view.unmount()
  })

  it.each([[0, false], [60000, true]])('evaluates Jobs to Count with source age %s and latency', async (age, stale) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true,
      json: async () => ({ jobs: [{ a: 1 }, { a: 2 }], as_of: now - age }) }))
    const provider = createHostProvider({ framed: false })
    render(<GraphView initialDocument={graphOf(provider, ['studio-jobs', 'count'])} />)
    await waitFor(() => expect(within(cardNamed('Count')).getByText('2')).toBeVisible())
    expect(within(cardNamed('Count')).getByText(`as of ${age / 1000}s ago`)).toHaveAttribute('data-badge')
    expect(within(cardNamed('Count')).getByText(/p50 .* ms · p99 .* ms/)).toHaveAttribute('data-badge')
    expect(within(cardNamed('Count')).queryByText('stale') !== null).toBe(stale)
  })

  it('shows a failed source as an empty card without exposing response text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private response')))
    const document = graphOf(createHostProvider({ framed: false }), ['studio-jobs'])
    const createProvider = vi.spyOn(hostProviderModule, 'createHostProvider')
    render(<GraphView initialDocument={document} />)
    await waitFor(() => expect(within(cardNamed('Jobs')).getByText('error: source-failed')).toBeVisible())
    expect(cardNamed('Jobs')).toHaveAttribute('data-value-kind', 'empty')
    const provider = createProvider.mock.results[0].value
    expect(provider.sources(document)[document.blocks[0].id].error.code).toBe('source-failed')
    expect(screen.queryByText(/private response/)).toBeNull()
  })

  it('offers Dashboard source and evaluates its row count locally', async () => {
    const provider = hostProvider()
    render(<GraphView provider={provider} initialDocument={graphOf(provider, ['host-source', 'count'])} />)
    expect(await screen.findByRole('button', { name: 'Dashboard source' })).toBeVisible()
    await waitFor(() => expect(within(cardNamed('Count')).getByText('1')).toBeVisible())
    expect(screen.getByText('Dashboard sources: markets')).toBeVisible()
    expect(screen.queryByLabelText('Host source rows')).toBeNull()
  })

  it('renders latest price as a Value card retaining host provenance', async () => {
    const provider = hostProvider()
    render(<GraphView provider={provider} initialDocument={graphOf(provider, ['host-source', 'latest', 'field'])} />)
    await waitFor(() => expect(within(cardNamed('Field')).getByText('42')).toBeVisible())
    expect(cardNamed('Field')).toHaveAttribute('data-value-kind', 'value')
    expect(within(cardNamed('Field')).getByText('host data')).toHaveAttribute('data-badge')
    expect(cards().queryByText('CANARY-5e1d')).toBeNull()
  })

  it('renders numeric row results with an inline Chart and the last value', async () => {
    const provider = hostProvider()
    render(<GraphView provider={provider} initialDocument={graphOf(provider, ['host-source'])} />)
    await waitFor(() => expect(cardNamed('Dashboard source')).toHaveAttribute('data-value-kind', 'chart'))
    expect(cardNamed('Dashboard source').querySelector('svg')).toHaveAttribute('width', '240')
    expect(within(cardNamed('Dashboard source')).getByText('42')).toBeVisible()
  })

  it('lists the residency refusal and disables Run for a host to custom Strategy pipe', async () => {
    const provider = hostProvider()
    const onRun = vi.fn()
    render(<GraphView provider={provider} initialDocument={graphOf(provider, ['host-source', 'count', 'custom-strategy'])} onRun={onRun} />)
    expect(await screen.findByText(/host data cannot reach Strategy \(custom-strategy\)/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Run graph' })).toBeDisabled()
    expect(onRun).not.toHaveBeenCalled()
  })

  it('refuses a new host to custom Strategy pipe in every render of the same act flush', () => {
    const provider = hostProvider()
    const renders = []
    const { result } = renderHook(() => {
      const live = useLiveGraph({ provider, clock: fixedClock })
      renders.push(live)
      return live
    })
    act(() => result.current.onChange({
      document: graphOf(provider, ['host-source', 'count']), ready: true, problems: [],
    }))
    expect(result.current.ready).toBe(true)

    const draft = { document: graphOf(provider, ['host-source', 'count', 'custom-strategy']), ready: true, problems: [] }
    const firstRender = renders.length
    act(() => result.current.onChange(draft))

    const changedRenders = renders.slice(firstRender)
    expect(changedRenders.length).toBeGreaterThan(0)
    for (const live of changedRenders) {
      expect(live.draft).toBe(draft)
      expect(live.ready).toBe(false)
      expect(live.problems).toEqual([
        'host data cannot reach Strategy (custom-strategy): it does not run in the browser',
      ])
      expect(live.result).toBeNull()
    }
  })

  it('re-evaluates provider announcements, keeps measurements summarized, and skips invalid drafts', async () => {
    let announce
    let invalidate
    let latest
    function Harness() {
      const [revision, setRevision] = useState(0)
      const [provider] = useState(() => createHostProvider({ framed: true, onChange: () => setRevision(value => value + 1) }))
      const live = useLiveGraph({ provider, revision, now: () => now, clock: fixedClock })
      latest = live
      useEffect(() => {
        provider.announce(rows)
        live.onChange({ document: graphOf(provider, ['host-source', 'count']), ready: true, problems: [] })
        announce = next => provider.announce(next)
        invalidate = () => {
          const document = graphOf(provider, ['host-source', 'count'])
          document.pipes = []
          live.onChange({ document, ready: false, problems: ['Input required'] })
        }
      }, [provider, live.onChange])
      return <LiveValues values={live.values} provider={provider} />
    }
    render(<Harness />)
    await waitFor(() => expect(within(cardNamed('Count')).getByText('1')).toBeVisible())
    const count = latest.measurements.blocks['count-1'].count
    act(() => announce([...rows, ...rows]))
    await waitFor(() => expect(within(cardNamed('Count')).getByText('2')).toBeVisible())
    expect(latest.measurements.blocks['count-1'].count).toBeGreaterThan(count)
    expect(latest.upstream['count-1/result']).toBe(2)
    expect(JSON.stringify(latest.measurements)).not.toContain('CANARY-5e1d')
    act(() => invalidate())
    expect(latest.result).toBeNull()
    expect(latest.ready).toBe(false)
  })

  it('keeps canary row values out of drafts proposed and run through the embedded bridge', async () => {
    let callbacks
    const bridge = { start: vi.fn(async () => { callbacks.onAdmitted() }), stop: vi.fn(),
      proposal: vi.fn().mockResolvedValue(true), run: vi.fn().mockResolvedValue(true) }
    const createHostBridge = next => { callbacks = next; return bridge }
    render(<LabWorkbench route={{ view: 'graph', params: {} }} status={{ state: 'on' }} embedded createHostBridge={createHostBridge} />)
    await screen.findByRole('region', { name: 'Dependency canvas' })
    act(() => callbacks.onSource(rows))
    const provider = hostProvider()
    let restored
    act(() => { restored = callbacks.onRestore({ document: graphOf(provider, ['host-source', 'latest', 'field']) }) })
    await act(async () => { await restored })
    await waitFor(() => expect(within(cardNamed('Field')).getByText('42')).toBeVisible())
    const run = screen.getByRole('button', { name: 'Run graph' })
    expect(run).toBeEnabled()
    fireEvent.click(run)
    await waitFor(() => expect(bridge.run).toHaveBeenCalledOnce())
    const draft = bridge.proposal.mock.calls.at(-1)[0]
    expect(draft.document.blocks).toHaveLength(3)
    expect(JSON.stringify(draft)).not.toContain('42')
    expect(JSON.stringify(draft)).not.toContain('CANARY-5e1d')
    expect(JSON.stringify(draft)).not.toContain('host_tainted')
  })
})
