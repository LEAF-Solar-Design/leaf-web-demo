/**
 * engineSession acceptance — the session-model states the frozen contract
 * requires before any dock work (docs/convergence/ACCEPTANCE.md,
 * "Engine-session ownership"):
 *
 *   selection identity across an edit · optimistic vs reparsed geometry ·
 *   save completion · worker crash recovery · drawing switch mid-edit ·
 *   engine-truth readouts only for engine-parsed documents.
 *
 * The worker here is a SCRIPTED TRANSPORT double, not a fake engine: the test
 * decides what the worker answers, and everything asserted is the store's own
 * state machine. The REAL engine is exercised where it already is —
 * cadEditSurface.test.jsx drives the actual compiled wasm through the real
 * worker module, gated on the machine-local wasm-pack artifact. These specs
 * run everywhere, including where that artifact is absent, which is exactly
 * why the store's states need their own oracle.
 *
 * Every message shape below is one EngineBoundary validates and forwards
 * (web/src/cad/engineWorker.js): a spec that could only pass through an
 * unvalidated channel would prove nothing about the shipped path.
 */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import useEngineSession, {
  GEOMETRY_SOURCE,
  MAX_DOCUMENT_BYTES,
  SESSION_ERROR,
  buildEditPayload,
  surviveSelection,
} from './engineSession.js'

afterEach(cleanup)

// Worker-shaped transport the test drives by hand. `emit` delivers a message
// the way a real worker would; `die` fires the Worker API's own error event,
// which is how a crashed worker actually reports itself.
class ScriptedWorker {
  constructor() {
    this.posted = []
    this.listeners = { message: [], error: [], messageerror: [] }
    this.terminated = false
  }

  addEventListener(type, cb) {
    if (this.listeners[type]) this.listeners[type].push(cb)
  }

  removeEventListener() {}

  postMessage(data) { this.posted.push(data) }

  terminate() { this.terminated = true }

  emit(data) {
    act(() => { this.listeners.message.forEach((cb) => cb({ data })) })
  }

  die() {
    act(() => { this.listeners.error.forEach((cb) => cb({ type: 'error' })) })
  }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }
const POLY = { id: 'e2', type: 'LWPOLYLINE', layer: 'Outline', vertices: [[0, 0], [1, 0], [1, 1]] }

function loadedMessage(entities, documentId = 'one.dxf', unsupported = []) {
  return {
    type: 'documentLoaded',
    documentId,
    entities,
    entityCount: entities.length,
    unsupported,
  }
}

function editedMessage(op, entities, bytes = new Uint8Array([1, 2, 3])) {
  return {
    type: 'editApplied',
    op,
    ok: true,
    entities,
    entityCount: entities.length,
    bytes,
    byteLength: bytes.length,
  }
}

function fileOf(name = 'one.dxf', text = '0\nEOF\n') {
  const bytes = new TextEncoder().encode(text)
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

function oversizedFile() {
  const file = new File([new Uint8Array(1)], 'huge.dxf')
  file.arrayBuffer = async () => new ArrayBuffer(1)
  Object.defineProperty(file, 'size', { value: MAX_DOCUMENT_BYTES + 1 })
  return file
}

// Mounts the real hook and hands back a live handle to its latest value.
function mountSession(options = {}) {
  const workers = []
  const createWorker = options.createWorker || vi.fn(() => {
    const worker = new ScriptedWorker()
    workers.push(worker)
    return worker
  })
  const handle = { current: null, workers, createWorker }
  function Host({ drawingId, saveTarget, onSaved }) {
    handle.current = useEngineSession({ createWorker, drawingId, saveTarget, onSaved })
    return null
  }
  const view = render(<Host
    drawingId={options.drawingId ?? null}
    saveTarget={options.saveTarget ?? null}
    onSaved={options.onSaved ?? null}
  />)
  handle.rerender = (next) => view.rerender(<Host
    drawingId={next.drawingId ?? null}
    saveTarget={next.saveTarget ?? options.saveTarget ?? null}
    onSaved={next.onSaved ?? options.onSaved ?? null}
  />)
  handle.unmount = view.unmount
  return handle
}

async function openDocument(session, file = fileOf()) {
  await act(async () => { await session.current.actions.open(file) })
}

describe('worker lifetime is the store\'s', () => {
  it('never spawns the engine worker at mount — only on the first open', async () => {
    const session = mountSession()
    expect(session.createWorker).not.toHaveBeenCalled()
    await openDocument(session)
    expect(session.createWorker).toHaveBeenCalledTimes(1)
    expect(session.workers[0].posted[0]).toEqual({ type: 'init' })
    expect(session.workers[0].posted[1].type).toBe('loadDocument')
  })

  it('refuses a file over the byte cap without reading or spawning anything', async () => {
    const session = mountSession()
    await openDocument(session, oversizedFile())
    expect(session.current.status).toContain('exceeds')
    expect(session.current.errorKind).toBe(SESSION_ERROR.LIMIT)
    expect(session.createWorker).not.toHaveBeenCalled()
  })

  it('terminates the worker on unmount', async () => {
    const session = mountSession()
    await openDocument(session)
    const worker = session.workers[0]
    session.unmount()
    expect(worker.terminated).toBe(true)
  })

  it('refuses to construct without an injected worker factory (the fence keeps the path at the call site)', () => {
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    function Bad() { useEngineSession({}); return null }
    expect(() => render(<Bad />)).toThrow(/createWorker/)
    quiet.mockRestore()
  })
})

describe('engine-truth readouts only for engine-parsed documents', () => {
  it('starts NOT engine-parsed, with no geometry source', () => {
    const session = mountSession()
    expect(session.current.engineParsed).toBe(false)
    expect(session.current.geometrySource).toBeNull()
    expect(session.current.entityCount).toBe(0)
  })

  it('a load seats the engine\'s parse of the supplied bytes', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE, POLY]))
    expect(session.current.engineParsed).toBe(true)
    expect(session.current.geometrySource).toBe(GEOMETRY_SOURCE.ENGINE_PARSE)
    expect(session.current.reparsed).toBe(false)
    expect(session.current.entityCount).toBe(2)
    expect(session.current.status).toBe('Loaded one.dxf: 2 entities.')
  })

  it('names the read-only kinds a load preserved', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE], 'one.dxf', [{ type: 'CIRCLE' }]))
    expect(session.current.status).toBe('Loaded one.dxf: 1 entities (1 preserved as read-only kinds).')
  })

  it('an engine refusal clears the list AND the engine-parsed claim, rather than showing an empty document', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    session.workers[0].emit({ type: 'error', message: 'bad_document_bytes' })
    expect(session.current.status).toBe('Engine refused: bad_document_bytes')
    expect(session.current.errorKind).toBe(SESSION_ERROR.ENGINE)
    expect(session.current.engineParsed).toBe(false)
    expect(session.current.geometrySource).toBeNull()
    expect(session.current.entities).toEqual([])
  })
})

describe('optimistic vs reparsed geometry', () => {
  it('an applied edit reports the RE-PARSE of the written bytes, never an optimistic list', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE, POLY]))
    act(() => session.current.actions.select('e1'))
    act(() => session.current.actions.applyEdit('move', { dx: '7', dy: '3' }))
    expect(session.current.busy).toBe(true)
    session.workers[0].emit(editedMessage('move', [{ ...LINE, vertices: [[7, 3], [8, 4]] }, POLY]))

    expect(session.current.geometrySource).toBe(GEOMETRY_SOURCE.ENGINE_REPARSE)
    expect(session.current.reparsed).toBe(true)
    expect(session.current.geometrySource).not.toBe(GEOMETRY_SOURCE.OPTIMISTIC)
    expect(session.current.busy).toBe(false)
    expect(session.current.status).toBe(
      'move applied. Re-parsed from the written bytes: 2 entities, 3 bytes.')
    expect(session.current.savedBytes).toEqual(new Uint8Array([1, 2, 3]))
  })

  it('a refused edit keeps the document standing and reports the typed reason', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    act(() => session.current.actions.select('e1'))
    act(() => session.current.actions.applyEdit('deleteVertex', { vertexIndex: '0' }))
    session.workers[0].emit({
      type: 'editApplied', op: 'deleteVertex', ok: false, reason: 'line_has_fixed_endpoints',
    })
    expect(session.current.status).toBe('Edit refused (deleteVertex): line_has_fixed_endpoints')
    expect(session.current.errorKind).toBe(SESSION_ERROR.REFUSED)
    expect(session.current.entityCount).toBe(1)
    expect(session.current.geometrySource).toBe(GEOMETRY_SOURCE.ENGINE_PARSE)
  })
})

describe('selection identity across an edit', () => {
  it('SURVIVES when the selected entity survives the re-parse', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE, POLY]))
    act(() => session.current.actions.select('e2'))
    act(() => session.current.actions.applyEdit('setLayer', { layer: 'Renamed' }))
    session.workers[0].emit(editedMessage('setLayer', [LINE, { ...POLY, layer: 'Renamed' }]))
    expect(session.current.selectedId).toBe('e2')
    expect(session.current.selected.layer).toBe('Renamed')
  })

  it('CLEARS when the selected entity was deleted by the edit', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE, POLY]))
    act(() => session.current.actions.select('e1'))
    act(() => session.current.actions.applyEdit('delete', {}))
    session.workers[0].emit(editedMessage('delete', [POLY]))
    expect(session.current.selectedId).toBe('')
    expect(session.current.selected).toBeNull()
  })

  it('a load always clears the selection — a new document shares no ids', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    act(() => session.current.actions.select('e1'))
    session.workers[0].emit(loadedMessage([LINE, POLY], 'two.dxf'))
    expect(session.current.selectedId).toBe('')
  })

  it('the survival rule itself', () => {
    expect(surviveSelection('e1', [LINE, POLY])).toBe('e1')
    expect(surviveSelection('e1', [POLY])).toBe('')
    expect(surviveSelection('', [LINE])).toBe('')
    expect(surviveSelection('e1', undefined)).toBe('')
  })
})

describe('edit dispatch refuses malformed input before it reaches the engine', () => {
  const cases = [
    ['move', { dx: 'nope', dy: '0' }, 'Move refused: dx and dy must both be numbers.'],
    ['moveVertex', { vertexIndex: '-1', dx: '1', dy: '1' }, 'moveVertex refused: vertex must be a non-negative integer.'],
    ['moveVertex', { vertexIndex: '0', dx: 'x', dy: '1' }, 'Move vertex refused: dx and dy must both be numbers.'],
    ['addVertex', { vertexIndex: '0', dx: 'x', dy: '1' }, 'Add vertex refused: x and y must both be numbers.'],
    ['deleteVertex', { vertexIndex: 'abc' }, 'deleteVertex refused: vertex must be a non-negative integer.'],
    ['setLayer', { layer: '   ' }, 'Set layer refused: enter a layer name.'],
  ]

  for (const [op, inputs, expected] of cases) {
    it(`${op}: ${expected}`, async () => {
      const session = mountSession()
      await openDocument(session)
      session.workers[0].emit(loadedMessage([LINE]))
      act(() => session.current.actions.select('e1'))
      const before = session.workers[0].posted.length
      act(() => session.current.actions.applyEdit(op, inputs))
      expect(session.current.status).toBe(expected)
      expect(session.workers[0].posted.length).toBe(before)
    })
  }

  it('dispatches nothing at all with no selection', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    const before = session.workers[0].posted.length
    act(() => session.current.actions.applyEdit('delete', {}))
    expect(session.workers[0].posted.length).toBe(before)
    expect(session.current.status).toBe('Loaded one.dxf: 1 entities.')
  })

  it('builds the exact payload the boundary schema carries', () => {
    expect(buildEditPayload('move', 'e1', { dx: '2.5', dy: '-3' }))
      .toEqual({ payload: { entityId: 'e1', dx: 2.5, dy: -3 } })
    expect(buildEditPayload('addVertex', 'e1', { vertexIndex: '2', dx: '1', dy: '4' }))
      .toEqual({ payload: { entityId: 'e1', vertexIndex: 2, x: 1, y: 4 } })
    expect(buildEditPayload('setLayer', 'e1', { layer: '  Panels ' }))
      .toEqual({ payload: { entityId: 'e1', layer: 'Panels' } })
    expect(buildEditPayload('delete', 'e1', {})).toEqual({ payload: { entityId: 'e1' } })
  })
})

describe('save completion', () => {
  const receipt = {
    new_version: { drawing_id: 'rooftop', version: 5, parent: 4 },
    head: 5,
    source_sha256: 'abcdef0123456789',
    cost: { engine_usd: 0 },
  }

  async function editedSession(saveTarget, onSaved) {
    const session = mountSession({ saveTarget, onSaved })
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    act(() => session.current.actions.select('e1'))
    act(() => session.current.actions.applyEdit('move', { dx: '1', dy: '1' }))
    session.workers[0].emit(editedMessage('move', [LINE]))
    return session
  }

  it('posts the EXACT edited bytes with a client-computed digest and records the version receipt', async () => {
    const save = vi.fn(async (bytes, parent, digest) => {
      expect(bytes).toEqual(new Uint8Array([1, 2, 3]))
      expect(parent).toBe(4)
      expect(digest).toMatch(/^[0-9a-f]{64}$/)
      return receipt
    })
    const onSaved = vi.fn()
    const session = await editedSession({ headVersion: 4, save }, onSaved)
    await act(async () => { await session.current.actions.save() })

    expect(save).toHaveBeenCalledTimes(1)
    expect(onSaved).toHaveBeenCalledWith(receipt)
    expect(session.current.savedVersion).toBe(5)
    expect(session.current.receipt).toBe(receipt)
    expect(session.current.status).toContain('Saved as version 5 (parent 4)')
    expect(session.current.status).toContain('engine cost $0')
    expect(session.current.busy).toBe(false)
  })

  it('a moved head (409) reads back as a refusal, not a success', async () => {
    const conflict = Object.assign(new Error('stale parent 4: head is now 6; refresh'), { status: 409 })
    const session = await editedSession({ headVersion: 4, save: async () => { throw conflict } })
    await act(async () => { await session.current.actions.save() })
    expect(session.current.status).toBe('Save refused: stale parent 4: head is now 6; refresh')
    expect(session.current.errorKind).toBe(SESSION_ERROR.SAVE)
    expect(session.current.receipt).toBeNull()
    expect(session.current.savedVersion).toBeNull()
  })

  it('two clicks in one tick write ONE version — `busy` alone cannot gate that', async () => {
    // Both calls are issued before any render can flip `busy`, which is
    // exactly the double-click window. The in-flight latch is what makes the
    // second one a no-op; without it the server takes two version writes.
    const save = vi.fn(async () => receipt)
    const session = await editedSession({ headVersion: 4, save })
    await act(async () => {
      await Promise.all([session.current.actions.save(), session.current.actions.save()])
    })
    expect(save).toHaveBeenCalledTimes(1)
    expect(session.current.savedVersion).toBe(5)
  })

  it('will not save with no target, and will not save unedited bytes', async () => {
    const save = vi.fn()
    const session = mountSession({ saveTarget: { headVersion: 1, save } })
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    await act(async () => { await session.current.actions.save() })
    expect(save).not.toHaveBeenCalled()
  })
})

describe('worker crash is a RECOVERABLE state', () => {
  it('reports the crash, drops the dead worker, and respawns a fresh one on the next open', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE, POLY]))
    act(() => session.current.actions.select('e1'))

    session.workers[0].die()

    expect(session.current.errorKind).toBe(SESSION_ERROR.CRASHED)
    expect(session.current.recoverable).toBe(true)
    expect(session.current.status).toContain('Engine stopped unexpectedly')
    // No list you cannot edit, and no claim of engine truth without a session.
    expect(session.current.entities).toEqual([])
    expect(session.current.selectedId).toBe('')
    expect(session.current.engineParsed).toBe(false)
    expect(session.workers[0].terminated).toBe(true)
    // The document name survives so the message can be acted on.
    expect(session.current.documentId).toBe('one.dxf')

    await openDocument(session, fileOf('again.dxf'))
    expect(session.createWorker).toHaveBeenCalledTimes(2)
    expect(session.workers[1].posted[0]).toEqual({ type: 'init' })
    session.workers[1].emit(loadedMessage([LINE], 'again.dxf'))
    expect(session.current.errorKind).toBeNull()
    expect(session.current.engineParsed).toBe(true)
  })

  it('a message from the DEAD worker after the crash never seats state', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    session.workers[0].die()
    session.workers[0].emit(editedMessage('move', [LINE, POLY]))
    expect(session.current.entities).toEqual([])
    expect(session.current.errorKind).toBe(SESSION_ERROR.CRASHED)
  })
})

describe('drawing switch mid-edit', () => {
  it('resets the session and tears the worker down — no cross-document bleed', async () => {
    const session = mountSession({ drawingId: 'drawing-a' })
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE, POLY]))
    act(() => session.current.actions.select('e1'))
    act(() => session.current.actions.applyEdit('move', { dx: '1', dy: '1' }))
    session.workers[0].emit(editedMessage('move', [LINE, POLY]))
    expect(session.current.savedBytes).not.toBeNull()

    act(() => { session.rerender({ drawingId: 'drawing-b' }) })

    expect(session.current.documentId).toBe('')
    expect(session.current.entities).toEqual([])
    expect(session.current.entityCount).toBe(0)
    expect(session.current.selectedId).toBe('')
    expect(session.current.savedBytes).toBeNull()
    expect(session.current.engineParsed).toBe(false)
    expect(session.current.geometrySource).toBeNull()
    expect(session.current.status).toBe('')
    expect(session.workers[0].terminated).toBe(true)
  })

  it('an in-flight reply from the OLD document never seats onto the new one', async () => {
    const session = mountSession({ drawingId: 'drawing-a' })
    await openDocument(session)
    const stale = session.workers[0]
    act(() => { session.rerender({ drawingId: 'drawing-b' }) })
    stale.emit(loadedMessage([LINE, POLY], 'from-drawing-a.dxf'))
    expect(session.current.documentId).toBe('')
    expect(session.current.entities).toEqual([])
  })

  it('an in-flight FILE READ from the old document is abandoned, never posted', async () => {
    const session = mountSession({ drawingId: 'drawing-a' })
    let releaseRead
    const slow = fileOf('slow.dxf')
    slow.arrayBuffer = () => new Promise((resolve) => { releaseRead = () => resolve(new ArrayBuffer(4)) })

    let pending
    act(() => { pending = session.current.actions.open(slow) })
    act(() => { session.rerender({ drawingId: 'drawing-b' }) })
    await act(async () => { releaseRead(); await pending })

    // The read resolved into a session that no longer wants it: no worker was
    // ever asked to load bytes from the abandoned document.
    expect(session.createWorker).not.toHaveBeenCalled()
    expect(session.current.documentId).toBe('')
  })

  it('the FIRST drawing id is not a switch — it must not clear a session mid-open', async () => {
    const session = mountSession({ drawingId: null })
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    act(() => { session.rerender({ drawingId: null }) })
    expect(session.current.entityCount).toBe(1)
  })

  it('reset() clears everything a switch would', async () => {
    const session = mountSession()
    await openDocument(session)
    session.workers[0].emit(loadedMessage([LINE]))
    act(() => session.current.actions.reset())
    expect(session.current.entities).toEqual([])
    expect(session.current.engineParsed).toBe(false)
    expect(session.workers[0].terminated).toBe(true)
  })
})
