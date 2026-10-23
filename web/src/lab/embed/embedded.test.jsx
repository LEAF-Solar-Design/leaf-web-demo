import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const connections = vi.hoisted(() => [])
vi.mock('./hostBridge.js', () => ({
  createLabHostBridge: vi.fn((callbacks) => {
    const connection = {
      callbacks, start: vi.fn(() => { callbacks.onAdmitted(); return Promise.resolve(true) }),
      stop: vi.fn(), proposal: vi.fn().mockResolvedValue(true), run: vi.fn().mockResolvedValue(true),
      state: null,
    }
    connections.push(connection)
    return connection
  }),
}))
vi.mock('../useLabSurfaceStatus.js', () => ({ useLabSurfaceStatus: vi.fn() }))
vi.mock('../views.js', () => ({
  registerLabViews: () => ['graph', 'widgets', 'history', 'runs', 'deploys'].map((id) => ({
    id, label: id, path: id === 'graph' ? '/app/lab' : `/app/lab/${id}`,
    component: ({ hidden }) => <p hidden={hidden}>Standalone {id}</p>,
  })),
}))
vi.mock('../canvas/LabCanvas.jsx', async () => {
  const { useEffect } = await import('react')
  const empty = { schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: {} }
  return { default: function Canvas({ initialDocument = empty, onChange, entries = {} }) {
    useEffect(() => { onChange({ document: initialDocument, ready: true, problems: [] }) }, [initialDocument, onChange])
    return <div data-canvas-editor>
      <section aria-label="Block palette">{Object.values(entries).map(entry => <button key={entry.id}>{entry.title}</button>)}</section>
      <p aria-label="Canvas document">{JSON.stringify(initialDocument)}</p>
      <button onClick={() => onChange({ document: initialDocument, ready: true, problems: [] })}>Commit ready draft</button>
      <button onClick={() => onChange({ document: initialDocument, ready: false, problems: ['Input required'] })}>Commit incomplete draft</button>
      <section className="lab-inspector" aria-label="Block inspector"><h2>Block inspector</h2></section>
    </div>
  } }
})

import { useLabSurfaceStatus } from '../useLabSurfaceStatus.js'
import LabGround, { setLabHostBridgeLoader } from '../LabGround.jsx'
import LabWorkbench from '../LabWorkbench.jsx'
import { createLabHostBridge } from './hostBridge.js'
import ProductSurfaceTabs from '../../components/ProductSurfaceTabs.jsx'
import { productSurfaceStates } from '../../site/productSurfaces.js'

let originalUrl
const bridgeImport = vi.fn(() => import('./hostBridge.js'))
beforeEach(() => {
  originalUrl = window.location.href
  window.history.replaceState({}, '', '/app/lab?embed=1')
  connections.length = 0
  vi.clearAllMocks()
  setLabHostBridgeLoader(bridgeImport)
  useLabSurfaceStatus.mockReturnValue({ state: 'on', posture: 'local' })
})
afterEach(() => {
  cleanup()
  setLabHostBridgeLoader()
  vi.restoreAllMocks()
  window.history.replaceState({}, '', originalUrl)
})
const framed = () => vi.spyOn(window, 'self', 'get').mockReturnValue({})
const canvas = async () => {
  const button = await screen.findByRole('button', { name: 'Commit ready draft' })
  await waitFor(() => expect(connections).toHaveLength(1))
  return button
}

describe('Lab embedded mode', () => {
  it.each([[false, '1'], [true, '0'], [true, 'true']])('keeps the standalone shell for framed=%s and embed=%s', async (frame, embed) => {
    if (frame) framed()
    window.history.replaceState({}, '', `/app/lab?embed=${embed}`)
    render(<><ProductSurfaceTabs activeSurface="cad" states={productSurfaceStates()} onSelect={() => {}} /><LabGround active /></>)
    await act(async () => {})
    expect(screen.getByRole('navigation', { name: 'Product workspace' })).toBeVisible()
    expect(document.querySelector('[data-lab-embed-root]')).toBeNull()
    expect(connections).toHaveLength(0)
    expect(bridgeImport).not.toHaveBeenCalled()
  })

  it('fills the frame, hides every outer navigation and preserves embedded Lab view links', async () => {
    framed()
    render(<>
      <header><nav aria-label="Outer navigation"><a href="/app">Home</a></nav></header>
      <ProductSurfaceTabs activeSurface="cad" states={productSurfaceStates()} onSelect={() => {}} />
      <LabGround active />
    </>)
    await canvas()
    expect(screen.queryByRole('navigation', { name: 'Product workspace' })).toBeNull()
    expect(screen.getByRole('navigation', { name: 'Outer navigation', hidden: true })).not.toBeVisible()
    const root = document.querySelector('[data-lab-embed-root]')
    expect(root.parentNode).toBe(document.body)
    expect(root).toBeVisible()
    const links = within(screen.getByRole('navigation', { name: 'Lab views' })).getAllByRole('link')
    expect(links).toHaveLength(5)
    expect(links.every((link) => new URL(link.href).searchParams.get('embed') === '1')).toBe(true)
    expect(connections).toHaveLength(1)
    expect(connections[0].start).toHaveBeenCalledOnce()
    expect(bridgeImport).toHaveBeenCalledOnce()
  })

  it.each([
    ['off', 'Lab is turned off.'], ['signed-out', 'Sign in to use Lab.'],
    ['auth-off', 'Lab sign-in service is unavailable.'], ['unavailable', 'Lab is unavailable right now.'],
  ])('keeps %s inside the frame and creates no bridge', async (state, message) => {
    framed()
    useLabSurfaceStatus.mockReturnValue({ state })
    render(<LabGround active />)
    await act(async () => {})
    expect(screen.getByRole('status')).toHaveTextContent(message)
    expect(screen.getByRole('status')).toBeVisible()
    expect(connections).toHaveLength(0)
    expect(bridgeImport).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Run graph' })).toBeNull()
  })

  it('defers the bridge import until the embedded ground is active', async () => {
    framed()
    const { rerender } = render(<LabGround active={false} />)
    await act(async () => {})
    expect(bridgeImport).not.toHaveBeenCalled()
    rerender(<LabGround active />)
    await canvas()
    expect(bridgeImport).toHaveBeenCalledOnce()
  })

  it('does not start a bridge if the rail closes while its module is loading', async () => {
    framed()
    let loaded
    bridgeImport.mockImplementationOnce(() => new Promise((resolve) => { loaded = resolve }))
    const { rerender } = render(<LabGround active />)
    expect(bridgeImport).toHaveBeenCalledOnce()
    useLabSurfaceStatus.mockReturnValue({ state: 'signed-out' })
    rerender(<LabGround active />)
    await act(async () => { loaded({ createLabHostBridge }) })
    expect(connections).toHaveLength(0)
    expect(screen.getByText('Sign in to use Lab.')).toBeVisible()
  })

  it('posts every committed draft, disables incomplete Run and hands ready Run to the host', async () => {
    framed()
    render(<LabGround active />)
    await canvas()
    const connection = connections[0]
    expect(connection.proposal).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Commit incomplete draft' }))
    expect(connection.proposal).toHaveBeenLastCalledWith(expect.objectContaining({ ready: false, problems: ['Input required'] }), expect.any(Number))
    expect(screen.getByRole('button', { name: 'Run graph' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Commit ready draft' }))
    expect(connection.proposal).toHaveBeenLastCalledWith(expect.objectContaining({ ready: true, problems: [] }), expect.any(Number))
    fireEvent.click(screen.getByRole('button', { name: 'Run graph' }))
    await waitFor(() => expect(connection.run).toHaveBeenCalledOnce())
  })

  it('shows the host wire-limit problem and keeps Run disabled until a later proposal fits', async () => {
    framed()
    render(<LabGround active />)
    await canvas()
    const connection = connections[0]
    const run = screen.getByRole('button', { name: 'Run graph' })
    expect(run).toBeEnabled()
    expect(screen.queryByRole('status', { name: 'Host proposal' })).toBeNull()

    act(() => {
      connection.state = { status: 'too_large', bytes: 4097 }
      connection.callbacks.onChange({ ready: false, problems: ['proposal exceeds the 4096-byte wire limit (4097 bytes)'] })
    })
    expect(screen.getByRole('status', { name: 'Host proposal' }))
      .toHaveTextContent('proposal exceeds the 4096-byte wire limit (4097 bytes)')
    expect(run).toBeDisabled()
    fireEvent.click(run)
    expect(connection.run).not.toHaveBeenCalled()

    act(() => { connection.callbacks.onChange({ ready: true, problems: [] }) })
    expect(run).toBeDisabled()
    act(() => {
      connection.state = null
      connection.callbacks.onChange({ ready: true, problems: [] })
    })
    expect(screen.getByRole('status', { name: 'Host proposal' })).toHaveTextContent('Proposal accepted by the host')
    expect(screen.queryByText('proposal exceeds the 4096-byte wire limit (4097 bytes)')).toBeNull()
    expect(run).toBeEnabled()
    fireEvent.click(run)
    await waitFor(() => expect(connection.run).toHaveBeenCalledOnce())
  })

  it('renders no host proposal status in standalone mode', async () => {
    render(<LabWorkbench route={{ view: 'graph', params: {} }} status={{ state: 'on' }} />)
    await act(async () => {})
    expect(screen.queryByRole('status', { name: 'Host proposal' })).toBeNull()
    expect(connections).toHaveLength(0)
  })

  it('applies a restored document before resolving and offers the Dashboard source without raw rows', async () => {
    framed()
    render(<LabGround active />)
    await canvas()
    const connection = connections[0]
    const document = { schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: { restored: { node: 'strategy-1', port: 'result' } } }
    let restored
    act(() => { restored = connection.callbacks.onRestore({ document, ready: true, problems: [], restore: true }) })
    await act(async () => { await restored })
    expect(screen.getByLabelText('Canvas document')).toHaveTextContent(JSON.stringify(document))
    expect(connection.proposal).not.toHaveBeenCalled()
    act(() => { connection.callbacks.onSource([{ source: 'markets', as_of: Date.now(), data: { price: 42 } }]) })
    expect(screen.getByText('Dashboard sources: markets')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Dashboard source' })).toBeVisible()
    expect(screen.queryByLabelText('Host source rows')).toBeNull()
  })

  it('shows source reload and tears the bridge down when the rail closes', async () => {
    const route = { view: 'graph', params: { embed: '1' } }
    const { rerender } = render(<LabWorkbench route={route} status={{ state: 'on' }} embedded createHostBridge={createLabHostBridge} />)
    await canvas()
    const connection = connections[0]
    act(() => { connection.callbacks.onReload() })
    expect(screen.getByText('Studio Lab reloaded by the dashboard')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Run graph' })).toBeDisabled()
    rerender(<LabWorkbench route={route} status={{ state: 'signed-out' }} embedded createHostBridge={createLabHostBridge} />)
    expect(connection.stop).toHaveBeenCalledOnce()
    expect(screen.getByText('Sign in to use Lab.')).toBeVisible()
  })

  it('applies a restore even when the graph view has never been revealed', async () => {
    framed()
    window.history.replaceState({}, '', '/app/lab/widgets?embed=1')
    render(<LabGround active />)
    expect(screen.queryByRole('button', { name: 'Commit ready draft', hidden: true })).toBeNull()
    await waitFor(() => expect(connections).toHaveLength(1))
    const document = { schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: {} }
    let restored
    act(() => { restored = connections[0].callbacks.onRestore({ document, ready: true, problems: [] }) })
    await act(async () => { await restored })
    expect(screen.getByLabelText('Canvas document')).toHaveTextContent(JSON.stringify(document))
    expect(screen.getByLabelText('Canvas document')).not.toBeVisible()
    act(() => { connections[0].callbacks.onReload() })
    expect(screen.getByText('Studio Lab reloaded by the dashboard')).toBeVisible()
  })
})
