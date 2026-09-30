import { useState } from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DrawingObjectsProvider, useDrawingObjects } from './DrawingObjectsContext.jsx'
import { buildDrawingObjectIndex } from '../lib/drawingObjectIndex.js'
import EngineSessionProvider, { useEngineSessionContext } from '../cadedit/EngineSessionProvider.jsx'
import EngineObjectBridge from '../cadedit/EngineObjectBridge.jsx'
import EngineDocumentView from '../cadedit/EngineDocumentView.jsx'

afterEach(cleanup)
const shape = (handle = 'A', pts = [[0, 0], [10, 10]]) => ({ handle, layer: 'Panels', pts })
const indexOf = (drawingKey = 'console:a', polylines = [shape()]) => {
  const intake = { polylines }
  return buildDrawingObjectIndex({ drawingKey, intake })
}
function fakeViewer(index) {
  const scene = { ready: true, drawingKey: index?.drawingKey, intake: index?.intake }
  const listeners = new Set()
  return {
    scene, frame: vi.fn(() => true), setFocusMarker: vi.fn(() => true), canSetFocusMarker: vi.fn(() => true),
    getDrawingScene: () => scene,
    subscribeCamera: (fn) => { listeners.add(fn); fn({ pose: null, viewport: null }); return () => listeners.delete(fn) },
    emit: () => listeners.forEach((fn) => fn({ pose: {}, viewport: {} })),
    applyVersion: vi.fn((intake) => { Object.assign(scene, { ready: true, intake, drawingKey: intake ? `engine:${intake.documentId}` : null }) }),
  }
}
function setup(index = indexOf()) {
  const api = fakeViewer(index), viewerRef = { current: api }
  let ctx
  function Probe() { ctx = useDrawingObjects(); return null }
  const mounted = render(<DrawingObjectsProvider viewerRef={viewerRef}><Probe /></DrawingObjectsProvider>)
  act(() => { ctx.publish('console', { index }); ctx.publishSelection('console', ['B']) })
  return { api, viewerRef, get: () => ctx, ...mounted }
}

it('frames focus without changing selection and makes retained callbacks inert after unmount', () => {
  const { api, get, unmount } = setup()
  act(() => { expect(get().focus('h:A')).toEqual({ ok: true }) })
  expect(api.frame).toHaveBeenCalledWith(get().index.byId.get('h:A').bounds)
  expect(api.setFocusMarker).toHaveBeenLastCalledWith(get().index.byId.get('h:A').bounds)
  expect(get().focusId).toBe('h:A'); expect(get().selectedHandles).toEqual(['B'])
  const retained = get(); unmount()
  api.frame.mockClear(); api.setFocusMarker.mockClear()
  retained.focus('h:A'); retained.clearFocus(); retained.publish('engine', { index: indexOf() }); retained.retract('console')
  expect(api.frame).not.toHaveBeenCalled(); expect(api.setFocusMarker).not.toHaveBeenCalled()
})

it.each(['unknown id', 'no bounds', 'viewer not ready', "scene not showing this index's drawing", 'frame refused'])('refuses %s with camera, focus and selection unchanged', (reason) => {
  const index = indexOf('console:a', [shape(), { handle: 'B', layer: 'Panels' }])
  const { api, get, viewerRef } = setup(index)
  act(() => { get().focus('h:A') })
  api.frame.mockClear(); api.setFocusMarker.mockClear()
  let id = 'h:A'
  if (reason === 'unknown id') id = 'missing'
  if (reason === 'no bounds') id = 'h:B'
  if (reason === 'viewer not ready') viewerRef.current = null
  if (reason === "scene not showing this index's drawing") api.scene.drawingKey = 'elsewhere'
  if (reason === 'frame refused') api.frame.mockReturnValue(false)
  act(() => { expect(get().focus(id)).toEqual({ ok: false, reason }) })
  expect(api.frame).toHaveBeenCalledTimes(reason === 'frame refused' ? 1 : 0)
  expect(api.setFocusMarker).not.toHaveBeenCalled()
  expect(get().focusId).toBe('h:A'); expect(get().selectedHandles).toEqual(['B'])
})

it('preflights marker support and refuses a pending rebuild or mismatched intake', () => {
  const { api, get } = setup()
  api.canSetFocusMarker.mockReturnValue(false)
  expect(get().focus('h:A').ok).toBe(false)
  api.canSetFocusMarker.mockReturnValue(true)
  api.scene.ready = false
  expect(get().focus('h:A').ok).toBe(false)
  api.scene.ready = true; api.scene.intake = {}
  expect(get().focus('h:A').ok).toBe(false)
  delete api.setFocusMarker
  expect(get().focus('h:A').ok).toBe(false)
  expect(api.frame).not.toHaveBeenCalled()
})

it('clears drawing changes and same-drawing deletions, but refreshes changed bounds without reframing', () => {
  const { api, get } = setup()
  act(() => { get().focus('h:A') })
  const changed = indexOf('console:a', [shape('A', [[20, 30], [40, 50]])])
  Object.assign(api.scene, { intake: changed.intake })
  act(() => { get().publish('console', { index: changed }) })
  expect(get().focusId).toBe('h:A')
  expect(api.setFocusMarker).toHaveBeenLastCalledWith(changed.byId.get('h:A').bounds)
  expect(api.frame).toHaveBeenCalledTimes(1)
  act(() => { get().publish('console', { index: indexOf('console:a', []) }) })
  expect(get().focusId).toBeNull(); expect(api.setFocusMarker).toHaveBeenLastCalledWith(null)
  Object.assign(api.scene, { intake: changed.intake })
  act(() => { get().publish('console', { index: changed }); get().focus('h:A') })
  act(() => { get().publish('console', { index: indexOf('console:b') }) })
  expect(get().focusId).toBeNull(); expect(api.setFocusMarker).toHaveBeenLastCalledWith(null)
})

it('clears focus if an existing record loses bounds', () => {
  const { get, api } = setup()
  act(() => { get().focus('h:A'); get().publish('console', { index: indexOf('console:a', [{ handle: 'A' }]) }) })
  expect(get().focusId).toBeNull(); expect(api.setFocusMarker).toHaveBeenLastCalledWith(null)
})

it('old publisher cleanup cannot erase a newer publication; retract restores fallback selection', async () => {
  const { get } = setup(), first = indexOf('engine:first'), second = indexOf('engine:second')
  let cleanupOld, cleanupNew
  act(() => { cleanupOld = get().publish('engine', { index: first }); get().publishSelection('engine', ['A']) })
  act(() => { cleanupNew = get().publish('engine', { index: second }) })
  await act(async () => { cleanupOld() })
  expect(get().index).toBe(second)
  await act(async () => { cleanupNew() })
  expect(get().index.drawingKey).toBe('console:a'); expect(get().selectedHandles).toEqual(['B'])
})

class Worker {
  constructor() { this.listeners = new Map(); this.posted = [] }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
  crash() { act(() => { this.listeners.get('error')?.({ message: 'worker died' }) }) }
}
const entities = [{ id: '42', type: 'LINE', layer: 'Panels', vertices: [[0, 0, 0], [10, 10, 0]], aci: 256, linetype: 'ByLayer', lineweight: -1 }]
function integration({ attached = true } = {}) {
  const api = fakeViewer(), viewerRef = { current: attached ? api : null }, worker = new Worker()
  const createWorker = () => worker
  let objects, engine
  function Probe() { objects = useDrawingObjects(); engine = useEngineSessionContext(); return null }
  function Contents({ active }) {
    const [intake, setIntake] = useState(null)
    return <><Probe /><EngineDocumentView viewerRef={viewerRef} onShown={(value) => setIntake(value)} />
      {active !== null && <EngineObjectBridge intake={intake} active={active} />}</>
  }
  const tree = (active) => <DrawingObjectsProvider viewerRef={viewerRef}>
    <EngineSessionProvider createWorker={createWorker}><Contents active={active} /></EngineSessionProvider>
  </DrawingObjectsProvider>
  const mounted = render(tree(true))
  act(() => { objects.publish('console', { index: indexOf() }); engine.session.actions.openBytes(new Uint8Array([0]), 'guest.dxf') })
  worker.emit({ type: 'documentLoaded', documentId: 'guest.dxf', entities, entityCount: entities.length, unsupported: [] })
  return { api, viewerRef, worker, objects: () => objects, engine: () => engine, ...mounted, rerender: (active = true) => mounted.rerender(tree(active)) }
}

it('real engine selection-only updates preserve the index and a staged property edit through focus and index updates', () => {
  const h = integration(), initial = h.objects().index
  act(() => { h.engine().session.actions.selectReplace(['42']) })
  expect(h.objects().index).toBe(initial); expect(h.objects().selectedHandles).toEqual(['2A'])
  act(() => { h.engine().setPending({ op: 'setColor', value: 'red', targetId: '42', label: 'LINE 2A' }) })
  const pending = h.engine().pending
  expect(pending).not.toBeNull()
  act(() => { expect(h.objects().focus('h:2A')).toEqual({ ok: true }) })
  act(() => {
    h.objects().publish('engine', { index: buildDrawingObjectIndex({ drawingKey: initial.drawingKey, entities, intake: initial.intake }) })
  })
  expect(h.engine().pending).toBe(pending); expect(h.engine().session.selectedIds).toEqual(['42'])
  expect(h.worker.posted.filter((m) => m.type === 'applyEdit')).toEqual([])
})

it('a guest engine drawing can open before the lazy viewer attaches', () => {
  const h = integration({ attached: false })
  expect(h.objects().index.drawingKey).toBe('engine:guest.dxf')
  expect(h.objects().focus('h:2A')).toEqual({ ok: false, reason: 'viewer not ready' })
  h.viewerRef.current = h.api; h.rerender()
  act(() => { expect(h.objects().focus('h:2A')).toEqual({ ok: true }) })
  expect(h.api.applyVersion).toHaveBeenCalled()
})

it.each(['close', 'crash', 'surface', 'bridge unmount'])('real engine %s restores the console fallback', async (mode) => {
  const h = integration()
  expect(h.objects().index.drawingKey).toBe('engine:guest.dxf')
  await act(async () => {
    if (mode === 'close') h.engine().session.actions.reset()
    else if (mode === 'crash') h.worker.crash()
    else h.rerender(mode === 'surface' ? false : null)
  })
  expect(h.objects().index.drawingKey).toBe('console:a')
})
