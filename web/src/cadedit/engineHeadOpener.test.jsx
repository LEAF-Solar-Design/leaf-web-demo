// W4g-1b: the console's own drawing opens in the browser engine at mount,
// a hand import always wins, a moved head re-opens only a clean engine copy,
// and every failure is a sentence on the ribbon, never a retry loop.
import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineHeadOpener, { REACH_STATE, headDocumentId, holdsHeadDocument } from './EngineHeadOpener.jsx'
import EngineRibbonClusters, { DRAW_REASONS, MODIFY_REASONS, SAVE_REASONS, saveReason } from './EngineRibbonClusters.jsx'
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
function mount({ drawingId = 'rooftop_demo', enabled = true, standDown = false, headKey = 1, fetchDxf, sourceKey = 'live', opener = true, saveTarget = null, onDirtyChange = null, onDocumentChange = null, strict = false } = {}) {
  workers = []
  const wrap = (tree) => (strict ? <StrictMode>{tree}</StrictMode> : tree)
  handle = {}
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  function Probe() { handle.context = useEngineSessionContext(); return null }
  function Tree(props) {
    return (
      <EngineSessionProvider createWorker={createWorker} saveTarget={props.saveTarget} onDirtyChange={props.onDirtyChange} onDocumentChange={props.onDocumentChange}>
        <Probe />
        {props.opener !== false && <EngineHeadOpener drawingId={props.drawingId} enabled={props.enabled} standDown={props.standDown} headKey={props.headKey} fetchDxf={props.fetchDxf} sourceKey={props.sourceKey} />}
        <DraftingRibbon clusters={[]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
        </DraftingRibbon>
        <CadEditSurface enabled />
      </EngineSessionProvider>
    )
  }
  const utils = render(wrap(<Tree drawingId={drawingId} enabled={enabled} standDown={standDown} headKey={headKey} fetchDxf={fetchDxf} sourceKey={sourceKey} opener={opener} saveTarget={saveTarget} onDirtyChange={onDirtyChange} onDocumentChange={onDocumentChange} />))
  handle.rerender = (next) => utils.rerender(wrap(<Tree drawingId={drawingId} enabled={enabled} standDown={standDown} headKey={headKey} fetchDxf={fetchDxf} sourceKey={sourceKey} opener={opener} saveTarget={saveTarget} onDirtyChange={onDirtyChange} onDocumentChange={onDocumentChange} {...next} />))
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
  it('a disabled opener fetches no head and opens no document', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ enabled: false, fetchDxf })
    await settle()
    expect(fetchDxf).not.toHaveBeenCalled()
    expect(workers).toHaveLength(0)
    expect(studio.context.session.documentId).toBe('')
    expect(studio.context.session.engineParsed).toBe(false)
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
    expect(note()).toBe(MODIFY_REASONS.noDocument)
  })

  it('disabling enabled alone preserves the document, edits and selection without reopening', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    act(() => { studio.context.session.actions.select('e1') })
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE], entityCount: 1, bytes: BYTES, byteLength: BYTES.length })
    studio.rerender({ enabled: false })
    await settle()
    expect(workers[0].terminated).toBe(false)
    expect(studio.context.session.documentId).toBe(headDocumentId('rooftop_demo', 1))
    expect(studio.context.session.dirty).toBe(true)
    expect(studio.context.session.selectedIds).toEqual(['e1'])
    studio.rerender({ enabled: true })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(workers).toHaveLength(1)
    expect(studio.context.session.dirty).toBe(true)
  })

  it('standDown false to true retires a clean document so drafting and Save are unavailable', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const saveTarget = { headVersion: 1, save: vi.fn() }
    const studio = mount({ fetchDxf, saveTarget })
    await settle()
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    act(() => { studio.context.session.actions.select('e1') })
    expect(studio.context.session.dirty).toBe(false)
    studio.rerender({ enabled: false, standDown: true })
    await settle()
    expect(workers[0].terminated).toBe(true)
    expect(studio.context.session.documentId).toBe('')
    expect(studio.context.session.entityCount).toBe(0)
    expect(studio.context.session.selectedIds).toEqual([])
    expect(studio.context.session.savedBytes).toBe(null)
    expect(studio.context.session.engineParsed).toBe(false)
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
    expect(note()).toBe(MODIFY_REASONS.noDocument)
    expect(saveReason(studio.context.session, studio.context.canSave)).toBe(SAVE_REASONS.noDocument)
    await act(async () => { await studio.context.session.actions.save() })
    expect(saveTarget.save).not.toHaveBeenCalled()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    // Leaving project mode may open the same head again after the reset.
    studio.rerender({ enabled: true, standDown: false })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    expect(workers).toHaveLength(2)
  })

  it('standDown false to true holds a dirty document until it is saved or the project closes', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: BYTES, byteLength: BYTES.length })
    const savedBytes = studio.context.session.savedBytes
    expect(studio.context.session.dirty).toBe(true)
    studio.rerender({ enabled: false, standDown: true })
    await settle()
    expect(workers[0].terminated).toBe(false)
    expect(studio.context.session.documentId).toBe(headDocumentId('rooftop_demo', 1))
    expect(studio.context.session.entityCount).toBe(2)
    expect(studio.context.session.savedBytes).toBe(savedBytes)
    expect(studio.context.session.dirty).toBe(true)
    expect(studio.context.reach.state).toBe(REACH_STATE.HELD)
    expect(studio.context.reach.sentence).toBe('save the edits to the previous drawing, or close the project to keep editing it, before editing this project')
    expect(note()).toBe(studio.context.reach.sentence)
    expect(fetchDxf).toHaveBeenCalledTimes(1)
  })

  it.each([true, false])('defers standDown while an edit is busy, then resolves after success=%s', async (ok) => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    const worker = workers[0]
    const documentId = headDocumentId('rooftop_demo', 1)
    loaded(worker, documentId)
    act(() => { studio.context.session.actions.select('e1') })
    act(() => { studio.context.session.actions.applyEdit('move', { dx: '1', dy: '0' }) })
    expect(studio.context.session.busy).toBe(true)
    expect(studio.context.session.dirty).toBe(false)
    studio.rerender({ enabled: false, standDown: true })
    await settle()
    expect(worker.terminated).toBe(false)
    expect(studio.context.session.documentId).toBe(documentId)
    expect(studio.context.session.busy).toBe(true)
    worker.emit({ type: 'editApplied', op: 'move', ok, reason: 'refused', entities: [LINE], entityCount: 1, bytes: BYTES, byteLength: BYTES.length })
    await settle()
    expect(studio.context.session.busy).toBe(false)
    expect(worker.terminated).toBe(!ok)
    expect(studio.context.session.documentId).toBe(ok ? documentId : '')
    expect(studio.context.reach.state).toBe(ok ? REACH_STATE.HELD : REACH_STATE.IDLE)
    expect(studio.context.session.dirty).toBe(ok)
    expect(fetchDxf).toHaveBeenCalledTimes(1)
  })

  it('waits for an in-flight load to finish before standing down the clean document', async () => {
    const studio = mount({ fetchDxf: vi.fn(async () => answer(1)) })
    await settle()
    const worker = workers[0]
    expect(studio.context.session.busy).toBe(true)
    studio.rerender({ enabled: false, standDown: true })
    await settle()
    expect(worker.terminated).toBe(false)
    loaded(worker, headDocumentId('rooftop_demo', 1))
    await settle()
    expect(worker.terminated).toBe(true)
    expect(studio.context.session.documentId).toBe('')
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
  })

  it('an ordinary surface switch preserves a clean document even after its busy load settles', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    const worker = workers[0]
    expect(studio.context.session.busy).toBe(true)
    studio.rerender({ enabled: false })
    loaded(worker, headDocumentId('rooftop_demo', 1))
    await settle()
    expect(studio.context.session.dirty).toBe(false)
    expect(studio.context.session.documentId).toBe(headDocumentId('rooftop_demo', 1))
    expect(worker.terminated).toBe(false)
    studio.rerender({ enabled: true })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(workers).toHaveLength(1)
  })

  it('a failed save keeps the hold and all edited bytes for retry', async () => {
    const save = vi.fn(async () => { throw new Error('offline') })
    const studio = mount({ fetchDxf: vi.fn(async () => answer(1)), saveTarget: { drawingId: 'rooftop_demo', headVersion: 1, save } })
    await settle()
    const worker = workers[0]
    const documentId = headDocumentId('rooftop_demo', 1)
    loaded(worker, documentId)
    worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [LINE], entityCount: 1, bytes: BYTES, byteLength: BYTES.length })
    studio.rerender({ enabled: false, standDown: true })
    await act(async () => { await studio.context.session.actions.save() })
    await settle()
    expect(save).toHaveBeenCalledTimes(1)
    expect(worker.terminated).toBe(false)
    expect(studio.context.session.documentId).toBe(documentId)
    expect(studio.context.session.savedBytes).toBe(BYTES)
    expect(studio.context.session.dirty).toBe(true)
    expect(studio.context.reach.state).toBe(REACH_STATE.HELD)
    expect(document.querySelector('[data-tool="save-version"]').disabled).toBe(false)
  })

  it('saving a held document uses its legacy target and stands down only after the save completes', async () => {
    let resolveSave
    const save = vi.fn(() => new Promise((resolve) => { resolveSave = resolve }))
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf, saveTarget: { drawingId: 'rooftop_demo', headVersion: 1, save } })
    await settle()
    const worker = workers[0]
    loaded(worker, headDocumentId('rooftop_demo', 1))
    worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [LINE], entityCount: 1, bytes: BYTES, byteLength: BYTES.length })
    studio.rerender({ enabled: false, standDown: true })
    await settle()
    const saveButton = document.querySelector('[data-tool="save-version"]')
    expect(saveButton.disabled).toBe(false)
    await act(async () => { fireEvent.click(saveButton) })
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toBe(BYTES)
    expect(save.mock.calls[0][1]).toBe(1)
    expect(studio.context.session.busy).toBe(true)
    expect(studio.context.reach.state).toBe(REACH_STATE.HELD)
    expect(worker.terminated).toBe(false)
    await act(async () => { resolveSave({ new_version: { drawing_id: 'rooftop_demo', version: 2, parent: 1 }, head: 2 }) })
    await settle()
    expect(worker.terminated).toBe(true)
    expect(studio.context.session.documentId).toBe('')
    expect(studio.context.session.savedBytes).toBe(null)
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
    expect(fetchDxf).toHaveBeenCalledTimes(1)
  })

  it('closing the project clears the hold with the dirty document and command intact', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    const worker = workers[0]
    const documentId = headDocumentId('rooftop_demo', 1)
    loaded(worker, documentId)
    act(() => {
      studio.context.session.actions.select('e1')
      studio.context.setArmed({ group: 'modify', op: 'move' })
    })
    worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [LINE], entityCount: 1, bytes: BYTES, byteLength: BYTES.length })
    const before = studio.context.session
    studio.rerender({ enabled: false, standDown: true })
    await settle()
    expect(studio.context.reach.state).toBe(REACH_STATE.HELD)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    studio.rerender({ enabled: true, standDown: false })
    await settle()
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
    expect(studio.context.session.documentId).toBe(documentId)
    expect(studio.context.session.savedBytes).toBe(before.savedBytes)
    expect(studio.context.session.entities).toBe(before.entities)
    expect(studio.context.session.selectedIds).toEqual(['e1'])
    expect(studio.context.session.undoDepth).toBe(before.undoDepth)
    expect(studio.context.session.dirty).toBe(true)
    expect(studio.context.armed).toEqual({ group: 'modify', op: 'move' })
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    expect(worker.terminated).toBe(false)
    expect(fetchDxf).toHaveBeenCalledTimes(1)
  })

  it('standing down abandons a pending head fetch even when it later succeeds', async () => {
    let resolve
    const fetchDxf = vi.fn(() => new Promise((r) => { resolve = r }))
    const studio = mount({ fetchDxf })
    await settle()
    studio.rerender({ enabled: false, standDown: true })
    await act(async () => { resolve(answer(1)) })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(workers).toHaveLength(0)
    expect(studio.context.session.documentId).toBe('')
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
  })

  it('row17 reports committed head provenance, plain same-name imports, and null on unmount', async () => {
    const onDocumentChange = vi.fn()
    const studio = mount({ fetchDxf: vi.fn(async () => answer(1)), onDocumentChange })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    const documentId = headDocumentId('rooftop_demo', 1)
    loaded(workers[0], documentId)
    // C-04C: provenance distinguishes the committed head from a same-name hand import.
    expect(onDocumentChange).toHaveBeenLastCalledWith({ documentId, committedVersion: 1, entityCount: 1, documentOrigin: 'head' })
    const calls = onDocumentChange.mock.calls.length
    studio.rerender({})
    await settle()
    expect(onDocumentChange).toHaveBeenCalledTimes(calls)
    await act(async () => { await studio.context.session.actions.open(fileOf(documentId)) })
    loaded(workers[workers.length - 1], documentId, [LINE, { ...LINE, id: 'e2' }])
    expect(onDocumentChange).toHaveBeenLastCalledWith({ documentId, committedVersion: null, entityCount: 2, documentOrigin: 'import' })
    studio.unmount()
    expect(onDocumentChange).toHaveBeenLastCalledWith(null)
  })

  it('opens the head into the engine at mount and the tools go live without an import', async () => {
    const fetchDxf = vi.fn(async () => answer(3))
    const studio = mount({ fetchDxf })
    // While the bytes are in flight the ribbon says so.
    expect(note()).toBe('Opening the drawing...')
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
    expect(note()).toBe('The drawing service is temporarily unavailable. Try again in a moment.')
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

  // The attempt identity is (drawing, source, head): the live API and the
  // static sample at the same head number are different heads.
  const SAMPLE_BYTES = new TextEncoder().encode('0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n')
  const sampleAnswer = (version = 1) => ({ ...answer(version), bytes: SAMPLE_BYTES, source: 'sample' })
  const loadPosts = () => workers.flatMap((w) => w.posted).filter((m) => m.type === 'loadDocument')

  it('source switch: a live head then the sample head re-opens from the sample', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf: fetchA, sourceKey: 'live' })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    const fetchB = vi.fn(async () => sampleAnswer(1))
    studio.rerender({ sourceKey: 'sample', fetchDxf: fetchB })
    await settle()
    expect(fetchB).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(loadPosts()[loadPosts().length - 1].bytes).toBe(SAMPLE_BYTES))
    const last = loadPosts()[loadPosts().length - 1]
    expect(last.documentId).toBe(headDocumentId('rooftop_demo', 1))
    expect(fetchA).toHaveBeenCalledTimes(1)
  })

  it('source switch: a settled live failure does not block the sample', async () => {
    const fetchA = vi.fn(async () => { const e = new Error('GET /api/drawings/rooftop_demo/dxf -> 503'); e.status = 503; throw e })
    const studio = mount({ fetchDxf: fetchA, sourceKey: 'live' })
    await settle()
    expect(studio.context.reach.state).toBe(REACH_STATE.FAILED)
    const fetchB = vi.fn(async () => sampleAnswer(1))
    studio.rerender({ sourceKey: 'sample', fetchDxf: fetchB })
    await settle()
    expect(fetchB).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(loadPosts().some((m) => m.bytes === SAMPLE_BYTES)).toBe(true))
    expect(fetchA).toHaveBeenCalledTimes(1)
  })

  it('source switch: an in-flight live fetch never lands after the switch', async () => {
    let resolveA
    const fetchA = vi.fn(() => new Promise((r) => { resolveA = r }))
    const studio = mount({ fetchDxf: fetchA, sourceKey: 'live' })
    await settle()
    expect(fetchA).toHaveBeenCalledTimes(1)
    const fetchB = vi.fn(async () => sampleAnswer(1))
    studio.rerender({ sourceKey: 'sample', fetchDxf: fetchB })
    await settle()
    expect(fetchB).toHaveBeenCalledTimes(1)
    await act(async () => { resolveA(answer(1)); await Promise.resolve(); await Promise.resolve() })
    await settle()
    await waitFor(() => expect(loadPosts()).toHaveLength(1))
    expect(loadPosts()[0].bytes).toBe(SAMPLE_BYTES)
    expect(loadPosts().some((m) => m.bytes === BYTES)).toBe(false)
  })

  it('source switch: a dirty engine document is not replaced', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf: fetchA, sourceKey: 'live' })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    // An engine edit makes the copy dirty; the source switch must not discard it.
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.dirty).toBe(true)
    const fetchB = vi.fn(async () => sampleAnswer(1))
    studio.rerender({ sourceKey: 'sample', fetchDxf: fetchB })
    await settle()
    expect(fetchB).not.toHaveBeenCalled()
    expect(studio.context.reach.state).toBe(REACH_STATE.STALE)
    expect(studio.context.session.entityCount).toBe(2)
    expect(loadPosts()).toHaveLength(1)
  })

  it('source switch: the same source and head fetch once', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf: fetchA, sourceKey: 'live' })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    studio.rerender({})
    await settle()
    studio.rerender({ sourceKey: 'live', headKey: 1 })
    await settle()
    studio.rerender({ sourceKey: 'live' })
    await settle()
    expect(fetchA).toHaveBeenCalledTimes(1)
  })

  // The engine-save shortcut: a head this engine saved is already held, but
  // only within the source it was opened from.
  const saveReceipt = () => ({ new_version: { drawing_id: 'rooftop_demo', version: 2, parent: 1 }, head: 2, cost: { engine_usd: 0 } })
  async function openEditAndSave(fetchA) {
    const saveTarget = { headVersion: 1, save: vi.fn(async () => saveReceipt()) }
    const studio = mount({ fetchDxf: fetchA, sourceKey: 'live', saveTarget })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.dirty).toBe(true)
    await act(async () => { await studio.context.session.actions.save() })
    expect(saveTarget.save).toHaveBeenCalledTimes(1)
    expect(studio.context.session.savedVersion).toBe(2)
    expect(studio.context.session.dirty).toBe(false)
    return studio
  }

  it('source switch: the head this engine saved is not fetched again within its source', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = await openEditAndSave(fetchA)
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchA).toHaveBeenCalledTimes(1)
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
    expect(studio.context.reach.source).toBe('engine-save')
  })

  it('source switch: an engine save never stands in for the other source\'s head', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = await openEditAndSave(fetchA)
    const fetchB = vi.fn(async () => sampleAnswer(2))
    studio.rerender({ headKey: 2, sourceKey: 'sample', fetchDxf: fetchB })
    await settle()
    expect(fetchB).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(loadPosts()[loadPosts().length - 1].bytes).toBe(SAMPLE_BYTES))
    expect(fetchA).toHaveBeenCalledTimes(1)
  })

  it('source switch: a remounted opener keeps the save shortcut', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = await openEditAndSave(fetchA)
    studio.rerender({ opener: false })
    await settle()
    studio.rerender({ opener: true, headKey: 2 })
    await settle()
    expect(fetchA).toHaveBeenCalledTimes(1)
    expect(studio.context.reach.source).toBe('engine-save')
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
    expect(loadPosts()).toHaveLength(1)
  })

  it('source switch: a save from the other source never stands in for this source\'s head', async () => {
    const fetchA = vi.fn(async () => answer(1))
    const studio = await openEditAndSave(fetchA)
    expect(studio.context.session.savedVersion).toBe(2)
    const fetchB = vi.fn(async () => sampleAnswer(1))
    studio.rerender({ sourceKey: 'sample', fetchDxf: fetchB })
    await settle()
    expect(fetchB).toHaveBeenCalledTimes(1)
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    // The session keeps the live save's version across the sample load.
    expect(studio.context.session.savedVersion).toBe(2)
    const fetchC = vi.fn(async () => sampleAnswer(2))
    studio.rerender({ sourceKey: 'sample', headKey: 2, fetchDxf: fetchC })
    await settle()
    expect(fetchC).toHaveBeenCalledTimes(1)
    expect(studio.context.reach.source).not.toBe('engine-save')
  })

  // #218: provenance, not the filename, says the engine holds the head.
  async function openHeadThenHandImportNamedLikeIt(fetchDxf) {
    const studio = mount({ fetchDxf })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    await act(async () => { await studio.context.session.actions.open(fileOf(headDocumentId('rooftop_demo', 1))) })
    loaded(workers[workers.length - 1], headDocumentId('rooftop_demo', 1), [LINE, { ...LINE, id: 'e2' }])
    expect(studio.context.session.documentOrigin).toBe('import')
    return studio
  }

  it('provenance: a hand import named like the head is never replaced by a moved head', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = await openHeadThenHandImportNamedLikeIt(fetchDxf)
    fetchDxf.mockImplementation(async () => answer(2))
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(studio.context.session.documentId).toBe(headDocumentId('rooftop_demo', 1))
    expect(studio.context.session.entityCount).toBe(2)
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
    const posts = loadPosts()
    expect(posts[posts.length - 1].documentId).toBe(headDocumentId('rooftop_demo', 1))
    expect(posts.some((m) => m.documentId === headDocumentId('rooftop_demo', 2))).toBe(false)
  })

  it('provenance: an edited hand import named like the head reads idle, not stale', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = await openHeadThenHandImportNamedLikeIt(fetchDxf)
    workers[workers.length - 1].emit({ type: 'editApplied', op: 'createLine', ok: true, entities: [LINE, { ...LINE, id: 'e2' }, { ...LINE, id: 'e3' }], entityCount: 3, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.dirty).toBe(true)
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(studio.context.reach.state).toBe(REACH_STATE.IDLE)
    expect(studio.context.session.entityCount).toBe(3)
  })

  it('provenance: the opener\'s own head load in flight still counts as the head', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    expect(studio.context.session.documentOrigin).toBe(null)
    expect(studio.context.session.busy).toBe(true)
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
    studio.rerender({})
    await settle()
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
    expect(studio.context.session.documentOrigin).toBe('head')
  })

  it('provenance: holdsHeadDocument decides by origin and shape', () => {
    const opened = 'rooftop_demo-v1.dxf'
    const rows = [
      ['head', 'rooftop_demo-v2.dxf', true],
      ['import', 'rooftop_demo-v1.dxf', false],
      ['starter', 'rooftop_demo-v1.dxf', false],
      [null, 'rooftop_demo-v1.dxf', true],
      [null, 'rooftop_demo-v3.dxf', false],
      ['head', 'other-v1.dxf', false],
      ['head', 'hand.dxf', false],
      [undefined, 'rooftop_demo-v1.dxf', false],
    ]
    for (const [documentOrigin, documentId, expected] of rows) {
      expect([documentOrigin, documentId, holdsHeadDocument({ documentId, documentOrigin }, 'rooftop_demo', opened)])
        .toEqual([documentOrigin, documentId, expected])
    }
  })

  // #217: StrictMode's setup, cleanup, setup shares one head fetch.
  const postedWorker = () => workers.find((w) => w.posted.some((m) => m.type === 'loadDocument'))

  it('one fetch: StrictMode opens the head with one fetch', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf, strict: true })
    await settle()
    await waitFor(() => expect(workers.length).toBeGreaterThan(0))
    await waitFor(() => expect(loadPosts()).toHaveLength(1))
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(loadPosts()).toHaveLength(1)
    loaded(postedWorker(), headDocumentId('rooftop_demo', 1))
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
  })

  it('one fetch: a StrictMode failure is one failure, not a retry', async () => {
    const fetchDxf = vi.fn(async () => { const e = new Error('GET /api/drawings/rooftop_demo/dxf -> 503'); e.status = 503; throw e })
    const studio = mount({ fetchDxf, strict: true })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    expect(studio.context.reach.state).toBe(REACH_STATE.FAILED)
    expect(note()).toBe('The drawing service is temporarily unavailable. Try again in a moment.')
  })

  it('one fetch: a new head after a shared fetch starts its own fetch', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf, strict: true })
    await settle()
    await waitFor(() => expect(loadPosts()).toHaveLength(1))
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    loaded(postedWorker(), headDocumentId('rooftop_demo', 1))
    fetchDxf.mockImplementation(async () => answer(2))
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(loadPosts()[loadPosts().length - 1].documentId).toBe(headDocumentId('rooftop_demo', 2)))
  })

  it('provenance: a longer drawing id that shares the prefix is not this drawing\'s head', () => {
    const rows = [
      ['rooftop_demo-villa-v1.dxf', false],
      ['rooftop_demo-v12.dxf', true],
      ['rooftop_demo-v.dxf', false],
      ['rooftop_demo-v1.dxf.bak', false],
    ]
    for (const [documentId, expected] of rows) {
      expect([documentId, holdsHeadDocument({ documentId, documentOrigin: 'head' }, 'rooftop_demo', null)])
        .toEqual([documentId, expected])
    }
    expect(holdsHeadDocument({ documentId: '-v1.dxf', documentOrigin: 'head' }, '', null)).toBe(false)
  })

  it('one fetch: a fetch that fails while its run was cancelled is fetched again', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    // The head moves; the fetch for v2 is in flight...
    let reject
    fetchDxf.mockImplementation(() => new Promise((_, r) => { reject = r }))
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    // ...an edit goes busy, cancelling the run that owned the fetch...
    act(() => { studio.context.session.actions.select('e1') })
    act(() => { studio.context.session.actions.applyEdit('move', { dx: '1', dy: '0' }) })
    expect(studio.context.session.busy).toBe(true)
    // ...and the fetch fails with no run attached.
    await act(async () => { reject(new Error('GET /api/drawings/rooftop_demo/dxf -> 503')); await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    // The edit is refused: clean and quiet again, so the same key fetches anew.
    fetchDxf.mockImplementation(async () => answer(2))
    workers[0].emit({ type: 'editApplied', op: 'move', ok: false, reason: 'test refusal' })
    await settle()
    expect(studio.context.session.dirty).toBe(false)
    expect(fetchDxf).toHaveBeenCalledTimes(3)
    const posts = workers[0].posted.filter((m) => m.type === 'loadDocument')
    expect(posts[posts.length - 1].documentId).toBe(headDocumentId('rooftop_demo', 2))
    expect(studio.context.reach.state).not.toBe(REACH_STATE.FAILED)
    // The v2 load is in flight; once it lands the session is quiet and the head is open.
    loaded(workers[0], headDocumentId('rooftop_demo', 2))
    expect(studio.context.session.busy).toBe(false)
    expect(studio.context.session.dirty).toBe(false)
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
  })

  it('one fetch: a fetch that settles just before a later run is fetched again', async () => {
    const fetchDxf = vi.fn(async () => answer(1))
    const studio = mount({ fetchDxf })
    await settle()
    await waitFor(() => expect(workers.length).toBe(1))
    loaded(workers[0], headDocumentId('rooftop_demo', 1))
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    let reject
    fetchDxf.mockImplementation(() => new Promise((_, r) => { reject = r }))
    studio.rerender({ headKey: 2 })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    act(() => { studio.context.session.actions.select('e1') })
    act(() => { studio.context.session.actions.applyEdit('move', { dx: '1', dy: '0' }) })
    expect(studio.context.session.busy).toBe(true)
    // An edit's reply arrives in a later task, so a microtask checkpoint always separates the edit starting from its refusal.
    await settle()
    // The fetch fails and the edit is refused with no yield between them.
    act(() => {
      reject(new Error('GET /api/drawings/rooftop_demo/dxf -> 503'))
      fetchDxf.mockImplementation(async () => answer(2))
      workers[0].emit({ type: 'editApplied', op: 'move', ok: false, reason: 'test refusal' })
    })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(3)
    const posts = workers[0].posted.filter((m) => m.type === 'loadDocument')
    expect(posts[posts.length - 1].documentId).toBe(headDocumentId('rooftop_demo', 2))
    loaded(workers[0], headDocumentId('rooftop_demo', 2))
    expect(studio.context.reach.state).toBe(REACH_STATE.OPEN)
  })

  it('one fetch: disabling and re-enabling during a pending fetch fetches again', async () => {
    let resolveA
    const fetchDxf = vi.fn(() => new Promise((r) => { resolveA = r }))
    const studio = mount({ fetchDxf, sourceKey: 'live' })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(1)
    studio.rerender({ enabled: false, sourceKey: 'sample' })
    fetchDxf.mockImplementation(async () => answer(2))
    studio.rerender({ enabled: true, sourceKey: 'live' })
    await act(async () => { resolveA(answer(1)); await Promise.resolve(); await Promise.resolve() })
    await settle()
    expect(fetchDxf).toHaveBeenCalledTimes(2)
    const posts = loadPosts()
    expect(posts.length).toBeGreaterThan(0)
    expect(posts.some((m) => m.documentId === headDocumentId('rooftop_demo', 1))).toBe(false)
    expect(posts[posts.length - 1].documentId).toBe(headDocumentId('rooftop_demo', 2))
  })
})
