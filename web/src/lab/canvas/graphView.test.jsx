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
  it('uses the Studio base, tenant and current bearer token for each source request', async () => {
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
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2))
      for (const path of ['/api/jobs', '/api/capabilities']) {
        expect(fetchImpl).toHaveBeenCalledWith(`${config.apiBase}${path}`, expect.objectContaining({
          headers: expect.objectContaining({ accept: 'application/json', 'X-Tenant-Id': config.tenant, Authorization: 'Bearer first-token' }),
        }))
      }
      await waitFor(() => expect(cardNamed('Jobs')).toBeVisible())
      localStorage.setItem('leaf.jwt', 'renewed-token')
      await act(async () => { globalThis.document.dispatchEvent(new Event('visibilitychange')) })
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(4))
      expect(fetchImpl.mock.calls.slice(2).every(([, init]) => init.headers.Authorization === 'Bearer renewed-token')).toBe(true)
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
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ jobs: [{ a: 7 }], as_of: now }) })
      .mockResolvedValue({ ok: true, json: async () => ({ jobs: [123], as_of: now }) })
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

  it('offers Studio reads and transforms, four inspector tabs, and unavailable save/history', async () => {
    render(<GraphView />)
    const palette = within(await screen.findByRole('region', { name: 'Strategy palette' }))
    for (const title of ['Jobs', 'Capabilities', 'Count', 'Latest', 'Field']) expect(palette.getByRole('button', { name: title })).toBeVisible()
    fireEvent.click(palette.getByRole('button', { name: 'Count' }))
    for (const name of ['Setup', 'Configure', 'Test', 'Code']) expect(screen.getByRole('tab', { name })).toBeVisible()
    expect(screen.getByRole('note')).toHaveTextContent('Save and history: not yet available')
    expect(screen.getByText('Studio sources: jobs, capabilities')).toBeVisible()
  })

  it('starts exactly one jobs poller on adding Jobs and disposes it on removal', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ jobs: [{ a: 1 }, { a: 2 }], as_of: now }) })
    vi.stubGlobal('fetch', fetchImpl)
    const removeListener = vi.spyOn(document, 'removeEventListener')
    const view = render(<GraphView />)
    const palette = within(await screen.findByRole('region', { name: 'Strategy palette' }))
    expect(fetchImpl).not.toHaveBeenCalled()
    fireEvent.click(palette.getByRole('button', { name: 'Jobs' }))
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Select studio-jobs-1' }))
    fireEvent.click(screen.getByRole('button', { name: 'Move studio-jobs-1' }))
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/jobs`)
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
