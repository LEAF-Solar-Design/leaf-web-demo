import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LabCanvas from './LabCanvas.jsx'
import GraphView from './GraphView.jsx'
import { Measurement } from './LatencyPipe.jsx'
import { BLOCK_MIME } from './Palette.jsx'
import { contract, manifests, manifestFor, resolveManifest } from './registry.js'
import { ELEMENT_FAMILIES, LANES } from './elementCatalog.js'
import { addBlock, connectPipe, emptyGraph, inputValues, missingInputs, updateSettings, validateGraph } from './graphDoc.js'
import { dependencyLayout, dependencyRanks } from './dependencyLayout.js'
import * as layout from './dependencyLayout.js'

// Detect even an attempted import, not just construction of the real engine.
const elkImport = vi.hoisted(() => vi.fn())
vi.mock('elkjs/lib/elk.bundled.js', () => {
  elkImport()
  throw new Error('Real ELK is forbidden under Vitest')
})

// Exercise our UI and callbacks independently of browser geometry.
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: point => point, getNodes: () => [], fitView: () => {} }),
  ReactFlow: ({ nodes, edges, nodeTypes, onNodeClick, onNodesChange, onConnect, onEdgesChange, children }) => <div data-testid="flow">
    {nodes.length > 1 && <button onClick={() => onConnect({ source: nodes[0].id, sourceHandle: 'result', target: nodes[1].id, targetHandle: 'value' })}>Connect first pair</button>}
    {edges.length > 0 && <button onClick={() => onEdgesChange([{ type: 'remove', id: edges[0].id }])}>Delete first pipe</button>}
    {nodes.map(node => { const Card = nodeTypes[node.type]; return <div key={node.id} data-testid={node.id} data-position={JSON.stringify(node.position)}>
      <button onClick={() => onNodeClick(null, node)}>Select {node.id}</button>
      <button onClick={() => onNodesChange([{ type: 'position', id: node.id, position: { x: 777, y: 888 } }])}>Move {node.id}</button>
      <Card {...node} />
    </div> })}<span data-testid="pipe-count">{edges.length}</span>
    {edges.map(edge => <span key={edge.id} data-testid={`budget-${edge.id}`}>{JSON.stringify(edge.data.budget)}</span>)}{children}
  </div>,
  Background: () => null, Controls: () => null, Handle: props => <span data-testid="handle" aria-label={props['aria-label']} />,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  fakeEngine.load.mockClear()
  layout.setLayoutEngineLoader(fakeEngine.load)
})
afterEach(async () => {
  cleanup()
  await act(async () => {})
  expect(fakeEngine.pendingCount()).toBe(0)
  expect(elkImport).not.toHaveBeenCalled()
  layout.setLayoutEngineLoader()
  vi.unstubAllGlobals()
})
const manifest = manifests.find(item => item.id === 'adjust-none')
const documentOf = count => Array.from({ length: count }).reduce(document => addBlock(document, manifest), emptyGraph())
const edge = (from, to) => ({ from: { node: `adjust-none-${from}`, port: 'result' }, to: { node: `adjust-none-${to}`, port: 'value' } })
const entriesOf = (...items) => Object.fromEntries(items.map(item => [`${item.id}@${item.version.semver}`, item]))
const pendingLayouts = new Set()
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  pendingLayouts.add(promise)
  return { promise,
    resolve: value => { pendingLayouts.delete(promise); resolve(value) },
    reject: error => { pendingLayouts.delete(promise); reject(error) } }
}
const fakeEngine = {
  pendingCount: () => pendingLayouts.size,
  load: vi.fn(() => {
    let pending
    return {
      layout: vi.fn(graph => {
        pending = deferred()
        const job = pending
        // Deliberately reverse the response order. Positions must use block IDs.
        queueMicrotask(() => job.resolve({ ...graph,
          children: graph.children.map((node, index) => ({ ...node, x: index * 50, y: 0 })).reverse() }))
        return job.promise
      }),
      terminateWorker: vi.fn(() => pending?.reject(new DOMException('Layout cancelled', 'AbortError'))),
    }
  }),
}

// jsdom does not synthesize a button's native keyboard click. Drive the key
// sequence and the resulting activation so two add handlers cannot pass.
const pressEnter = button => {
  button.focus()
  fireEvent.keyDown(button, { key: 'Enter', code: 'Enter' })
  fireEvent.keyUp(button, { key: 'Enter', code: 'Enter' })
  fireEvent.click(button, { detail: 0 })
}

describe('registry and deterministic dependencies', () => {
  it('retains the trusted fixture manifests and exactly twelve families and nine Lanes', () => {
    const fixture = JSON.parse(readFileSync(resolve(process.cwd(), '../contract/fixtures/lab/blocks.json'), 'utf8'))
    expect(manifests).toEqual(fixture.manifests)
    manifests.forEach(item => expect(contract.block(item, item)).toBe(true))
    expect(ELEMENT_FAMILIES).toHaveLength(12)
    expect(LANES.map(lane => lane.title)).toEqual(['Processing', 'Ordering', 'Cash out', 'Strategies', 'Sizing', 'Admission', 'Observation', 'Capture', 'Lifecycle'])
  })
  it('aligns independent blocks, then ranks forks only from drawn pipes', async () => {
    const independent = documentOf(4)
    const first = await dependencyLayout(independent)
    expect(new Set(Object.values(first).map(point => point.y)).size).toBe(1)
    let fork = connectPipe(independent, edge(1, 2))
    fork = connectPipe(fork, edge(1, 3))
    const positions = await dependencyLayout(fork)
    expect(positions['adjust-none-1'].y).toBe(positions['adjust-none-4'].y)
    expect(positions['adjust-none-2'].y).toBe(positions['adjust-none-3'].y)
    expect(positions['adjust-none-2'].y).toBeGreaterThan(positions['adjust-none-1'].y)
    expect(await dependencyLayout(fork)).toEqual(positions)
    expect(await dependencyLayout({ ...fork, blocks: [...fork.blocks].reverse(), pipes: [...fork.pipes].reverse() })).toEqual(positions)
    expect(dependencyRanks(fork)['adjust-none-3']).toBe(1)
    expect(() => dependencyRanks({ ...fork, pipes: [...fork.pipes, edge(2, 1)] })).toThrow(/Cycles/)
  })
  it('passes only drawn pipes and measured dimensions to the injected engine', async () => {
    const document = connectPipe(documentOf(3), edge(1, 2))
    const positions = await dependencyLayout(document, { 'adjust-none-1': { width: 450, height: 200 } })
    const engine = fakeEngine.load.mock.results.at(-1).value
    const input = engine.layout.mock.calls[0][0]
    expect(input.edges).toEqual([{ id: 'adjust-none-1/result>adjust-none-2/value',
      sources: ['adjust-none-1'], targets: ['adjust-none-2'] }])
    expect(input.children).toEqual([
      { id: 'adjust-none-1', width: 450, height: 200 },
      { id: 'adjust-none-2', width: 310, height: 360 },
      { id: 'adjust-none-3', width: 310, height: 360 },
    ])
    expect(positions).toEqual({
      'adjust-none-1': { x: 30, y: 30 },
      'adjust-none-3': { x: 540, y: 30 },
      'adjust-none-2': { x: 50, y: 490 },
    })
    expect(engine.terminateWorker).toHaveBeenCalledTimes(1)
  })
})

describe('canvas authoring', () => {
  it.each(['missing version', 'changed hash'])('renders and removes an unresolved block with a %s', async kind => {
    const initial = documentOf(1)
    const block = initial.blocks[0]
    if (kind === 'missing version') block.registry.version = '9.0.0'
    else block.registry.code_sha256 = '1'.repeat(64)
    const entries = entriesOf(manifest)
    const problem = kind === 'missing version' ? `unknown block ${manifest.id}@9.0.0` : `code changed for ${manifest.id}@1.0.0`
    const resolution = resolveManifest(block, entries)
    expect(resolution.problem).toBe(problem)
    expect(resolution.manifest).toEqual({ id: manifest.id, title: `${manifest.id}@${block.registry.version}`,
      lane: 'unresolved', effect: 'unresolved', inputs: {}, outputs: {}, settings_schema: null,
      latency_budget_us: null, version: { semver: block.registry.version, code_sha256: block.registry.code_sha256 } })
    expect(() => manifestFor(block, entries)).toThrow(problem)
    expect(missingInputs(initial, block, entries)).toEqual([])
    expect(inputValues(initial, block, {}, entries)).toEqual({})
    expect(() => updateSettings(initial, block.id, {}, entries)).toThrow(problem)
    const changed = vi.fn()
    render(<LabCanvas initialDocument={initial} entries={entries} onChange={changed} />)
    expect(changed).toHaveBeenCalledWith({ document: initial, ready: false, problems: [problem] })
    const card = within(screen.getByRole('article', { name: resolution.manifest.title }))
    expect(card.getByText('Unresolved')).toHaveClass('lane-unresolved')
    expect(card.getByText(problem)).toBeVisible()
    expect(card.queryAllByTestId('handle')).toHaveLength(0)
    await waitFor(() => expect(screen.getByText('Layout updated')).toBeVisible())
    fireEvent.click(screen.getByRole('button', { name: `Select ${block.id}` }))
    const inspector = within(screen.getByRole('region', { name: 'Block inspector' }))
    expect(inspector.getByRole('alert')).toHaveTextContent(problem)
    expect(inspector.getAllByRole('button').map(button => button.textContent)).toEqual(['Remove block'])
    expect(inspector.queryByRole('tablist')).not.toBeInTheDocument()
    expect(inspector.queryByRole('combobox')).not.toBeInTheDocument()
    fireEvent.click(inspector.getByRole('button', { name: 'Remove block' }))
    expect(changed.mock.calls.at(-1)[0]).toEqual({ document: emptyGraph(), ready: true, problems: [] })
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
  })

  it('lists every unresolved problem and still lays out and connects resolved blocks', async () => {
    const initial = documentOf(4)
    initial.blocks[2].registry.version = '9.0.0'
    initial.blocks[3].registry.code_sha256 = '1'.repeat(64)
    initial.pipes = [edge(1, 3), { ...edge(4, 2), to: { node: 'adjust-none-2', port: 'auxiliary' } }]
    const problems = [`unknown block ${manifest.id}@9.0.0`, `code changed for ${manifest.id}@1.0.0`]
    const entries = entriesOf(manifest)
    expect(validateGraph(initial, entries)).toMatchObject({ ready: false, problems })
    expect(() => connectPipe(initial, edge(1, 3), entries)).toThrow(problems[0])
    expect(() => connectPipe(initial, edge(4, 2), entries)).toThrow(problems[1])
    const changed = vi.fn()
    render(<LabCanvas initialDocument={initial} entries={entries} onChange={changed} />)
    await waitFor(() => expect(screen.getByText('Layout updated')).toBeVisible())
    expect(screen.getByTestId('budget-adjust-none-1/result>adjust-none-3/value')).toHaveTextContent('null')
    expect(screen.getByTestId('budget-adjust-none-4/result>adjust-none-2/auxiliary')).toHaveTextContent('null')
    fireEvent.click(screen.getByRole('button', { name: 'Select adjust-none-1' }))
    const inspector = within(screen.getByRole('region', { name: 'Block inspector' }))
    fireEvent.click(inspector.getByRole('tab', { name: 'Configure' }))
    fireEvent.change(inspector.getByRole('combobox', { name: 'Input' }), { target: { value: 'value' } })
    const sources = within(inspector.getByRole('combobox', { name: 'Source' })).getAllByRole('option')
    expect(sources.some(option => option.textContent.includes('adjust-none-3'))).toBe(false)
    expect(sources.some(option => option.textContent.includes('adjust-none-4'))).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    expect(changed.mock.calls.at(-1)[0]).toEqual({ document: { ...initial, pipes: [...initial.pipes, edge(1, 2)] }, ready: false, problems })
  })

  it('reports the initial document exactly once through rerenders and Strict Mode effect replay, then reports edits', async () => {
    const initial = documentOf(1)
    const changed = vi.fn()
    const view = render(<StrictMode><LabCanvas initialDocument={initial} onChange={changed} /></StrictMode>)
    expect(changed).toHaveBeenCalledTimes(1)
    expect(changed.mock.calls[0][0]).toEqual({ document: initial, ready: true, problems: [] })
    expect(changed.mock.calls[0][0].document).not.toBe(initial)
    await waitFor(() => expect(screen.getByText('Layout updated')).toBeVisible())
    const replacement = vi.fn()
    view.rerender(<StrictMode><LabCanvas initialDocument={documentOf(1)} entries={entriesOf(manifest)} onChange={replacement} /></StrictMode>)
    expect(changed).toHaveBeenCalledTimes(1)
    expect(replacement).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Strategy' }))
    expect(replacement).toHaveBeenCalledTimes(1)
    expect(replacement.mock.calls[0][0]).toEqual({ document: documentOf(2), ready: true, problems: [] })
  })

  it('enables the host Run for a valid loaded graph without editing', async () => {
    const initial = documentOf(1)
    const onRun = vi.fn()
    render(<GraphView initialDocument={initial} onRun={onRun} />)
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    const readiness = within(screen.getByRole('region', { name: 'Graph readiness' }))
    expect(readiness.getByRole('status')).toHaveTextContent('Ready')
    expect(readiness.queryByRole('list')).not.toBeInTheDocument()
    const run = readiness.getByRole('button', { name: 'Run graph' })
    expect(run).toBeEnabled()
    fireEvent.click(run)
    expect(onRun).toHaveBeenCalledTimes(1)
    expect(onRun).toHaveBeenCalledWith(initial)
  }, 20000)

  it('reports a loaded graph with invalid settings and shows its problems before editing', async () => {
    const required = manifests.find(item => item.id === 'level-fast-index')
    const initial = addBlock(emptyGraph(), required)
    const entries = entriesOf(required)
    const { problems } = validateGraph(initial, entries)
    expect(problems).not.toHaveLength(0)
    const onRun = vi.fn()
    render(<GraphView initialDocument={initial} entries={entries} onRun={onRun} />)
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    const readiness = within(screen.getByRole('region', { name: 'Graph readiness' }))
    expect(readiness.getByRole('status')).toHaveTextContent('No valid graph')
    expect(readiness.getAllByRole('listitem').map(item => item.textContent)).toEqual(problems)
    expect(readiness.getByRole('button', { name: 'Run graph' })).toBeDisabled()
    expect(onRun).not.toHaveBeenCalled()
  }, 20000)

  it('adds from the keyboard-accessible palette, focuses slash search and preserves positions until Relayout', async () => {
    const changed = vi.fn()
    const { container } = render(<LabCanvas entries={entriesOf(manifest)} onChange={changed} />)
    changed.mockClear() // Subsequent assertions count edits after the initial report.
    const search = screen.getByRole('searchbox')
    fireEvent.keyDown(container.querySelector('[data-canvas-editor]'), { key: '/' })
    expect(search).toHaveFocus()
    fireEvent.change(search, { target: { value: 'Strategy' } })
    vi.spyOn(screen.getByTestId('flow').parentElement, 'getBoundingClientRect').mockReturnValue({ left: 100, top: 60, width: 600, height: 400 })
    const button = screen.getByRole('button', { name: 'Strategy' })
    pressEnter(button)
    await waitFor(() => expect(screen.getByTestId('adjust-none-1')).toBeInTheDocument())
    expect(screen.getAllByRole('article')).toHaveLength(1)
    expect(screen.queryByTestId('adjust-none-2')).not.toBeInTheDocument()
    expect(changed).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('adjust-none-1')).toHaveAttribute('data-position', JSON.stringify({ x: 400, y: 260 }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Relayout' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Move adjust-none-1' }))
    expect(screen.getByTestId('adjust-none-1')).toHaveAttribute('data-position', JSON.stringify({ x: 777, y: 888 }))
    expect(changed.mock.calls.at(-1)[0].document.blocks[0]).not.toHaveProperty('position')
    fireEvent.click(screen.getByRole('button', { name: 'Relayout' }))
    await waitFor(() => expect(screen.getByTestId('adjust-none-1')).not.toHaveAttribute('data-position', JSON.stringify({ x: 777, y: 888 })))
    expect(screen.getByText(/Validation .* ms/)).toHaveClass('leaf-timing')
  })
  it('supports palette drag/drop and keeps Strategy limited to Signals and Rules', async () => {
    const { container } = render(<LabCanvas />)
    expect(container.querySelectorAll('[data-element-family]')).toHaveLength(12)
    const dataTransfer = { setData: vi.fn(), getData: vi.fn(() => 'adjust-none'), effectAllowed: '' }
    fireEvent.dragStart(within(container.querySelector('[data-element-family="adjust"]')).getAllByRole('button', { name: 'Strategy' })[0], { dataTransfer })
    expect(dataTransfer.setData).toHaveBeenCalledWith(BLOCK_MIME, 'adjust-none')
    fireEvent.drop(screen.getByTestId('flow').parentElement, { dataTransfer, clientX: 120, clientY: 90 })
    await waitFor(() => expect(screen.getByTestId('adjust-none-1')).toBeInTheDocument())
    const signals = screen.getByRole('region', { name: 'Signals' })
    const rules = screen.getByRole('region', { name: 'Rules' })
    expect(within(signals).queryByText(/Bet size/)).not.toBeInTheDocument()
    expect(within(rules).queryByText(/Cash out/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Group blocks'), { target: { value: 'lane' } })
    for (const lane of LANES) expect(screen.getByRole('region', { name: lane.title })).toBeInTheDocument()
  })
  it('opens all four tabs, validates settings JSON and wires compatible upstream data', async () => {
    const changed = vi.fn()
    render(<LabCanvas initialDocument={documentOf(2)} onChange={changed} />)
    fireEvent.click(screen.getByRole('button', { name: 'Select adjust-none-2' }))
    const inspector = screen.getByRole('region', { name: 'Block inspector' })
    for (const name of ['Setup', 'Configure', 'Test', 'Code']) expect(within(inspector).getByRole('tab', { name })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Configure' }))
    fireEvent.change(screen.getByLabelText('Input'), { target: { value: 'value' } })
    const picker = screen.getByLabelText('Source')
    expect(within(picker).queryByText('adjust-none-2 / result')).not.toBeInTheDocument()
    fireEvent.change(picker, { target: { value: JSON.stringify({ node: 'adjust-none-1', port: 'result' }) } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect pipe' }))
    expect(screen.getByTestId('pipe-count')).toHaveTextContent('1')
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect value' }))
    expect(screen.getByTestId('pipe-count')).toHaveTextContent('0')
    fireEvent.change(screen.getByLabelText('Settings JSON'), { target: { value: '{"api_token":"refused"}' } })
    const calls = changed.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings JSON' }))
    expect(changed).toHaveBeenCalledTimes(calls)
    fireEvent.change(screen.getByLabelText('Settings JSON'), { target: { value: '{"max_tokens":128}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings JSON' }))
    expect(changed.mock.calls.at(-1)[0].document.blocks[1].settings).toEqual({ max_tokens: 128 })
    fireEvent.click(screen.getByRole('tab', { name: 'Test' }))
    fireEvent.click(screen.getByRole('button', { name: 'Validate graph' }))
    expect(screen.getByText('Validation checks wiring and settings. Execution is unavailable here.')).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'Code' }))
    expect(screen.getByRole('heading', { name: 'Code unavailable' })).toBeVisible()
  })
  it('shows missing required pipes, declared defaults and explicit measurement states', () => {
    const required = { ...manifest, inputs: { value: { ...manifest.inputs.value, required: true }, optional: { ...manifest.inputs.value, has_default: true, default: 7 } } }
    render(<LabCanvas initialDocument={addBlock(emptyGraph(), required)} entries={entriesOf(required)} />)
    expect(screen.getByText('Not ready: pipe required for value')).toBeVisible()
    expect(screen.getByText(/default 7/)).toBeVisible()
    expect(screen.getByText(/Value not measured/)).toBeVisible()
    const { container } = render(<Measurement measurement={{ value: 3, count: 1, p50_us: 5, p99_us: 11 }} budget={{ p50: 5, p99: 10 }} />)
    expect(container.querySelector('[data-budget-state="over-budget"]')).toBeInTheDocument()
  })

  it.each(['pure', 'write'])('renders the manifest effect %s on the card', effect => {
    const entry = { ...manifest, effect }
    render(<LabCanvas initialDocument={addBlock(emptyGraph(), entry)} entries={entriesOf(entry)} />)
    const card = within(screen.getByRole('article', { name: entry.title }))
    expect(card.getByLabelText(`Effect: ${effect}`)).toHaveTextContent(effect)
    expect(card.getByText('Strategies')).toHaveClass('lab-lane-chip')
  })

  it('delivers missing required settings as not ready until the inspector supplies valid values', () => {
    const required = manifests.find(item => item.id === 'level-fast-index')
    const changed = vi.fn()
    render(<LabCanvas entries={entriesOf(required)} onChange={changed} />)
    changed.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Market' }))
    const card = within(screen.getByTestId('level-fast-index-1'))
    expect(card.getByRole('status')).toHaveTextContent(/Not ready/)
    expect(changed).toHaveBeenCalledTimes(1)
    const incomplete = changed.mock.calls[0][0]
    expect(incomplete.ready).toBe(false)
    expect(incomplete.problems).not.toHaveLength(0)
    expect(incomplete.document.blocks[0].settings).toEqual({})
    fireEvent.change(screen.getByLabelText('mean_fast_index (required)'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(card.getByRole('status')).toHaveTextContent('Ready')
    expect(changed).toHaveBeenCalledTimes(2)
    const { document, ready, problems } = changed.mock.calls[1][0]
    expect(ready).toBe(true)
    expect(problems).toEqual([])
    expect(document.blocks[0].settings).toEqual({ mean_fast_index: 1 })
    expect(contract.graph(document, entriesOf(required))).toBe(true)
  })

  it('fills declared settings defaults and emits a valid document immediately', () => {
    const defaults = { ...manifest, settings_schema: { type: 'object', additionalProperties: false,
      properties: { max_tokens: { type: 'integer', minimum: 0, maximum: 4096, default: 128 } }, required: ['max_tokens'] } }
    const changed = vi.fn()
    render(<LabCanvas entries={entriesOf(defaults)} onChange={changed} />)
    changed.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Strategy' }))
    expect(changed).toHaveBeenCalledTimes(1)
    const { document, ready, problems } = changed.mock.calls[0][0]
    expect(ready).toBe(true)
    expect(problems).toEqual([])
    expect(document.blocks[0].settings).toEqual({ max_tokens: 128 })
    expect(contract.graph(document, entriesOf(defaults))).toBe(true)
    expect(within(screen.getByTestId('adjust-none-1')).getByRole('status')).toHaveTextContent('Ready')
  })

  it('delivers settings defaults that violate their schema as not ready', () => {
    const invalid = { ...manifest, settings_schema: { type: 'object', additionalProperties: false,
      properties: { max_tokens: { type: 'integer', maximum: 4096, default: 5000 } }, required: ['max_tokens'] } }
    const changed = vi.fn()
    render(<LabCanvas entries={entriesOf(invalid)} onChange={changed} />)
    changed.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Strategy' }))
    expect(within(screen.getByTestId('adjust-none-1')).getByRole('status')).toHaveTextContent(/Not ready/)
    expect(changed).toHaveBeenCalledTimes(1)
    expect(changed.mock.calls[0][0]).toMatchObject({ ready: false,
      document: { blocks: [{ settings: { max_tokens: 5000 } }] } })
    expect(changed.mock.calls[0][0].problems).not.toHaveLength(0)
  })

  it('refuses incompatible connections through the actual onConnect handler', () => {
    const incompatible = { ...manifest, inputs: { ...manifest.inputs, value: { ...manifest.inputs.value, mode: 'stream' } } }
    const changed = vi.fn()
    render(<LabCanvas initialDocument={documentOf(2)} entries={entriesOf(incompatible)} onChange={changed} />)
    changed.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    expect(screen.getByText('Scalar and stream modes differ')).toBeVisible()
    expect(screen.getByTestId('pipe-count')).toHaveTextContent('0')
    expect(changed).not.toHaveBeenCalled()
  })

  it('connects and deletes a drawn pipe through ReactFlow callbacks', () => {
    const changed = vi.fn()
    render(<LabCanvas initialDocument={documentOf(2)} onChange={changed} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    expect(changed.mock.calls.at(-1)[0].document.pipes).toEqual([edge(1, 2)])
    fireEvent.click(screen.getByRole('button', { name: 'Delete first pipe' }))
    expect(changed.mock.calls.at(-1)[0].document.pipes).toEqual([])
  })

  it.each(['React Flow', 'inspector'])('delivers the latest not-ready draft after a required pipe is disconnected through %s', path => {
    const required = { ...manifest, id: 'required-strategy',
      inputs: { value: { ...manifest.inputs.value, required: true } } }
    const entries = entriesOf(manifest, required)
    const initial = addBlock(documentOf(1), required)
    const pipe = { from: { node: initial.blocks[0].id, port: 'result' }, to: { node: initial.blocks[1].id, port: 'value' } }
    const changed = vi.fn()
    render(<LabCanvas initialDocument={initial} entries={entries} onChange={changed} />)
    changed.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    const connected = changed.mock.calls[0][0]
    expect(connected).toEqual({ document: { ...initial, pipes: [pipe] }, ready: true, problems: [] })

    if (path === 'inspector') {
      fireEvent.click(screen.getByRole('button', { name: `Select ${initial.blocks[1].id}` }))
      fireEvent.click(screen.getByRole('tab', { name: 'Configure' }))
      fireEvent.click(screen.getByRole('button', { name: 'Disconnect value' }))
    } else fireEvent.click(screen.getByRole('button', { name: 'Delete first pipe' }))
    expect(changed).toHaveBeenCalledTimes(2)
    expect(changed.mock.calls[1][0]).toEqual({ document: initial, ready: false, problems: ['Test: input required'] })
    expect(connected.document.pipes).toEqual([pipe])

    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    expect(changed).toHaveBeenCalledTimes(3)
    expect(changed.mock.calls[2][0]).toEqual(connected)
    expect(contract.graph(changed.mock.calls[2][0].document, entries)).toBe(true)
  })

  it('revokes the host run document when a required pipe is disconnected and restores it on reconnect', async () => {
    const required = { ...manifest, id: 'required-strategy',
      inputs: { value: { ...manifest.inputs.value, required: true } } }
    const onRun = vi.fn()
    render(<GraphView initialDocument={addBlock(documentOf(1), required)} entries={entriesOf(manifest, required)} onRun={onRun} />)
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    const readiness = within(screen.getByRole('region', { name: 'Graph readiness' }))
    const run = readiness.getByRole('button', { name: 'Run graph' })
    expect(run).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    expect(readiness.getByRole('status')).toHaveTextContent('Ready')
    expect(run).toBeEnabled()
    fireEvent.click(run)
    expect(onRun).toHaveBeenCalledTimes(1)
    expect(onRun.mock.calls[0][0].pipes).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Delete first pipe' }))
    expect(readiness.getByRole('status')).toHaveTextContent('No valid graph')
    expect(readiness.getByRole('listitem')).toHaveTextContent('Test: input required')
    expect(run).toBeDisabled()
    fireEvent.click(run)
    expect(onRun).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Connect first pair' }))
    expect(run).toBeEnabled()
    expect(readiness.queryByRole('list')).not.toBeInTheDocument()
    fireEvent.click(run)
    expect(onRun).toHaveBeenCalledTimes(2)
  }, 20000)

  it('uses only the supplied registry for click, keyboard, slash search and drop', () => {
    const second = manifests.find(item => item.id === 'gates-price-v1')
    const changed = vi.fn()
    const { container } = render(<LabCanvas entries={entriesOf(manifest, second)} onChange={changed} />)
    const palette = screen.getByRole('region', { name: 'Strategy palette' })
    expect(within(palette).getAllByRole('button')).toHaveLength(2)
    expect(within(palette).queryByRole('button', { name: 'Market' })).not.toBeInTheDocument()
    fireEvent.click(within(palette).getByRole('button', { name: 'Strategy' }))
    const allowed = within(palette).getByRole('button', { name: 'Target price' })
    pressEnter(allowed)
    expect(screen.getByTestId('gates-price-v1-1')).toBeInTheDocument()
    fireEvent.keyDown(container.querySelector('[data-canvas-editor]'), { key: '/' })
    expect(screen.getByRole('searchbox')).toHaveFocus()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'Market' } })
    expect(within(palette).queryAllByRole('button')).toHaveLength(0)
    const calls = changed.mock.calls.length
    fireEvent.drop(screen.getByTestId('flow').parentElement, {
      dataTransfer: { getData: () => 'level-fast-index' }, clientX: 120, clientY: 90,
    })
    expect(screen.getByText('Block kind is not in this canvas registry')).toBeVisible()
    expect(screen.queryByTestId('level-fast-index-1')).not.toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(changed).toHaveBeenCalledTimes(calls)
  })

  it('scopes slash search to the canvas receiving the key', () => {
    const first = render(<LabCanvas />)
    const second = render(<LabCanvas />)
    const firstRoot = first.container.querySelector('[data-canvas-editor]')
    const secondRoot = second.container.querySelector('[data-canvas-editor]')
    const firstSearch = within(first.container).getByRole('searchbox')
    const secondSearch = within(second.container).getByRole('searchbox')
    fireEvent.keyDown(firstRoot, { key: '/' })
    expect(firstSearch).toHaveFocus()
    fireEvent.keyDown(secondRoot, { key: '/' })
    expect(secondSearch).toHaveFocus()
    fireEvent.keyDown(firstSearch, { key: '/' })
    expect(secondSearch).toHaveFocus()
    firstRoot.setAttribute('hidden', '')
    fireEvent.keyDown(firstRoot, { key: '/' })
    expect(secondSearch).toHaveFocus()
    fireEvent.keyDown(window, { key: '/' })
    expect(secondSearch).toHaveFocus()
  })

  it('mounts only on the first reveal and preserves the editor through hide and show', async () => {
    const runner = { run: vi.fn(async () => ({})), cancel: vi.fn() }
    vi.spyOn(layout, 'createDependencyLayoutRunner').mockReturnValue(runner)
    const view = render(<GraphView hidden />)
    expect(view.container.querySelector('[data-canvas-editor]')).toBeNull()
    expect(runner.run).not.toHaveBeenCalled()

    view.rerender(<GraphView />)
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    const editor = view.container.querySelector('[data-canvas-editor]')
    const search = within(editor).getByRole('searchbox')
    fireEvent.change(search, { target: { value: 'Market' } })
    await waitFor(() => expect(runner.run).toHaveBeenCalledTimes(1))

    view.rerender(<GraphView hidden />)
    expect(editor).not.toBeVisible()
    view.rerender(<GraphView />)
    expect(view.container.querySelector('[data-canvas-editor]')).toBe(editor)
    expect(search).toHaveValue('Market')
    expect(editor).toBeVisible()
    expect(runner.run).toHaveBeenCalledTimes(1)
    expect(runner.cancel).not.toHaveBeenCalled()
  }, 20000)

  it('keeps initial layout stable through prop rerenders and lays out the current draft explicitly', async () => {
    const runner = { run: vi.fn(async () => ({ 'adjust-none-1': { x: 30, y: 30 } })), cancel: vi.fn() }
    const createRunner = vi.spyOn(layout, 'createDependencyLayoutRunner').mockReturnValue(runner)
    const entries = entriesOf(manifest)
    const view = render(<LabCanvas initialDocument={documentOf(1)} entries={entries} />)
    await waitFor(() => expect(screen.getByText('Layout updated')).toBeVisible())
    view.rerender(<LabCanvas initialDocument={documentOf(1)} entries={entries} measurements={{}} upstream={{}} onChange={vi.fn()} />)
    expect(createRunner).toHaveBeenCalledTimes(1)
    expect(runner.run).toHaveBeenCalledTimes(1)
    expect(runner.cancel).not.toHaveBeenCalled()

    fireEvent.click(within(screen.getByRole('region', { name: 'Strategy palette' })).getByRole('button', { name: 'Strategy' }))
    expect(runner.run).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Relayout' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Relayout' })).not.toBeDisabled())
    expect(runner.run).toHaveBeenCalledTimes(2)
    expect(runner.run.mock.calls[1][0].blocks.map(block => block.id)).toEqual(['adjust-none-1', 'adjust-none-2'])
  })

  it('cancels pending layout on unmount and ignores its late resolution', async () => {
    const pending = deferred()
    const runner = { run: vi.fn(() => pending.promise), cancel: vi.fn() }
    vi.spyOn(layout, 'createDependencyLayoutRunner').mockReturnValue(runner)
    const changed = vi.fn()
    const view = render(<LabCanvas initialDocument={documentOf(1)} onChange={changed} />)
    changed.mockClear()
    expect(runner.run).toHaveBeenCalledTimes(1)
    view.unmount()
    expect(runner.cancel).toHaveBeenCalledTimes(1)
    await act(async () => pending.resolve({ 'adjust-none-1': { x: 999, y: 999 } }))
    expect(view.container).toBeEmptyDOMElement()
    expect(changed).not.toHaveBeenCalled()
  })

  it('ignores an older layout after editing and a newer request complete', async () => {
    const older = deferred()
    const newer = deferred()
    const runner = { run: vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise), cancel: vi.fn() }
    vi.spyOn(layout, 'createDependencyLayoutRunner').mockReturnValue(runner)
    render(<LabCanvas initialDocument={documentOf(1)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Move adjust-none-1' }))
    expect(runner.cancel).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Relayout' }))
    expect(runner.run).toHaveBeenCalledTimes(2)
    await act(async () => newer.resolve({ 'adjust-none-1': { x: 20, y: 40 } }))
    await act(async () => older.resolve({ 'adjust-none-1': { x: 999, y: 999 } }))
    expect(screen.getByTestId('adjust-none-1')).toHaveAttribute('data-position', JSON.stringify({ x: 20, y: 40 }))
    expect(screen.getByRole('button', { name: 'Relayout' })).not.toBeDisabled()
  })

  it('keeps a newer layout pending when the cancelled request rejects late', async () => {
    const older = deferred()
    const newer = deferred()
    const runner = { run: vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise), cancel: vi.fn() }
    vi.spyOn(layout, 'createDependencyLayoutRunner').mockReturnValue(runner)
    render(<LabCanvas initialDocument={documentOf(1)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Relayout' }))
    expect(runner.run).toHaveBeenCalledTimes(2)
    await act(async () => older.reject(new DOMException('Layout cancelled', 'AbortError')))
    expect(screen.queryByText('Layout cancelled')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Relayout' })).toBeDisabled()
    await act(async () => newer.resolve({ 'adjust-none-1': { x: 20, y: 40 } }))
    expect(screen.getByTestId('adjust-none-1')).toHaveAttribute('data-position', JSON.stringify({ x: 20, y: 40 }))
    expect(screen.getByRole('button', { name: 'Relayout' })).not.toBeDisabled()
  })
})

describe('layout runner cancellation', () => {
  it('rejects the default loader under Vitest before importing ELK', async () => {
    layout.setLayoutEngineLoader()
    await expect(dependencyLayout(documentOf(1))).rejects.toThrow('Inject a layout engine under Vitest')
    expect(elkImport).not.toHaveBeenCalled()
  })

  it('releases an engine loaded after cancellation without starting layout', async () => {
    const loading = deferred()
    const engine = { layout: vi.fn(), terminateWorker: vi.fn() }
    const runner = layout.createDependencyLayoutRunner(() => loading.promise)
    const result = runner.run(documentOf(1))
    const rejection = expect(result).rejects.toMatchObject({ name: 'AbortError' })
    runner.cancel()
    await rejection
    loading.resolve(engine)
    await loading.promise
    expect(engine.layout).not.toHaveBeenCalled()
    expect(engine.terminateWorker).toHaveBeenCalledTimes(1)
    runner.cancel()
    expect(engine.terminateWorker).toHaveBeenCalledTimes(1)
  })

  it('handles a cancelled loader rejecting after a newer layout completes', async () => {
    const loading = deferred()
    const runner = layout.createDependencyLayoutRunner(vi.fn()
      .mockReturnValueOnce(loading.promise).mockImplementationOnce(fakeEngine.load))
    const result = runner.run(documentOf(1))
    const rejection = expect(result).rejects.toMatchObject({ name: 'AbortError' })
    const newer = runner.run(documentOf(1))
    await rejection
    await expect(newer).resolves.toEqual({ 'adjust-none-1': { x: 30, y: 30 } })
    loading.reject(new Error('Old import failed'))
    await expect(loading.promise).rejects.toThrow('Old import failed')
    runner.cancel()
  })

  it('releases bundled workers without terminate on completion and cancellation', async () => {
    const pending = deferred()
    const completedWorker = { onmessage: vi.fn() }
    const cancelledWorker = { onmessage: vi.fn() }
    const completed = { worker: { worker: completedWorker },
      layout: async () => ({ children: [{ id: 'adjust-none-1', x: 30 }] }), terminateWorker: vi.fn() }
    const cancelled = { worker: { worker: cancelledWorker },
      layout: () => pending.promise, terminateWorker: vi.fn() }
    const runner = layout.createDependencyLayoutRunner(vi.fn().mockReturnValueOnce(completed).mockReturnValueOnce(cancelled))
    expect(await runner.run(documentOf(1))).toEqual({ 'adjust-none-1': { x: 30, y: 30 } })
    expect(completed.worker.worker).toBeNull()
    expect(completed.terminateWorker).not.toHaveBeenCalled()
    const result = runner.run(documentOf(1))
    const rejection = expect(result).rejects.toMatchObject({ name: 'AbortError' })
    runner.cancel()
    await rejection
    expect(cancelled.worker.worker).toBeNull()
    expect(cancelled.terminateWorker).not.toHaveBeenCalled()
    expect(() => cancelledWorker.onmessage({ data: {} })).not.toThrow()
    pending.resolve({ children: [{ id: 'adjust-none-1', x: 999 }] })
    await pending.promise
    runner.cancel()
  })

  it('terminates the worker on cancellation and on successful completion', async () => {
    const pending = deferred()
    const firstWorker = { layout: () => pending.promise, terminateWorker: vi.fn() }
    const secondWorker = { layout: async () => ({ children: [{ id: 'adjust-none-1', x: 30 }] }), terminateWorker: vi.fn() }
    const createElk = vi.fn().mockReturnValueOnce(firstWorker).mockReturnValueOnce(secondWorker)
    const runner = layout.createDependencyLayoutRunner(createElk)
    const first = runner.run(documentOf(1))
    const cancelled = expect(first).rejects.toMatchObject({ name: 'AbortError' })
    const second = runner.run(documentOf(1))
    await cancelled
    expect(firstWorker.terminateWorker).toHaveBeenCalledTimes(1)
    expect(await second).toEqual({ 'adjust-none-1': { x: 30, y: 30 } })
    expect(secondWorker.terminateWorker).toHaveBeenCalledTimes(1)
    pending.resolve({ children: [{ id: 'adjust-none-1', x: 999 }] })
    runner.cancel()
    expect(secondWorker.terminateWorker).toHaveBeenCalledTimes(1)
  })
})
