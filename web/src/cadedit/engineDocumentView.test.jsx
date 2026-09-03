// W4f slice A0: the engine document reaches the canvas through the viewer's
// own applyVersion seam while a DXF is open, and the console drawing comes
// back when it closes, the worker dies, or the surface unmounts.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CadEditSurface from './CadEditSurface.jsx'
import EngineDocumentView from './EngineDocumentView.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
  die() { act(() => { this.listeners.get('error')?.({ message: 'worker died' }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0, 0], [100, 50, 0]], radius: null, startDeg: null, endDeg: null }
const CIRCLE = { id: 'e2', type: 'CIRCLE', layer: '0', vertices: [[3, 3, 0]], radius: 1.5, startDeg: null, endDeg: null }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
let viewer
let viewerRef
let onShown
function mount() {
  workers = []
  viewer = { applyVersion: vi.fn() }
  viewerRef = { current: viewer }
  onShown = vi.fn()
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  return render(
    <EngineSessionProvider createWorker={createWorker}>
      <EngineDocumentView viewerRef={viewerRef} onShown={onShown} />
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}

async function openAndLoad(entities, name = 'one.dxf') {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf(name)] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers[workers.length - 1].emit({ type: 'documentLoaded', documentId: name, entities, entityCount: entities.length, unsupported: [] })
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('EngineDocumentView (W4f slice A0)', () => {
  it('shows the engine document once loaded, re-shows on every edit, and never touches the viewer before that', async () => {
    mount()
    expect(viewer.applyVersion).not.toHaveBeenCalled()
    await openAndLoad([LINE, CIRCLE])
    expect(viewer.applyVersion).toHaveBeenCalledTimes(1)
    const intake = viewer.applyVersion.mock.calls[0][0]
    expect(intake.source).toBe('engine')
    expect(intake.documentId).toBe('one.dxf')
    expect(intake.polylines.map((p) => p.handle)).toEqual(['e1', 'e2'])
    expect(intake.polylines[1].closed).toBe(true)
    expect(onShown).toHaveBeenCalledWith(intake)
    // An edit re-parses: a new entity list, a new intake; the same list never re-applies.
    workers[0].emit({ type: 'editApplied', op: 'delete', ok: true, entities: [CIRCLE], entityCount: 1, bytes: new Uint8Array([48]), byteLength: 1 })
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(viewer.applyVersion.mock.calls[1][0].polylines.map((p) => p.handle)).toEqual(['e2'])
  })

  it('hands the console drawing back (null) when the worker dies, and again on unmount only if something was shown', async () => {
    const utils = mount()
    await openAndLoad([LINE])
    expect(viewer.applyVersion).toHaveBeenCalledTimes(1)
    workers[0].die()
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(viewer.applyVersion.mock.calls[1][0]).toBeNull()
    expect(onShown).toHaveBeenLastCalledWith(null)
    utils.unmount()
    // Nothing shown at unmount time: no third call.
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
  })

  it('restores the console drawing on unmount while a document is showing, and tolerates a viewer without applyVersion', async () => {
    const utils = mount()
    await openAndLoad([LINE])
    utils.unmount()
    expect(viewer.applyVersion).toHaveBeenCalledTimes(2)
    expect(viewer.applyVersion.mock.calls[1][0]).toBeNull()
    // A ref with no viewer yet: nothing thrown, nothing shown.
    workers = []
    viewer = {}
    viewerRef = { current: null }
    const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
    render(
      <EngineSessionProvider createWorker={createWorker}>
        <EngineDocumentView viewerRef={viewerRef} />
        <CadEditSurface enabled />
      </EngineSessionProvider>,
    )
    await openAndLoad([LINE], 'two.dxf')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
  })
})
