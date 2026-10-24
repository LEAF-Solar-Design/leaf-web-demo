import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LabWorkbench from './LabWorkbench.jsx'
import { LAB_VIEWS, registerLabViews } from './views.js'
import WidgetsView from './widgets/WidgetsView.jsx'
import glossary from './contract/sources/leaf-glossary.v1.json'
import { setLayoutEngineLoader } from './canvas/dependencyLayout.js'
import { setGraphCanvasLoader } from './canvas/GraphView.jsx'
import { createWidgetDoc } from './widgets/useWidgetEditor.js'
import { createTextElement } from './widgets/widgetDoc.js'

// The shell proves the lazy mount; one Node gate proves real ELK.
const elkImport = vi.hoisted(() => vi.fn())
vi.mock('elkjs/lib/elk.bundled.js', () => {
  elkImport()
  throw new Error('Real ELK is forbidden under Vitest')
})
// Shell tests have no geometry. Keep React Flow's resize/zoom animation work
// out of jsdom rather than mounting it with an inert ResizeObserver.
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: point => point, getNodes: () => [], fitView: () => {} }),
  ReactFlow: ({ children }) => <div>{children}</div>,
  Background: () => null, Controls: () => null, Handle: () => null,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))
const fakeEngine = {
  pending: new Set(),
  pendingCount() { return this.pending.size },
  load() {
    let pending
    return {
      async layout(graph) {
        pending = {}
        fakeEngine.pending.add(pending)
        try {
          await Promise.resolve()
          return { ...graph, children: graph.children.map((node, index) => ({ ...node, x: index * 370, y: 0 })) }
        } finally { fakeEngine.pending.delete(pending) }
      },
      terminateWorker() { fakeEngine.pending.delete(pending) },
    }
  },
}
const canvasImport = vi.fn(() => import('./canvas/LabCanvas.jsx'))

beforeEach(() => {
  canvasImport.mockClear()
  setGraphCanvasLoader(canvasImport)
  setLayoutEngineLoader(fakeEngine.load)
})

afterEach(async () => {
  cleanup()
  await act(async () => {})
  expect(fakeEngine.pendingCount()).toBe(0)
  expect(elkImport).not.toHaveBeenCalled()
  setGraphCanvasLoader()
  setLayoutEngineLoader()
  vi.unstubAllGlobals()
})

const on = { state: 'on', posture: 'local' }
const route = (view) => ({ view, params: {} })

describe('Lab view registration', () => {
  it('registers the five views once in order, with frozen glossary labels', () => {
    const first = registerLabViews()
    const second = registerLabViews()
    expect(first).toBe(second)
    expect(first).toBe(LAB_VIEWS)
    expect(first.map(({ id }) => id)).toEqual(['graph', 'widgets', 'history', 'runs', 'deploys'])
    expect(new Set(first.map(({ component }) => component)).size).toBe(5)
    expect(first.find(({ id }) => id === 'widgets').component).toBe(WidgetsView)
    expect(first.map(({ label }) => label)).toEqual(['strategy', 'market', 'test', 'keeper', 'lane']
      .map((id) => glossary.terms.find((term) => term.id === id).plain))
    expect(Object.isFrozen(first)).toBe(true)
    expect(first.every(Object.isFrozen)).toBe(true)
  })
})

describe('LabWorkbench', () => {
  it.each([
    ['hidden', { route: route('graph'), status: on, hidden: true }],
    ['inactive', { route: route('widgets'), status: on }],
    ['dormant', { route: route('graph'), status: { state: 'off' } }],
  ])('starts no canvas import while %s and imports once on first reveal', async (_, props) => {
    const { container, rerender } = render(<LabWorkbench {...props} />)
    await act(async () => {})
    expect(container.querySelector('[data-graph-placeholder]')).toBeInTheDocument()
    expect(container.querySelector('[data-canvas-editor]')).toBeNull()
    expect(canvasImport).not.toHaveBeenCalled()

    rerender(<LabWorkbench route={route('graph')} status={on} />)
    expect(screen.getByRole('status', { name: 'Loading dependency canvas' })).toBeVisible()
    const editor = await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    expect(canvasImport).toHaveBeenCalledTimes(1)
    rerender(<LabWorkbench route={route('graph')} status={on} hidden />)
    expect(editor).not.toBeVisible()
    rerender(<LabWorkbench route={route('graph')} status={on} />)
    expect(screen.getByRole('region', { name: 'Dependency canvas' })).toBe(editor)
    expect(canvasImport).toHaveBeenCalledTimes(1)
  }, 20000)

  it('lazily mounts the dependency editor in the graph view', async () => {
    render(<LabWorkbench route={route('graph')} status={on} />)
    expect(screen.getByRole('status', { name: 'Loading dependency canvas' })).toBeVisible()
    expect(await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })).toBeVisible()
    expect(screen.getByRole('region', { name: 'Strategy palette' })).toBeVisible()
    expect(screen.getByRole('region', { name: 'Block inspector' })).toBeVisible()
    expect(within(screen.getByRole('navigation', { name: 'Lab views' })).getAllByRole('link')).toHaveLength(5)
  }, 20000)

  it('retains every view DOM instance through navigation, dormancy and hiding', async () => {
    const { container, rerender } = render(<LabWorkbench route={route('graph')} status={on} />)
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    const panels = [...container.querySelectorAll('[data-lab-view]')]
    expect(panels).toHaveLength(5)
    const editor = container.querySelector('.widgets-view')
    for (const active of LAB_VIEWS) {
      rerender(<LabWorkbench route={route(active.id)} status={on} />)
      expect([...container.querySelectorAll('[data-lab-view]')]).toEqual(panels)
      for (const panel of panels) {
        expect(panel.hidden).toBe(panel.dataset.labView !== active.id)
      }
      expect(container.querySelector('.widgets-view')).toBe(editor)
      expect(screen.getByRole('link', { name: active.label })).toHaveAttribute('aria-current', 'page')
    }
    rerender(<LabWorkbench route={route('widgets')} status={{ state: 'off' }} />)
    expect(panels.every((panel) => panel.hidden)).toBe(true)
    rerender(<LabWorkbench route={route('widgets')} status={on} hidden />)
    expect(container.querySelector('.lab-workbench')).toHaveAttribute('hidden')
    expect(panels.every((panel) => panel.hidden)).toBe(true)
    rerender(<LabWorkbench route={route('widgets')} status={on} />)
    expect(container.querySelector('.widgets-view')).toBe(editor)
    expect(editor).toBeVisible()
  })

  it('replaces the last valid graph with the latest not-ready draft and shows its problems', async () => {
    render(<LabWorkbench route={route('graph')} status={on} />)
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    const palette = within(screen.getByRole('region', { name: 'Strategy palette' }))
    const readiness = within(screen.getByRole('region', { name: 'Graph readiness' }))
    fireEvent.click(palette.getAllByRole('button', { name: 'Strategy' })[0])
    expect(readiness.getByRole('status')).toHaveTextContent('Ready')
    fireEvent.click(palette.getAllByRole('button', { name: 'Market' })[0])
    expect(readiness.getByRole('status')).toHaveTextContent('No valid graph')
    expect(readiness.getByRole('listitem')).not.toBeEmptyDOMElement()
    expect(readiness.getByRole('button', { name: 'Run graph' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('mean_fast_index (required)'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(readiness.getByRole('status')).toHaveTextContent('Ready')
    expect(readiness.queryByRole('list')).not.toBeInTheDocument()
  })

  it.each([
    ['off', 'Lab is turned off.'],
    ['auth-off', 'Lab sign-in service is unavailable.'],
    ['signed-out', 'Sign in to use Lab.'],
    ['unavailable', 'Lab is unavailable right now.'],
    ['unknown', 'Lab is unavailable right now.'],
  ])('renders a dormant message for %s without fetching', (state, message) => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    render(<LabWorkbench status={{ state }} />)
    expect(screen.getByText(message)).toBeVisible()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Strategy palette' })).not.toBeInTheDocument()
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(canvasImport).not.toHaveBeenCalled()
  })

  it('fetches the status probe and the head read, nothing else, when status is not provided', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ ok: true, schema: 'leaf.lab-status.v1', rail: 'on', posture: 'local', tenant: 't1' }),
    }))
    vi.stubGlobal('fetch', fetchImpl)
    const { rerender } = render(<LabWorkbench route={route('graph')} />)
    await waitFor(() => expect(screen.getByRole('navigation', { name: 'Lab views' })).toBeVisible())
    await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })
    await act(async () => {})
    const requestCount = fetchImpl.mock.calls.length
    expect(requestCount).toBeGreaterThanOrEqual(1)
    expect(requestCount).toBeLessThanOrEqual(2)
    expect(fetchImpl.mock.calls[0][0]).toMatch(/\/api\/lab\/status$/)
    if (requestCount === 2) expect(fetchImpl.mock.calls[1][0]).toMatch(/\/api\/lab\/docs\/strategy\/head$/)
    rerender(<LabWorkbench route={route('history')} hidden />)
    await act(async () => {})
    expect(fetchImpl).toHaveBeenCalledTimes(requestCount)
  })

  it('does not grant widget scope or forward authority from route params or extra props', () => {
    render(<LabWorkbench route={{ view: 'widgets', params: { scopeKey: 'untrusted', tenant: 'other' } }}
      status={on} scopeKey="untrusted" token="untrusted" />)
    expect(screen.getByText('Choose a project to create a text widget.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
  })

  it.each([
    [false, true, 'sample'],
    [true, false, 'sample'],
    [true, true, 'live'],
  ])('supplies widget host values only with embedding and a bridge (embedded: %s, bridge: %s)', async (embedded, connected, freshness) => {
    const doc = createWidgetDoc()
    doc.formats.la.els = [{ ...createTextElement('host'), min_renderer: '1.0.0', text: 'Example host',
      binding: { source: 'kashbot.board', field: 'openCount', min_renderer: '1.0.0', scale: 'identity', tone: 'ink' } }]
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => ({ kind: 'widget', graph: doc, revision: 1 }) }))
    vi.stubGlobal('fetch', fetchImpl)
    const stop = vi.fn()
    let callbacks
    const createHostBridge = vi.fn((next) => {
      callbacks = next
      return { start: () => next.onSource([{ source: 'board', data: { openCount: '7' }, as_of: Date.now() }]), stop }
    })
    const { unmount } = render(<LabWorkbench route={route('widgets')} status={on} embedded={embedded} createHostBridge={connected ? createHostBridge : undefined} />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Load' })) })
    const preview = screen.getByRole('region', { name: 'Live widget preview' })
    const element = preview.querySelector('[data-element-id="host"]')
    expect(element).toHaveAttribute('data-freshness', freshness)
    expect(element).toHaveTextContent(freshness === 'live' ? '7' : 'Example host')
    expect(createHostBridge).toHaveBeenCalledTimes(embedded && connected ? 1 : 0)
    if (embedded && connected) {
      act(() => callbacks.onSource([{ source: 'board', data: { openCount: '8' }, as_of: Date.now() }]))
      expect(element).toHaveTextContent('8')
      expect(element).toHaveAttribute('data-freshness', 'live')
    }
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    unmount()
    expect(stop).toHaveBeenCalledTimes(embedded && connected ? 1 : 0)
  })

  it('has no browser dialogs or authority prop spreading in the owned implementation', () => {
    for (const file of ['LabWorkbench.jsx', 'Placeholder.jsx', 'views.js', 'labRoute.js',
      'canvas/GraphView.jsx', 'widgets/WidgetsView.jsx', 'history/HistoryView.jsx',
      'runs/RunsView.jsx', 'deploys/DeploysView.jsx']) {
      // Resolve from the web workspace; jsdom can rewrite import.meta.url.
      const source = readFileSync(resolve(process.cwd(), 'src/lab', file), 'utf8')
      expect(source).not.toMatch(/window\s*\.\s*(prompt|confirm|alert)\s*\(/)
      expect(source).not.toMatch(/\{\s*\.\.\.(props|route\.params)\s*\}/)
    }
  })
})
