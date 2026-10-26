import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { authHeaders, config } from '../../api.js'
import GraphView, { setGraphCanvasLoader, setLabToolsLoader } from './GraphView.jsx'
import LabCanvas from './LabCanvas.jsx'
import Palette, { BLOCK_MIME } from './Palette.jsx'
import { manifests, registry, withTools } from './registry.js'
import { fetchLabTools, toolRegistry } from './toolNodes.js'
import { setLayoutEngineLoader } from './dependencyLayout.js'
import { createHostProvider } from '../host/hostProvider.js'

vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: point => point, getNodes: () => [], fitView: () => {} }),
  ReactFlow: ({ nodes, nodeTypes, children }) => <div data-testid="flow">
    {nodes.map(node => { const Card = nodeTypes[node.type]; return <Card key={node.id} {...node} /> })}{children}
  </div>,
  Background: () => null, Controls: () => null, Handle: () => null,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))

const toolManifest = { ...manifests[0], id: 'tenant-tool', title: 'Tenant tool', kind: 'builtin', effect: 'read',
  version: { semver: '2.3.0', code_sha256: 'a'.repeat(64) } }
const available = { id: toolManifest.id, version: toolManifest.version.semver, title: 'Run tenant tool',
  source: 'tenant', effect: 'read', available: true, reason: null, manifest: toolManifest }
const unavailable = { ...available, id: 'locked-tool', title: 'Locked tool', available: false,
  reason: 'authored execution not armed', manifest: { ...toolManifest, id: 'locked-tool' } }
const noManifest = { ...available, id: 'missing-tool', title: 'Missing tool', manifest: null }
const tools = [available, unavailable, noManifest]
const body = (items = tools, extra = {}) => ({ ok: true, schema: 'leaf.lab-tools.v1', truncated: false, tools: items, ...extra })
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data })
const loadedTools = async () => ({ state: 'on', tools, truncated: false })
function toggleTools(group, open) {
  group.open = open
  fireEvent(group, new Event('toggle'))
}
async function openTools() {
  const group = await screen.findByLabelText('Tools')
  toggleTools(group, true)
  await within(group).findByText('Tools run on the server: save the graph and use Run.')
  return group
}

afterEach(async () => {
  cleanup()
  await act(async () => {})
  setLabToolsLoader()
  setGraphCanvasLoader()
  setLayoutEngineLoader()
  vi.restoreAllMocks()
})

describe('Lab tools transport and registry', () => {
  it('loads validated tools with the Lab request headers', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(200, body()))
    await expect(fetchLabTools({ fetchImpl })).resolves.toEqual({ state: 'on', tools, truncated: false })
    expect(fetchImpl).toHaveBeenCalledWith(`${config.apiBase}/api/lab/tools`, expect.objectContaining({
      headers: { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() },
    }))
  })

  it.each([[404, 'off'], [401, 'signed-out'], [403, 'signed-out'], [503, 'unavailable'], [500, 'unavailable']])(
    'maps HTTP %s to %s', async (status, state) => {
      await expect(fetchLabTools({ fetchImpl: async () => response(status) }))
        .resolves.toEqual({ state, tools: [], truncated: false })
    })

  it('resolves fetch and JSON failures to unavailable', async () => {
    for (const fetchImpl of [async () => { throw new Error('offline') },
      async () => ({ ok: true, json: async () => { throw new Error('bad JSON') } })]) {
      await expect(fetchLabTools({ fetchImpl })).resolves.toEqual({ state: 'unavailable', tools: [], truncated: false })
    }
  })

  it.each([null, {}, body([], { ok: false }), body([], { schema: 'other' }),
    body([], { tools: null }), body([], { truncated: 'false' })])('rejects malformed envelopes %j', async data => {
    await expect(fetchLabTools({ fetchImpl: async () => response(200, data) }))
      .resolves.toEqual({ state: 'unavailable', tools: [], truncated: false })
  })

  it('drops invalid tool fields and manifest identity or version mismatches', async () => {
    const invalid = [null, {}, { ...available, id: '' }, { ...available, version: ' ' },
      { ...available, title: null }, { ...available, available: 1 }, { ...available, reason: undefined },
      { ...available, source: 42 }, { ...available, source: 'other' }, { ...available, effect: null }, { ...available, effect: '' },
      { ...available, manifest: [] }, { ...available, manifest: 'manifest' },
      { ...available, manifest: { ...toolManifest, id: 'other' } },
      { ...available, manifest: { ...toolManifest, version: { semver: '9.0.0' } } }]
    await expect(fetchLabTools({ fetchImpl: async () => response(200, body([...invalid, ...tools])) }))
      .resolves.toEqual({ state: 'on', tools, truncated: false })
    const duplicate = { ...available, version: '9.9.9', manifest: { ...toolManifest, version: { semver: '9.9.9', code_sha256: 'c'.repeat(64) } } }
    await expect(fetchLabTools({ fetchImpl: async () => response(200, body([...tools, duplicate])) }))
      .resolves.toEqual({ state: 'on', tools, truncated: false })
  })

  it('caps kept tools at 200 and retains the server truncation flag', async () => {
    const many = Array.from({ length: 201 }, (_, index) => ({ ...noManifest, id: `tool-${index}` }))
    await expect(fetchLabTools({ fetchImpl: async () => response(200, body(many)) }))
      .resolves.toEqual({ state: 'on', tools: many.slice(0, 200), truncated: true })
    await expect(fetchLabTools({ fetchImpl: async () => response(200, body([], { truncated: true })) }))
      .resolves.toEqual({ state: 'on', tools: [], truncated: true })
  })

  it('registers tools with manifests even when unavailable, with frozen base precedence', () => {
    const key = `${available.id}@${available.version}`
    const base = { [key]: manifests[0] }
    expect(toolRegistry(tools)).toEqual({ [key]: toolManifest, 'locked-tool@2.3.0': unavailable.manifest })
    expect(toolRegistry([{ ...available, manifest: { ...toolManifest, id: 'wrong' } }])).toEqual({})
    const merged = withTools(base, tools)
    expect(merged).not.toBe(base)
    expect(Object.isFrozen(merged)).toBe(true)
    expect(merged[key]).toBe(base[key])
    expect(merged['locked-tool@2.3.0']).toBe(unavailable.manifest)
    expect(base).toEqual({ [key]: manifests[0] })
  })
})

describe('Tools palette', () => {
  it('adds and drags available tools and disables tools with the server reason or no manifest', async () => {
    const onAdd = vi.fn()
    render(<Palette entries={withTools(registry, tools)} tools={tools} onAdd={onAdd} onLoadTools={loadedTools} />)
    const group = within(await openTools())
    expect(group.getByText('Tools run on the server: save the graph and use Run.')).toBeVisible()
    const button = group.getByRole('button', { name: available.title })
    expect(button).toHaveAttribute('draggable', 'true')
    fireEvent.click(button)
    expect(onAdd).toHaveBeenCalledWith(toolManifest)
    const dataTransfer = { setData: vi.fn() }
    fireEvent.dragStart(button, { dataTransfer })
    expect(dataTransfer.setData).toHaveBeenCalledWith(BLOCK_MIME, available.id)
    expect(dataTransfer.effectAllowed).toBe('copy')
    const locked = group.getByRole('button', { name: /Locked tool/ })
    expect(locked).toBeDisabled()
    expect(locked).toHaveAttribute('draggable', 'false')
    expect(group.getByText('Unavailable: authored execution not armed')).toBeVisible()
    expect(group.getByRole('button', { name: /Missing tool/ })).toBeDisabled()
    expect(group.getByText('Unavailable: no manifest')).toBeVisible()
    fireEvent.click(locked)
    expect(onAdd).toHaveBeenCalledTimes(1)
  })

  it('keeps tool manifests out of family and lane groups and filters tools by title and id', async () => {
    const { container } = render(<Palette entries={withTools(registry, tools)} tools={tools} onAdd={vi.fn()} onLoadTools={loadedTools} />)
    await openTools()
    expect(container.querySelector('[data-element-family="tenant-tool"]')).toBeNull()
    expect(screen.queryByRole('button', { name: toolManifest.title, exact: true })).toBeNull()
    expect(screen.getAllByRole('button', { name: available.title })).toHaveLength(1)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'RUN TENANT' } })
    expect(screen.getByRole('button', { name: available.title })).toBeVisible()
    expect(screen.queryByRole('button', { name: /Locked tool/ })).toBeNull()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'tenant-tool' } })
    expect(screen.getByRole('button', { name: available.title })).toBeVisible()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'no-match' } })
    expect(within(screen.getByLabelText('Tools')).getByText('No matching tools.')).toBeVisible()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lane' } })
    const group = screen.getByLabelText('Tools')
    expect(within(group).getByRole('button', { name: available.title })).toBeVisible()
    expect(within(screen.getByRole('region', { name: 'Strategies' })).queryByRole('button', { name: available.title })).toBeNull()
    expect(within(screen.getByRole('region', { name: 'Strategies' })).queryByRole('button', { name: toolManifest.title })).toBeNull()
    expect(group).toHaveAttribute('data-palette-tools')
  })

  it('never hides a built-in block behind a tool that reuses its id', async () => {
    const base = manifests[0]
    const colliding = { ...available, id: base.id, version: '9.9.9', title: 'Colliding tool',
      manifest: { ...toolManifest, id: base.id, version: { semver: '9.9.9', code_sha256: 'b'.repeat(64) } } }
    const entries = withTools(registry, [colliding])
    expect(entries[`${base.id}@9.9.9`]).toBeUndefined()
    const { container } = render(<Palette entries={entries} tools={[colliding]} onAdd={vi.fn()} onLoadTools={async () => ({ state: 'on', tools: [colliding], truncated: false })} />)
    await openTools()
    const visible = () => [...container.querySelectorAll('button')].filter(button => !button.closest('[data-palette-tools]'))
    expect(visible().length).toBe([...render(<Palette entries={registry} onAdd={vi.fn()} />).container.querySelectorAll('button')].length)
    expect(within(screen.getAllByLabelText('Tools')[0]).queryByRole('button', { name: 'Colliding tool' })).toBeNull()
  })

  it('omits Tools without a loader callback, even when tools are supplied', () => {
    render(<Palette entries={withTools(registry, tools)} tools={tools} onAdd={vi.fn()} />)
    expect(screen.queryByLabelText('Tools')).toBeNull()
    expect(screen.queryByRole('button', { name: available.title })).toBeNull()
  })

  it('starts collapsed in the lane view and shows loading until tools resolve', async () => {
    let resolve
    const loader = vi.fn(() => new Promise(done => { resolve = done }))
    render(<Palette entries={registry} tools={[]} onAdd={vi.fn()} onLoadTools={loader} />)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lane' } })
    const group = screen.getByLabelText('Tools')
    expect(group.open).toBe(false)
    expect(loader).not.toHaveBeenCalled()
    toggleTools(group, true)
    expect(within(group).getByText('Loading tools.')).toBeVisible()
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))
    await act(async () => resolve({ state: 'on', tools: [], truncated: false }))
    expect(within(group).getByText('No matching tools.')).toBeVisible()
  })
})

describe('GraphView tool loading', () => {
  beforeEach(() => {
    setGraphCanvasLoader(async () => ({ default: LabCanvas }))
    setLayoutEngineLoader(() => ({ layout: async graph => ({ ...graph, children: [] }), terminateWorker: vi.fn() }))
  })

  function client() {
    return {
      state: { restoredGraph: null, restoredKey: 0, head: null, headLoaded: true,
        lastSaved: null, saveTimes: [], published: null, conflict: null },
      subscribe: () => () => {}, currentDocId: () => 'tools', currentGeneration: () => 0,
      head: vi.fn().mockResolvedValue({ ok: true }), save: vi.fn().mockResolvedValue({ ok: true, revision: 1 }),
    }
  }

  it('loads only on first open, passes tools to the palette and saves the added tool with its exact registry pin', async () => {
    const loader = vi.fn().mockResolvedValue({ state: 'on', tools, truncated: false })
    const fetchImpl = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(404))
    setLabToolsLoader(loader)
    const artifacts = client()
    const provider = createHostProvider({ framed: false })
    const view = render(<GraphView client={artifacts} provider={provider} />)
    const collapsed = await screen.findByLabelText('Tools')
    expect(collapsed.open).toBe(false)
    expect(loader).not.toHaveBeenCalled()
    expect(fetchImpl).not.toHaveBeenCalled()
    const group = await openTools()
    toggleTools(group, false)
    toggleTools(group, true)
    await act(async () => {})
    expect(loader).toHaveBeenCalledTimes(1)
    fireEvent.click(within(group).getByRole('button', { name: available.title }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    expect(artifacts.save).toHaveBeenCalledWith('tools', expect.objectContaining({ blocks: [expect.objectContaining({
      registry: { id: available.id, version: available.version, code_sha256: toolManifest.version.code_sha256 },
    })] }), null)
    view.rerender(<GraphView client={artifacts} provider={provider} hidden />)
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('shows unavailable without logging when the loader rejects', async () => {
    const loader = vi.fn().mockRejectedValue(new Error('offline'))
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    setLabToolsLoader(loader)
    render(<GraphView client={client()} provider={createHostProvider({ framed: false })} />)
    const group = await screen.findByLabelText('Tools')
    expect(loader).not.toHaveBeenCalled()
    toggleTools(group, true)
    await within(group).findByText('Tools are not available here.')
    expect(loader).toHaveBeenCalledTimes(1)
    expect(within(group).queryAllByRole('button')).toHaveLength(0)
    expect(consoleError).not.toHaveBeenCalled()
  })

  it.each(['off', 'signed-out', 'unavailable'])('shows unavailable for a %s load', async state => {
    setLabToolsLoader(async () => ({ state, tools: [], truncated: false }))
    render(<GraphView client={client()} provider={createHostProvider({ framed: false })} />)
    const group = await screen.findByLabelText('Tools')
    toggleTools(group, true)
    expect(await within(group).findByText('Tools are not available here.')).toBeVisible()
    expect(within(group).queryAllByRole('button')).toHaveLength(0)
  })

  it('ignores a tool response received after unmount', async () => {
    let resolve
    setLabToolsLoader(() => new Promise(done => { resolve = done }))
    const view = render(<GraphView client={client()} provider={createHostProvider({ framed: false })} />)
    toggleTools(await screen.findByLabelText('Tools'), true)
    await waitFor(() => expect(resolve).toBeTypeOf('function'))
    view.unmount()
    await act(async () => resolve({ state: 'on', tools, truncated: false }))
    expect(view.container).toBeEmptyDOMElement()
  })
})
