import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RunsView from './RunsView.jsx'
import { createValueStore } from './valueStore.js'
import { config } from '../../api.js'

const surface = vi.hoisted(() => ({ current: null }))
vi.mock('../useLabSurfaceStatus.js', () => ({ useLabSurfaceStatus: () => surface.current }))
afterEach(() => { cleanup(); surface.current = null; vi.restoreAllMocks() })
const seed = () => {
  const store = createValueStore()
  store.upsertRun('r', { state: 'running', graph_revision: 4 })
  store.apply('r', { seq: 0, kind: 'value', block_id: 'count', payload: { result: { data: 3, as_of: Date.now() - 120000 } } })
  return store
}

describe('Runs view', () => {
  it('says the worker status is unavailable instead of offline when the store read failed', () => {
    surface.current = { worker: { state: 'unavailable', reason: 'lab_store_unavailable' } }
    render(<RunsView store={createValueStore()} />)
    expect(screen.getByText('Lab worker status is unavailable right now.')).toBeVisible()
    expect(screen.queryByText('Worker offline')).toBeNull()
    expect(screen.queryByText('Worker paused')).toBeNull()
    expect(screen.queryByText('Worker status unavailable')).toBeNull()
    expect(screen.queryByText(/^Queue age/)).toBeNull()
  })
  it('offers Resume for an auth-expired run and retries its registered stream once', () => {
    const store = seed(), retry = vi.fn()
    store.setStreamControl('r', { retry, stop: vi.fn() })
    const view = render(<RunsView store={store} mock />)
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    act(() => store.setTransport('r', { status: 'auth_expired' }))
    expect(screen.getByText('Sign in again, then resume')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(retry).toHaveBeenCalledTimes(1)
    act(() => store.setTransport('r', { status: 'connected' }))
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    view.unmount()
  })
  it('renders worker offline, paused, transport lag and stale source as separate indicators', () => {
    surface.current = { worker: { paused: true, workers: [{ heartbeat_age_s: 61 }], queue_age_max_s: 17 } }
    const store = seed(); store.setTransport('r', { status: 'connected', lagMs: 12 })
    render(<RunsView store={store} />)
    expect(screen.getByText('Worker offline')).toBeVisible()
    expect(screen.getByText('Worker paused')).toBeVisible()
    expect(screen.getByText('Stale source')).toBeVisible()
    expect(screen.getByText('Queue age: 17s')).toBeVisible()
    expect(screen.getByText('Stream: connected · lag: 12 ms')).toBeVisible()
    expect(screen.getByText('Source age: 120s')).toBeVisible()
  })
  it('does not confuse an offline worker with fresh source data or healthy transport', () => {
    surface.current = { worker: { paused: false, workers: [], queue_age_max_s: 0 } }
    const store = seed()
    store.apply('r', { seq: 1, kind: 'value', block_id: 'count', payload: { result: { data: 4, as_of: Date.now() } } })
    render(<RunsView store={store} />)
    expect(screen.getByText('Worker offline')).toBeVisible()
    expect(screen.queryByText('Stale source')).toBeNull()
    expect(screen.queryByText('Worker paused')).toBeNull()
  })
  it('keeps a stale source visible when a worker is online at the heartbeat boundary', () => {
    surface.current = { worker: { paused: false, workers: [{ heartbeat_age_s: 60 }] } }
    render(<RunsView store={seed()} />)
    expect(screen.queryByText('Worker offline')).toBeNull()
    expect(screen.getByText('Stale source')).toBeVisible()
  })
  it('posts cancel only once while the request is pending or accepted', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ cancel_requested: true }) })
    render(<RunsView store={seed()} fetchImpl={fetchImpl} mock />)
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    fireEvent.click(cancel); fireEvent.click(cancel)
    await screen.findByText('Cancellation requested')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/runs/r/cancel`)
    expect(fetchImpl.mock.calls[0][1].method).toBe('POST')
    expect(cancel).toBeDisabled()
  })
  it('shows recovered gaps, bounded summaries and event/drop counts with newest runs first', () => {
    const store = createValueStore({ maxEvents: 1 })
    store.upsertRun('older', { state: 'finished', graph_revision: 1 })
    store.upsertRun('newer', { state: 'running', graph_revision: 2 })
    store.addGap('newer', { recovered: true })
    store.apply('newer', { seq: 0, kind: 'value', block_id: 'block', payload: { out: 'x'.repeat(500) } })
    store.apply('newer', { seq: 1, kind: 'value', block_id: 'block', payload: { out: 'x'.repeat(500) } })
    render(<RunsView store={store} mock />)
    expect(screen.getByText('Recovered from a snapshot after a gap')).toBeVisible()
    expect(screen.getAllByRole('article')[0]).toHaveAccessibleName('Run newer')
    const run = within(screen.getByRole('article', { name: 'Run newer' }))
    expect(run.getByText('Events: 1 · Dropped: 1')).toBeVisible()
    expect(run.getByText(/^out:/).textContent).toHaveLength(205)
  })
  it('reads worker details when the shared hook supplies only posture', async () => {
    surface.current = { state: 'on', posture: 'local' }
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ worker: { paused: true, workers: [] } }) })
    render(<RunsView store={createValueStore()} fetchImpl={fetchImpl} />)
    await screen.findByText('Worker paused')
    expect(fetchImpl.mock.calls[0][0]).toBe(`${config.apiBase}/api/lab/status`)
  })
  it('updates an individual block from the shared store', async () => {
    const store = seed(); render(<RunsView store={store} mock />)
    act(() => store.apply('r', { seq: 1, kind: 'value', block_id: 'count', payload: { result: { data: 99, as_of: Date.now() } } }))
    await waitFor(() => expect(screen.getByText(/result:.*99/)).toBeVisible())
  })
})
