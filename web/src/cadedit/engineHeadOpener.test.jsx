// W4g-1b: the console's own drawing opens in the browser engine at mount,
// a hand import always wins, a moved head re-opens only a clean engine copy,
// and every failure is a sentence on the ribbon, never a retry loop.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineHeadOpener, { REACH_STATE, headDocumentId } from './EngineHeadOpener.jsx'
import EngineRibbonClusters, { DRAW_REASONS, MODIFY_REASONS } from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0, 0], [100, 50, 0]], radius: null, startDeg: null, endDeg: null }
const BYTES = new TextEncoder().encode('0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n')

function fileOf(name = 'hand.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
let handle
function mount({ drawingId = 'rooftop_demo', enabled = true, headKey = 1, fetchDxf, onDirtyChange = null } = {}) {
  workers = []
  handle = {}
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  function Probe() { handle.context = useEngineSessionContext(); return null }
  function Tree(props) {
    return (
      <EngineSessionProvider createWorker={createWorker} onDirtyChange={props.onDirtyChange}>
        <Probe />
        <EngineHeadOpener drawingId={props.drawingId} enabled={props.enabled} headKey={props.headKey} fetchDxf={props.fetchDxf} />
        <DraftingRibbon clusters={[]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
        </DraftingRibbon>
        <CadEditSurface enabled />
      </EngineSessionProvider>
    )
  }
  const utils = render(<Tree drawingId={drawingId} enabled={enabled} headKey={headKey} fetchDxf={fetchDxf} onDirtyChange={onDirtyChange} />)
  handle.rerender = (next) => utils.rerender(<Tree drawingId={drawingId} enabled={enabled} headKey={headKey} fetchDxf={fetchDxf} onDirtyChange={onDirtyChange} {...next} />)
  handle.unmount = utils.unmount
  return handle
}

function answer(version = 1, extra = {}) {
  return { bytes: BYTES, version, head: version, source: 'intake-synth', etag: '"x"', ...extra }
}

async function settle() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
}

function loaded(worker, documentId, entities = [LINE]) {
  worker.emit({ type: 'documentLoaded', documentId, entities, entityCount: entities.length, unsupported: [] })
}

const note = () => screen.getByRole('group', { name: 'Modify' }).querySelector('.ribbon-note')?.textContent ?? ''

beforeEach(() => {
  vi.useRealTimers()
  // The surface offers the edited bytes as a download; jsdom has no blob URLs.
  URL.createObjectURL = vi.fn(() => 'blob:engine')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup() })

describe('EngineHeadOpener', () => {
  it('opens the head into the engine at mount and the tools go live without an import', async () => {
    const fetchDxf = vi.fn(async () => answer(3))
    const studio = mount({ fetchDxf })
    // While the bytes are in flight the ribbon says so.
    expect(note()).toBe('opening rooftop_demo in the browser engine...')
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(fetchDxf).toHaveBeenCalledWith('rooftop_demo')
    await waitFor(() => expect(workers.length).toBe(1))
    const post = workers[0].posted.find((m) => m.type === 'loadDocument')
    expect(post.documentId).toBe(headDocumentId('rooftop_demo', 3))
    expect(post.bytes).toBe(BYTES)
    loaded(workers[0], post.documentId)
    expect(studio.context.session.engineParsed).toBe(true)
    expect(studio.context.session.committedVersion).toBe(3)
    expect(studio.context.session.committedEntities).toEqual([LINE])
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
    expect(studio.context.reach.version).toBe(3)
    expect(screen.getByRole('button', { name: 'line' })).toBeEnabled()
    expect(note()).toBe(MODIFY_REASONS.noSelection)
  })

  it('a fetch failure is the ribbon reason, tried once per head, and the hand import still works', async () => {
    const fetchDxf = vi.fn(async () => { const e = new Error('GET /api/drawings/rooftop_demo/dxf -> 503'); e.status = 503; throw e })
    const studio = mount({ fetchDxf })
    await settle()
    expect(studio.context.reach.state).toBe(REACH_STATE.FAILED)
    expect(note()).toBe('the drawing could not be opened in the browser engine: GET /api/drawings/rooftop_demo/dxf -> 503; import a DXF instead')
    expect(screen.getByRole('button', { name: /^line/ })).toBeDisabled()
    studio.rerender({})
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    // The import pane is untouched by the failure.
    await act(async () => {
      fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf('hand.dxf')] } })
      await Promise.resolve(); await Promise.resolve()
    })
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], 'hand.dxf')
    expect(studio.context.session.documentId).toBe('hand.dxf')
    expect(screen.getByRole('button', { name: 'line' })).toBeEnabled()
    // The next head tries again.
    fetchDxf.mockImplementation(async () => answer(4))
    studio.rerender({ headKey: 4 })
    await settle()
    // ...but a hand import is never replaced.
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(studio.context.session.documentId).toBe('hand.dxf')
  })

  it('a hand import that lands while the bytes are in flight wins; the late bytes are dropped', async () => {
    let resolve
    const fetchDxf = vi.fn(() => new Promise((r) => { resolve = r }))
    const studio = mount({ fetchDxf })
    await settle()
    await act(async () => {
      fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf('hand.dxf')] } })
      await Promise.resolve(); await Promise.resolve()
    })
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], 'hand.dxf')
    await act(async () => { resolve(answer(1)); await Promise.resolve(); await Promise.resolve() })
    expect(workers[0].posted.filter((m) => m.type === 'loadDocument')).toHaveLength(1)
    expect(studio.context.session.documentId).toBe('hand.dxf')
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
  })

  it('a moved head re-opens a clean engine copy, and reports stale over unsaved edits', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    // The server head moves (a tool run): the engine follows.
    fetchDxf.mockImplementation(async () => answer(2))
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    const posts = workers[0].posted.filter((m) => m.type === 'loadDocument')
    expect(posts[posts.length - 1].documentId).toBe(headDocumentId('rooftop_demo', 2))
    loaded(workers[0], headDocumentId('rooftop_demo', 2))
    // An engine edit makes the copy dirty; the next head move must not discard it.
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.dirty).toBe(true)
    studio.rerender({ headKey: 3 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    expect(studio.context.reach.state).toBe(REACH_STATE.STALE)
    expect(studio.context.session.documentId).toBe(headDocumentId('rooftop_demo', 2))
    expect(studio.context.session.entityCount).toBe(2)
  })

  it('W4g-2: the provider reports dirty to the host on change only (edit -> true, save -> false, unmount -> false)', async () => {
    const onDirtyChange = vi.fn()
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf, onDirtyChange })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)
    const calls = onDirtyChange.mock.calls.length
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
    expect(onDirtyChange.mock.calls.length).toBe(calls + 1)
    // A second edit is still dirty: no second call.
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }, { ...LINE, id: 'e3' }], entityCount: 3, bytes: new Uint8Array([48, 11]), byteLength: 2 })
    expect(onDirtyChange.mock.calls.length).toBe(calls + 1)
    studio.unmount()
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)
  })

  it('disabled, or without a drawing, it opens nothing and the ribbon keeps the plain reason', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    mount({ fetchDxf, enabled: false })
    await settle()
    expect(fetchDxf).not.toHaveBeenCalled()
    expect(note()).toBe(MODIFY_REASONS.noDocument)
    expect(DRAW_REASONS.noDocument).toBe(MODIFY_REASONS.noDocument)
    cleanup()
    mount({ fetchDxf, drawingId: null })
    await settle()
    expect(fetchDxf).not.toHaveBeenCalled()
  })

  it('a fetch that resolves while an edit is in flight stands down, then re-opens only if the engine is clean once busy clears', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    // The head moves; the fetch for v2 is in flight...
    let resolve
    fetchDxf.mockImplementation(() => new Promise((r) => { resolve = r }))
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    // ...and the drafter applies an edit: busy is set now, its reply lands later.
    act(() => { studio.context.session.actions.select('e1') })
    act(() => { studio.context.session.actions.applyEdit('move', { dx: '1', dy: '0' }) })
    expect(studio.context.session.busy).toBe(true)
    await act(async () => { resolve(answer(2)); await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    // No load over the in-flight edit.
    expect(workers[0].posted.filter((m) => m.type === 'loadDocument')).toHaveLength(1)
    // The edit lands: dirty. Busy clears, the effect tries again and reports stale, never loading.
    fetchDxf.mockImplementation(async () => answer(2))
    workers[0].emit({ type: 'editApplied', op: 'move', ok: true, entities: [LINE], entityCount: 1, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    await settle()
    expect(studio.context.session.dirty).toBe(true)
    expect(studio.context.reach.state).toBe(REACH_STATE.STALE)
    expect(workers[0].posted.filter((m) => m.type === 'loadDocument')).toHaveLength(1)
  })

  it('hidden version headers (a cross-origin API) fall back to the head the host holds; no head at all is the sentence', async () => {
    const fetchDxf = vi.fn(async () => ({ bytes: BYTES, version: null, head: null, source: '', etag: null }))
    const studio = mount({ fetchDxf, headKey: 7 })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    const post = workers[0].posted.find((m) => m.type === 'loadDocument')
    expect(post.documentId).toBe(headDocumentId('rooftop_demo', 7))
    expect(studio.context.reach.version).toBe(7)
    cleanup()
    const again = mount({ fetchDxf, headKey: null })
    await settle()
    expect(again.context.reach.state).toBe(REACH_STATE.FAILED)
    expect(note()).toContain('answered without a document')
  })

  it('a server answer without a document is a sentence, not a crash', async () => {
    const fetchDxf = vi.fn(async () => ({ bytes: null, version: 0 }))
    const studio = mount({ fetchDxf })
    await settle()
    expect(studio.context.reach.state).toBe(REACH_STATE.FAILED)
    expect(note()).toContain('answered without a document')
    expect(workers.length).toBe(0)
  })
})
