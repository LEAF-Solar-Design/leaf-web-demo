// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./labRoute.js', async (original) => ({
  ...await original(), fetchLabStatus: vi.fn(),
}))
vi.mock('../auth.js', async (original) => ({
  ...await original(),
  subscribeTokenStored: vi.fn(() => () => {}),
  subscribeBeforeLogout: vi.fn(() => () => {}),
}))
vi.mock('../api.js', async (original) => ({
  ...await original(), subscribeUnauthorized: vi.fn(() => () => {}),
}))
vi.mock('../site/useSurfaceContract.js', async (original) => ({
  ...await original(),
  useSurfaceConfigOverlay: () => ({ lab: { authoring: true, chrome: { tab: true, rail: null } } }),
}))

// Ground tests exercise the lazy boundary without browser geometry or real ELK.
const elkImport = vi.hoisted(() => vi.fn())
vi.mock('elkjs/lib/elk.bundled.js', () => {
  elkImport()
  throw new Error('Real ELK is forbidden under Vitest')
})
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: point => point, getNodes: () => [], fitView: () => {} }),
  ReactFlow: ({ children }) => <div>{children}</div>,
  Background: () => null, Controls: () => null, Handle: () => null,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))

import { fetchLabStatus } from './labRoute.js'
import { setGraphCanvasLoader } from './canvas/GraphView.jsx'
import { setLayoutEngineLoader } from './canvas/dependencyLayout.js'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { subscribeUnauthorized } from '../api.js'
import { _resetLabSurfaceStatusForTests } from './useLabSurfaceStatus.js'
import LabGround from './LabGround.jsx'
import ProductSurfaceTabs from '../components/ProductSurfaceTabs.jsx'
import {
  DEFAULT_PRODUCT_SURFACE, normalizeProductSurface, productSurfaceFromSearch,
  productSurfaceStates, searchForProductSurface, surfaceContract, surfaceRailState,
} from '../site/productSurfaces.js'

const canvasImport = vi.fn(() => import('./canvas/LabCanvas.jsx'))
// The unmeasured ground stays visibility:hidden in jsdom. Await its mounted
// editor without pretending the ground has browser geometry.
const findCanvas = () => waitFor(() => {
  const editor = document.querySelector('[data-canvas-editor]')
  expect(editor).toBeInTheDocument()
  return editor
}, { timeout: 15000 })
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

let originalUrl
beforeEach(() => {
  originalUrl = window.location.href
  window.history.replaceState({}, '', '/app')
  localStorage.removeItem('leaf.jwt')
  _resetLabSurfaceStatusForTests()
  vi.clearAllMocks()
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
  _resetLabSurfaceStatusForTests()
  window.history.replaceState({}, '', originalUrl)
  localStorage.removeItem('leaf.jwt')
})

describe('Lab rail selection and tab visibility', () => {
  it.each([null, ...['off', 'auth-off', 'signed-out', 'unavailable'].map((state) => ({ state }))])(
    'hides a pending or dormant rail despite an authored tab and authoring overlay: %j', async (status) => {
      fetchLabStatus.mockReturnValue(status === null ? new Promise(() => {}) : Promise.resolve(status))
      render(<><ProductSurfaceTabs activeSurface="cad" states={productSurfaceStates()} onSelect={() => {}} mock={false} /><LabGround active /></>)
      await act(async () => {})
      expect(canvasImport).not.toHaveBeenCalled()
      expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
      const dormant = status?.state === 'off' ? 'Lab is turned off.'
        : status?.state === 'auth-off' ? 'Lab sign-in service is unavailable.'
          : status?.state === 'signed-out' ? 'Sign in to use Lab.' : 'Lab is unavailable right now.'
      expect(screen.getAllByRole('status', { hidden: true }).filter((el) => el.textContent === dormant)).toHaveLength(1)
      expect(surfaceRailState('lab', status)).toBe(false)
      expect(normalizeProductSurface('lab', status)).toBe(DEFAULT_PRODUCT_SURFACE)
      expect(productSurfaceFromSearch('?surface=lab', status)).toBe(DEFAULT_PRODUCT_SURFACE)
      expect(productSurfaceFromSearch('?surface=lab', status)).toBe(productSurfaceFromSearch('?surface=unknown'))
      expect(new URLSearchParams(searchForProductSurface('', 'lab')).get('surface')).toBe(DEFAULT_PRODUCT_SURFACE)
      expect(productSurfaceStates({ labStatus: status }).lab.state).toBe('hidden')
    },
  )

  it('shares the first probe, then inserts Lab after Ops and enables URL selection only when on', async () => {
    let resolve
    fetchLabStatus.mockReturnValue(new Promise((done) => { resolve = done }))
    const states = productSurfaceStates()
    render(<><ProductSurfaceTabs activeSurface="cad" states={states} onSelect={() => {}} mock={false} /><LabGround active /></>)
    expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
    expect(fetchLabStatus).toHaveBeenCalledTimes(1)
    await act(async () => { resolve({ state: 'on', posture: 'local' }) })
    await findCanvas()
    expect(screen.getAllByRole('tab').map((tab) => tab.dataset.surface))
      .toEqual(['browser', 'cad', 'solar', 'ios', 'ops', 'lab'])
    expect(screen.getByRole('tab', { name: 'Lab' })).toHaveTextContent('Ready')
    expect(normalizeProductSurface('lab')).toBe('lab')
    expect(productSurfaceFromSearch('?surface=lab')).toBe('lab')
    expect(new URLSearchParams(searchForProductSurface('', 'lab')).get('surface')).toBe('lab')
    expect(surfaceContract('lab').toolbar.ribbon).toBe(false)
    expect(surfaceContract('lab').authoring).toBe(true)
    expect(surfaceContract('lab').entitlements).toEqual([])
  }, 20000)

  it.each([subscribeBeforeLogout, subscribeUnauthorized])('hides immediately on sign-out and rejects an earlier authority probe: %s', async (subscribe) => {
    let resolveOld
    fetchLabStatus.mockResolvedValueOnce({ state: 'on', posture: 'local' })
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve }))
      .mockResolvedValueOnce({ state: 'on', posture: 'local' })
    render(<><ProductSurfaceTabs activeSurface="cad" states={productSurfaceStates()} onSelect={() => {}} mock={false} /><LabGround active /></>)
    await act(async () => {})
    await findCanvas()
    expect(screen.getByRole('tab', { name: 'Lab' })).toBeInTheDocument()
    expect(subscribe).toHaveBeenCalledTimes(1)
    const tokenStored = subscribeTokenStored.mock.calls[0][0]
    act(() => { localStorage.setItem('leaf.jwt', 'replacement'); tokenStored('replacement') })
    expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
    act(() => { subscribe.mock.calls[0][0]() })
    expect(normalizeProductSurface('lab')).toBe(DEFAULT_PRODUCT_SURFACE)
    await act(async () => { resolveOld({ state: 'on', posture: 'local' }) })
    expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
    expect(screen.getByText('Sign in to use Lab.')).toBeInTheDocument()
    act(() => { localStorage.setItem('leaf.jwt', 'next'); tokenStored('next') })
    await act(async () => {})
    expect(screen.getByRole('tab', { name: 'Lab' })).toBeInTheDocument()
    expect(fetchLabStatus).toHaveBeenCalledTimes(3)
  }, 20000)

  it('invalidates the rail when another tab clears the session', async () => {
    fetchLabStatus.mockResolvedValue({ state: 'on', posture: 'local' })
    render(<ProductSurfaceTabs activeSurface="cad" states={productSurfaceStates()} onSelect={() => {}} mock={false} />)
    await act(async () => {})
    expect(screen.getByRole('tab', { name: 'Lab' })).toBeInTheDocument()
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: null })) })
    expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
    expect(productSurfaceFromSearch('?surface=lab')).toBe(DEFAULT_PRODUCT_SURFACE)
  })
})

it('starts no canvas import while the ground is hidden and imports once on first reveal', async () => {
  fetchLabStatus.mockResolvedValue({ state: 'on', posture: 'local' })
  const { container, rerender } = render(<LabGround active={false} />)
  await act(async () => {})
  expect(container.querySelector('[data-graph-placeholder]')).toBeInTheDocument()
  expect(container.querySelector('[data-canvas-editor]')).toBeNull()
  expect(canvasImport).not.toHaveBeenCalled()

  rerender(<LabGround active />)
  expect(container.querySelector('[aria-label="Loading dependency canvas"]')).toBeInTheDocument()
  const editor = await findCanvas()
  expect(canvasImport).toHaveBeenCalledTimes(1)
  rerender(<LabGround active={false} />)
  expect(editor).not.toBeVisible()
  rerender(<LabGround active />)
  expect(container.querySelector('[data-canvas-editor]')).toBe(editor)
  expect(canvasImport).toHaveBeenCalledTimes(1)
}, 20000)

it('retains the workbench and all views while propagating hidden', async () => {
  fetchLabStatus.mockResolvedValue({ state: 'on', posture: 'local' })
  const { container, rerender } = render(<LabGround active />)
  await act(async () => {})
  await findCanvas()
  const workbench = container.querySelector('.lab-workbench')
  const views = [...container.querySelectorAll('[data-lab-view]')]
  expect(views).toHaveLength(6)
  expect(views[0]).not.toHaveAttribute('hidden')
  rerender(<LabGround active={false} />)
  expect(container.querySelector('.lab-workbench')).toBe(workbench)
  expect(workbench).toHaveAttribute('hidden')
  expect(views.every((view) => view.hidden)).toBe(true)
  rerender(<LabGround active />)
  expect(container.querySelector('.lab-workbench')).toBe(workbench)
  expect([...container.querySelectorAll('[data-lab-view]')]).toEqual(views)
  expect(fetchLabStatus).toHaveBeenCalledTimes(1)
}, 20000)
