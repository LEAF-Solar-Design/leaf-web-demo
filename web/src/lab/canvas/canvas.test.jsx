import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LabCanvas from './LabCanvas.jsx'
import NodeCard from './NodeCard.jsx'
import { Measurement } from './LatencyPipe.jsx'
import { BLOCK_MIME } from './Palette.jsx'
import { contract, manifests } from './registry.js'
import { ELEMENT_FAMILIES, LANES } from './elementCatalog.js'
import { addBlock, connectPipe, emptyGraph } from './graphDoc.js'
import { dependencyLayout, dependencyRanks } from './dependencyLayout.js'

// Exercise our UI and callbacks independently of browser geometry. ELK remains real.
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: point => point, getNodes: () => [], fitView: () => {} }),
  ReactFlow: ({ nodes, edges, nodeTypes, onNodeClick, onNodesChange, children }) => <div data-testid="flow">
    {nodes.map(node => { const Card = nodeTypes[node.type]; return <div key={node.id} data-testid={node.id} data-position={JSON.stringify(node.position)}>
      <button onClick={() => onNodeClick(null, node)}>Select {node.id}</button>
      <button onClick={() => onNodesChange([{ type: 'position', id: node.id, position: { x: 777, y: 888 } }])}>Move {node.id}</button>
      <Card {...node} />
    </div> })}<span data-testid="pipe-count">{edges.length}</span>{children}
  </div>,
  Background: () => null, Controls: () => null, Handle: () => null,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))
beforeEach(() => vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const manifest = manifests.find(item => item.id === 'adjust-none')
const documentOf = count => Array.from({ length: count }).reduce(document => addBlock(document, manifest), emptyGraph())
const edge = (from, to) => ({ from: { node: `adjust-none-${from}`, port: 'result' }, to: { node: `adjust-none-${to}`, port: 'value' } })

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
})

describe('canvas authoring', () => {
  it('adds from the keyboard-accessible palette, focuses slash search and preserves positions until Relayout', async () => {
    const changed = vi.fn()
    render(<LabCanvas onChange={changed} />)
    const search = screen.getByRole('searchbox')
    fireEvent.keyDown(window, { key: '/' })
    expect(search).toHaveFocus()
    fireEvent.change(search, { target: { value: 'adjust-none' } })
    const button = screen.getByRole('button', { name: 'Strategy · adjust-none' })
    button.focus()
    // A native button dispatches click for Enter; test the resulting accessible action.
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByTestId('adjust-none-1')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Relayout' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Move adjust-none-1' }))
    expect(screen.getByTestId('adjust-none-1')).toHaveAttribute('data-position', JSON.stringify({ x: 777, y: 888 }))
    expect(changed.mock.calls.at(-1)[0].blocks[0]).not.toHaveProperty('position')
    fireEvent.click(screen.getByRole('button', { name: 'Relayout' }))
    await waitFor(() => expect(screen.getByTestId('adjust-none-1')).not.toHaveAttribute('data-position', JSON.stringify({ x: 777, y: 888 })))
    expect(screen.getByText(/Validation .* ms/)).toHaveClass('leaf-timing')
  })
  it('supports palette drag/drop and keeps Strategy limited to Signals and Rules', async () => {
    const { container } = render(<LabCanvas />)
    expect(container.querySelectorAll('[data-element-family]')).toHaveLength(12)
    const dataTransfer = { setData: vi.fn(), getData: vi.fn(() => 'adjust-none'), effectAllowed: '' }
    fireEvent.dragStart(screen.getByRole('button', { name: 'Strategy · adjust-none' }), { dataTransfer })
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
    const picker = screen.getByLabelText('Compatible upstream output')
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
    expect(changed.mock.calls.at(-1)[0].blocks[1].settings).toEqual({ max_tokens: 128 })
    fireEvent.click(screen.getByRole('tab', { name: 'Test' }))
    fireEvent.click(screen.getByRole('button', { name: 'Validate graph' }))
    expect(screen.getByText('Validation checks wiring and settings. Execution is unavailable here.')).toBeVisible()
    fireEvent.click(screen.getByRole('tab', { name: 'Code' }))
    expect(screen.getByRole('heading', { name: 'Code unavailable' })).toBeVisible()
  })
  it('shows missing required pipes, declared defaults and explicit measurement states', () => {
    const required = { ...manifest, inputs: { value: { ...manifest.inputs.value, required: true }, optional: { ...manifest.inputs.value, has_default: true, default: 7 } } }
    render(<NodeCard id="example" data={{ manifest: required, missing: ['value'] }} />)
    expect(screen.getByText('Not ready: pipe required for value')).toBeVisible()
    expect(screen.getByText(/default 7/)).toBeVisible()
    expect(screen.getByText(/Value not measured/)).toBeVisible()
    const { container } = render(<Measurement measurement={{ value: 3, count: 1, p50_us: 5, p99_us: 11 }} budget={{ p50: 5, p99: 10 }} />)
    expect(container.querySelector('[data-budget-state="over-budget"]')).toBeInTheDocument()
  })
})
