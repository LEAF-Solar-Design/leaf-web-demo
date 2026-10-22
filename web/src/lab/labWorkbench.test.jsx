import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LabWorkbench from './LabWorkbench.jsx'
import { LAB_VIEWS, registerLabViews } from './views.js'
import WidgetsView from './widgets/WidgetsView.jsx'
import glossary from './contract/sources/leaf-glossary.v1.json'
import { setLayoutEngineLoader } from './canvas/dependencyLayout.js'

// Do not resolve or evaluate the multi-megabyte ELK implementation in jsdom.
// The shell proves the lazy mount; one Node gate proves real ELK.
vi.mock('elkjs/lib/elk.bundled.js', () => ({ default: class {
  constructor() { throw new Error('Real ELK is forbidden under Vitest') }
} }))
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

beforeEach(() => {
  setLayoutEngineLoader(fakeEngine.load)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})

afterEach(async () => {
  cleanup()
  await act(async () => {})
  expect(fakeEngine.pendingCount()).toBe(0)
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
  it('lazily mounts the dependency editor in the graph view', async () => {
    render(<LabWorkbench route={route('graph')} status={on} />)
    expect(await screen.findByRole('status', { name: 'Loading dependency canvas' }, { timeout: 15000 })).toBeVisible()
    expect(await screen.findByRole('region', { name: 'Dependency canvas' }, { timeout: 15000 })).toBeVisible()
    expect(screen.getByRole('region', { name: 'Strategy palette' })).toBeVisible()
    expect(screen.getByRole('region', { name: 'Block inspector' })).toBeVisible()
    expect(within(screen.getByRole('navigation', { name: 'Lab views' })).getAllByRole('link')).toHaveLength(5)
  }, 20000)

  it('retains every view DOM instance through navigation, dormancy and hiding', () => {
    const { container, rerender } = render(<LabWorkbench route={route('graph')} status={on} />)
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
  })

  it('fetches only the status probe when status is not provided', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ ok: true, schema: 'leaf.lab-status.v1', rail: 'on', posture: 'local', tenant: 't1' }),
    }))
    vi.stubGlobal('fetch', fetchImpl)
    const { rerender } = render(<LabWorkbench route={route('graph')} />)
    await waitFor(() => expect(screen.getByRole('navigation', { name: 'Lab views' })).toBeVisible())
    rerender(<LabWorkbench route={route('history')} hidden />)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toMatch(/\/api\/lab\/status$/)
  })

  it('does not grant widget scope or forward authority from route params or extra props', () => {
    render(<LabWorkbench route={{ view: 'widgets', params: { scopeKey: 'untrusted', tenant: 'other' } }}
      status={on} scopeKey="untrusted" token="untrusted" />)
    expect(screen.getByText('Choose a project to create a text widget.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
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
