import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import WidgetPreview from './WidgetPreview.jsx'
import WidgetsView from './WidgetsView.jsx'
import LabWorkbench from '../LabWorkbench.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'
import { createHostProvider } from '../host/hostProvider.js'
import { evaluateOperation, runtimeValue } from '../runtime/evaluate.js'

const binding = (source, extra = {}) => ({ source, min_renderer: '1.0.0', scale: 'identity', tone: 'ink', ...extra })
const graph = binding('graph.output', { doc_id: 'graph-doc', output: 'total' })
const board = binding('kashbot.board', { field: 'openCount' })
const element = (id, bound) => ({ ...createTextElement(id), min_renderer: '1.0.0', text: `Example ${id}`, ...(bound ? { binding: bound } : {}) })
function documentWith(...elements) {
  const doc = createWidgetDoc()
  doc.formats.la.els = elements
  return doc
}
const doc = documentWith(element('graph', graph), element('host', board), element('literal'))
const resolved = (value) => ({ values: [{ status: 'resolved', value }] })
const pending = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const node = (id) => screen.getByRole('region', { name: 'Live widget preview' }).querySelector(`[data-element-id="${id}"]`)
const flush = async () => { await act(async () => {}) }
const advance = async (ms) => { await act(async () => { vi.advanceTimersByTime(ms) }) }
let visibility
async function visible(value) {
  visibility = value
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(100000)
  visibility = 'visible'
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('live widget values', () => {
  it('clamps polling to five seconds and sends only graph bindings', async () => {
    const host = createHostProvider({ framed: true })
    host.announce([{ source: 'board', data: { openCount: 'PRIVATE_HOST_VALUE' }, as_of: Date.now() }])
    const client = { resolveValues: vi.fn().mockResolvedValue(resolved('Graph value')) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} host={host} intervalMs={1000} />)
    await flush()
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
    expect(client.resolveValues.mock.calls[0][0]).toBe('widget')
    expect(client.resolveValues.mock.calls[0][1]).toEqual([graph])
    expect(JSON.stringify(client.resolveValues.mock.calls)).not.toMatch(/PRIVATE_HOST_VALUE|kashbot\.board|openCount|as_of|host_tainted|simulated/)
    expect(node('graph')).toHaveTextContent('Graph value')
    expect(node('graph')).toHaveAttribute('data-freshness', 'live')
    expect(node('host')).toHaveAttribute('data-host-tainted', 'true')
    expect(node('literal')).toHaveAttribute('data-freshness', 'sample')
    await advance(4999)
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(client.resolveValues).toHaveBeenCalledTimes(2)
  })

  it('pauses hidden polling and resumes once immediately, including repeated visibility events', async () => {
    const client = { resolveValues: vi.fn().mockResolvedValue(resolved(10)) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await flush()
    await visible('hidden'); await advance(20000)
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
    await visible('visible')
    expect(client.resolveValues).toHaveBeenCalledTimes(2)
    await visible('visible')
    expect(client.resolveValues).toHaveBeenCalledTimes(2)
    await advance(5000)
    expect(client.resolveValues).toHaveBeenCalledTimes(3)
  })

  it.each([3000, 5100])('starts the next poll at the later of its deadline and completion at %s ms', async (duration) => {
    const requests = [], starts = [], start = Date.now()
    const client = { resolveValues: vi.fn(() => {
      const request = pending()
      requests.push(request)
      starts.push(Date.now() - start)
      return request.promise
    }) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await advance(duration)
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
    await act(async () => { requests[0].resolve(resolved(1)) })
    if (duration < 5000) {
      expect(client.resolveValues).toHaveBeenCalledTimes(1)
      await advance(5000 - duration - 1)
      expect(client.resolveValues).toHaveBeenCalledTimes(1)
      await advance(1)
    }
    expect(starts).toEqual([0, Math.max(5000, duration)])
    await advance(10000)
    expect(client.resolveValues).toHaveBeenCalledTimes(2)
    await act(async () => { requests[1].resolve(resolved(2)) })
    expect(starts).toEqual([0, Math.max(5000, duration), Math.max(5000, duration) + 10000])
  })

  it('does not start while hidden and waits for visibility', async () => {
    visibility = 'hidden'
    const client = { resolveValues: vi.fn().mockResolvedValue(resolved(1)) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await advance(20000)
    expect(client.resolveValues).not.toHaveBeenCalled()
    await visible('visible')
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
  })

  it('allows one request in flight and aborts it on unmount', async () => {
    const request = pending(), client = { resolveValues: vi.fn(() => request.promise) }
    const { unmount } = render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await advance(20000)
    await visible('hidden'); await visible('visible')
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
    const signal = client.resolveValues.mock.calls[0][2].signal
    expect(signal.aborted).toBe(false)
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { request.resolve(resolved(1)) })
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
  })

  it('joins unmount cancellation to the real values client fetch', async () => {
    const fetchImpl = vi.fn(() => new Promise(() => {}))
    vi.stubGlobal('fetch', fetchImpl)
    const { unmount } = render(<WidgetPreview docId="widget" doc={doc} />)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ bindings: [graph] })
    const signal = fetchImpl.mock.calls[0][1].signal
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('retains values across failures, ages them after three intervals and sanitizes errors', async () => {
    const client = { resolveValues: vi.fn().mockResolvedValueOnce(resolved('Known value')).mockRejectedValue(new Error('server detail PRIVATE_HOST_VALUE')) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await flush(); await advance(5000)
    expect(screen.getByRole('alert')).toHaveTextContent(/^Values unavailable; showing the last known values$/)
    expect(screen.getByRole('alert')).not.toHaveTextContent(/server detail|PRIVATE_HOST_VALUE/)
    expect(node('graph')).toHaveTextContent('Known value')
    expect(node('graph')).toHaveAttribute('data-freshness', 'live')
    await advance(15000)
    expect(node('graph')).toHaveAttribute('data-freshness', 'stale')
  })

  it('marks an unavailable entry stale only if it has a previous value, and recovers', async () => {
    const client = { resolveValues: vi.fn().mockResolvedValueOnce({ values: [{ status: 'unavailable', reason: 'private' }] })
      .mockResolvedValueOnce(resolved(42)).mockResolvedValueOnce({ values: [{ status: 'unavailable' }] }).mockResolvedValue(resolved(43)) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await flush()
    expect(node('graph')).toHaveAttribute('data-freshness', 'sample')
    await advance(5000)
    expect(node('graph')).toHaveAttribute('data-freshness', 'live')
    await advance(5000)
    expect(node('graph')).toHaveTextContent('42')
    expect(node('graph')).toHaveAttribute('data-freshness', 'stale')
    await advance(5000)
    expect(node('graph')).toHaveTextContent('43')
    expect(node('graph')).toHaveAttribute('data-freshness', 'live')
  })

  it('keeps response failures fixed and shows samples until a value is available', async () => {
    const client = { resolveValues: vi.fn().mockResolvedValue({ ok: false, detail: 'private server response' }) }
    render(<WidgetPreview docId="widget" doc={doc} client={client} />)
    await flush()
    expect(node('graph')).toHaveTextContent('Example graph')
    expect(node('graph')).toHaveAttribute('data-freshness', 'sample')
    expect(node('host')).toHaveAttribute('data-freshness', 'sample')
    expect(screen.getByRole('alert')).not.toHaveTextContent('private server response')
  })

  it('labels live, stale and simulated host values without server requests', async () => {
    const host = createHostProvider({ framed: true }), hostDoc = documentWith(element('host', board))
    host.announce([{ source: 'board', data: { openCount: '3' }, as_of: Date.now() }])
    const client = { resolveValues: vi.fn() }
    const { rerender } = render(<WidgetPreview docId="widget" doc={hostDoc} client={client} host={host} />)
    expect(node('host')).toHaveAttribute('data-freshness', 'live')
    expect(node('host')).toHaveTextContent('3')
    await advance(65000)
    expect(node('host')).toHaveAttribute('data-freshness', 'stale')
    host.announce([{ source: 'board', data: { openCount: '4' }, as_of: Date.now(), simulated: true }])
    rerender(<WidgetPreview docId="widget" doc={hostDoc} client={client} host={host} />)
    expect(node('host')).toHaveAttribute('data-freshness', 'simulated')
    expect(node('host')).toHaveAttribute('data-host-tainted', 'true')
    expect(client.resolveValues).not.toHaveBeenCalled()
  })

  it('preserves transitive taint when host and ordinary values are combined', () => {
    const hostInput = runtimeValue([{ count: 2 }], { host_tainted: true, as_of: Date.now() })
    const combined = evaluateOperation('count', { value: hostInput, other: runtimeValue(10) })
    expect(combined.host_tainted).toBe(true)
    const host = createHostProvider({ framed: true })
    host.announce([{ source: 'board', data: { openCount: String(combined.data) }, as_of: combined.as_of }])
    render(<WidgetPreview doc={documentWith(element('mixed', board))} host={host} />)
    expect(node('mixed')).toHaveAttribute('data-host-tainted', 'true')
    expect(node('mixed')).toHaveAttribute('data-freshness', 'live')
    expect(node('mixed')).toHaveTextContent('1')
  })

  it.each([false, true])('uses the later host source for both value and freshness (simulated: %s)', (simulatedLast) => {
    const host = createHostProvider({ framed: true })
    const live = { data: { openCount: '7' }, as_of: Date.now() }
    const simulated = { data: { openCount: '9' }, as_of: Date.now() - 60001, simulated: true, stale: true }
    host.announce([
      { source: 'a-first', ...(simulatedLast ? live : simulated) },
      { source: 'z-last', ...(simulatedLast ? simulated : live) },
    ])
    render(<WidgetPreview host={host} doc={documentWith(element('host', board))} />)
    expect(node('host')).toHaveTextContent(simulatedLast ? '9' : '7')
    expect(node('host')).toHaveAttribute('data-freshness', simulatedLast ? 'simulated' : 'live')
    expect(node('host')).toHaveAttribute('data-host-tainted', 'true')
  })

  it('labels each host field independently and treats missing fields as samples', () => {
    const host = createHostProvider({ framed: true })
    host.announce([
      { source: 'fresh', data: { openCount: '2' }, as_of: Date.now() },
      { source: 'old', data: { cash: '100' }, as_of: Date.now() - 60001 },
      { source: 'practice', data: { exposure: '200' }, simulated: true },
    ])
    render(<WidgetPreview host={host} doc={documentWith(element('fresh', board),
      element('old', binding('kashbot.board', { field: 'cash' })),
      element('practice', binding('kashbot.board', { field: 'exposure' })),
      element('missing', binding('kashbot.board', { field: 'total' })))} />)
    expect(node('fresh')).toHaveAttribute('data-freshness', 'live')
    expect(node('old')).toHaveAttribute('data-freshness', 'stale')
    expect(node('practice')).toHaveAttribute('data-freshness', 'simulated')
    expect(node('missing')).toHaveAttribute('data-freshness', 'sample')
    expect(node('missing')).toHaveTextContent('Example missing')
  })

  it('respects longer intervals and matches ordered graph results in the active format', async () => {
    const activeDoc = documentWith(element('first', graph), element('hidden', graph))
    activeDoc.formats.la.els[1].visible = false
    activeDoc.formats.homeS.els = [element('other', binding('graph.output', { doc_id: 'other-doc', output: 'other' }))]
    const client = { resolveValues: vi.fn().mockResolvedValue({ values: [{ status: 'resolved', value: 'First' }, { status: 'unavailable' }] }) }
    const { rerender } = render(<WidgetPreview docId="widget" doc={activeDoc} client={client} intervalMs={10000} />)
    await flush()
    expect(client.resolveValues.mock.calls[0][1]).toEqual([graph, graph])
    expect(node('first')).toHaveTextContent('First')
    expect(node('hidden')).toBeNull()
    await advance(9999)
    expect(client.resolveValues).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(client.resolveValues).toHaveBeenCalledTimes(2)
    rerender(<WidgetPreview docId="widget" doc={activeDoc} format="homeS" client={client} intervalMs={10000} />)
    await flush()
    expect(client.resolveValues.mock.calls.at(-1)[1]).toEqual([activeDoc.formats.homeS.els[0].binding])
    expect(node('other')).toHaveAttribute('data-freshness', 'live')
  })

  it('mounts the live preview only for saved store documents, never guests', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ kind: 'widget', graph: doc, revision: 1 }) })
    vi.stubGlobal('fetch', fetchImpl)
    const { rerender } = render(<WidgetsView scopeKey="guest" />)
    expect(screen.queryByRole('region', { name: 'Live widget preview' })).not.toBeInTheDocument()
    expect(fetchImpl).not.toHaveBeenCalled()
    rerender(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    expect(screen.queryByRole('region', { name: 'Live widget preview' })).not.toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Load' })) })
    expect(screen.getByRole('region', { name: 'Live widget preview' })).toBeInTheDocument()
    rerender(<WidgetsView scopeKey="guest" />)
    expect(screen.queryByRole('region', { name: 'Live widget preview' })).not.toBeInTheDocument()
  })

  it.each([false, true])('uses the embedded source in a saved widget (simulated: %s) and keeps it out of requests', async (simulated) => {
    let callbacks
    const connection = { start: vi.fn(() => {
      callbacks.onAdmitted()
      callbacks.onSource([{ source: 'board', data: { openCount: 'PRIVATE_HOST_VALUE' }, as_of: Date.now(), simulated }])
    }), stop: vi.fn() }
    const createHostBridge = vi.fn((next) => { callbacks = next; return connection })
    const fetchImpl = vi.fn(async (url) => ({ ok: true, json: async () =>
      url.endsWith('/values') ? resolved('Graph value') : { kind: 'widget', graph: doc, revision: 1 } }))
    vi.stubGlobal('fetch', fetchImpl)
    const { unmount } = render(<LabWorkbench route={{ view: 'widgets', params: {} }} status={{ state: 'on' }} embedded createHostBridge={createHostBridge} />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Load' })) })
    expect(createHostBridge).toHaveBeenCalledTimes(1)
    expect(connection.start).toHaveBeenCalledTimes(1)
    expect(node('host')).toHaveTextContent('PRIVATE_HOST_VALUE')
    expect(node('host')).toHaveAttribute('data-freshness', simulated ? 'simulated' : 'live')
    expect(node('host')).toHaveAttribute('data-host-tainted', 'true')
    expect(node('graph')).toHaveTextContent('Graph value')

    act(() => callbacks.onSource([{ source: 'board', data: { openCount: 'UPDATED_PRIVATE_VALUE' }, as_of: Date.now(), simulated: !simulated }]))
    expect(node('host')).toHaveTextContent('UPDATED_PRIVATE_VALUE')
    expect(node('host')).toHaveAttribute('data-freshness', simulated ? 'live' : 'simulated')
    const requests = fetchImpl.mock.calls.filter(([, options]) => options.body)
    expect(requests).toHaveLength(1)
    expect(JSON.parse(requests[0][1].body)).toEqual({ bindings: [graph] })
    expect(JSON.stringify(fetchImpl.mock.calls)).not.toMatch(/PRIVATE_HOST_VALUE|UPDATED_PRIVATE_VALUE|kashbot\.board|openCount|host_tainted|simulated/)
    unmount()
    expect(connection.stop).toHaveBeenCalledTimes(1)
  })
})
