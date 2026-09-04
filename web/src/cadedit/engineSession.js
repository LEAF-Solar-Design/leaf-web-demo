/**
 * engineSession — the ONE engine-session store (convergence W1,
 * docs/convergence/ACCEPTANCE.md "Engine-session ownership", binding).
 *
 * Extracted from CadEditSurface.jsx, which is now the (still only) CONSUMER.
 * The store owns, and is the only thing that owns: EngineBoundary
 * construction, worker lifetime, document bytes and documentId, the entity
 * list, selection identity, edit dispatch (delete / translate / move-vertex /
 * add-vertex / delete-vertex / set-layer), the save-as-version flow, the
 * re-parse-written-bytes truth, and the busy / error / refusal states. The
 * PropertiesDock and every other cockpit surface CONSUME this store; none of
 * them may construct a second EngineBoundary.
 *
 * LICENSE FENCE (docs/CAD-ENGINE-LICENSE-FENCE.md, deny rule 3). This module
 * NEVER names the engine worker path. `createWorker` is a REQUIRED injected
 * dependency, so the one fence-legal `new Worker(new URL(..., import.meta.url))`
 * spawn site stays exactly where it is today — in CadEditSurface.jsx — and
 * this extraction adds no second legal site to bless. Every engine MESSAGE
 * still crosses EngineBoundary, unmodified, which stays the sole
 * schema-validating channel in both directions. The only thing this module
 * attaches to the raw worker handle is a DEATH WATCH (`error` /
 * `messageerror`), because worker LIFETIME is this store's by contract and
 * lifetime includes death; no engine payload is read there.
 *
 * HARDENING CONTRACT, stated so it survives the next edit:
 *   * Every async leg carries a generation token. A file read, or a save,
 *     that resolves after a reset or a drawing switch is ABANDONED — it never
 *     seats bytes from a document that is no longer open (no cross-document
 *     bleed).
 *   * The byte cap is checked against File.size BEFORE any read, so an
 *     oversized file costs a comparison, never a decode, and never a spawn.
 *   * Malformed edit inputs are refused in this module, before the boundary,
 *     so a bad delta is a typed refusal instead of an engine round trip.
 *   * The worker is spawned lazily on the first open, never at mount.
 *   * State is ONE frozen record behind ONE setState: a transition costs one
 *     render, not one per field.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { EngineBoundary } from '../cad/engineWorker.js'
import { SESSION_ERROR } from './engineSessionErrors.js'

// Mirrors the worker's own bound. Checked against File.size BEFORE any read.
export const MAX_DOCUMENT_BYTES = 16 * 1024 * 1024

/**
 * Which geometry the entity list currently describes — the ACCEPTANCE
 * "optimistic vs reparsed geometry" state, exposed rather than implied.
 *
 *   ENGINE_PARSE   the engine's parse of the bytes we HANDED it (a load)
 *   ENGINE_REPARSE the engine's re-parse of the bytes it WROTE (an edit) —
 *                  what a reader of the saved file would actually see
 *   OPTIMISTIC     geometry this client predicted and the engine has not
 *                  confirmed. No path produces it today; the value exists so
 *                  a consumer can already distinguish it rather than assume
 *                  every list is engine truth.
 */
export const GEOMETRY_SOURCE = Object.freeze({
  OPTIMISTIC: 'optimistic',
  ENGINE_PARSE: 'engine-parse',
  ENGINE_REPARSE: 'engine-reparse',
})

/** Typed session errors. `CRASHED` is recoverable: open a document again. */
// The typed error kinds live in their own module so web/src/lib/actionRegistry.js
// (React-free by contract) can read them without importing this hook module.
// Re-exported here unchanged: every existing importer keeps its path.
export { SESSION_ERROR } from './engineSessionErrors.js'

const NO_ENTITIES = Object.freeze([])

const INITIAL_SESSION = Object.freeze({
  documentId: '',
  entities: NO_ENTITIES,
  entityCount: 0,
  selectedId: '',
  status: '',
  savedBytes: null,
  // W4g-1b: the bytes the last successful save committed (the same reference
  // `savedBytes` held then). `dirty` below is savedBytes !== committedBytes:
  // an undo back to the committed snapshot reads clean by reference.
  committedBytes: null,
  busy: false,
  // Engine-truth gate (ACCEPTANCE): entity/byte readouts render ONLY for a
  // document that actually passed through the engine. There is no setter for
  // this outside a `documentLoaded` message, so a server-loaded intake can
  // never turn it on.
  engineParsed: false,
  geometrySource: null,
  errorKind: null,
  receipt: null,
  savedVersion: null,
  // W4f slice F: how many engine edits can be undone / redone right now.
  // The snapshots themselves (whole-document bytes) live in refs, never in
  // React state; these counts are what the affordances read.
  undoDepth: 0,
  redoDepth: 0,
})

const CRASH_STATUS = 'Engine stopped unexpectedly. Open a drawing again to restart it.'

// W4f slice F: undo is a bytes-snapshot stack. Every applied edit re-parses
// the WHOLE written document and hands the bytes back, so the state before an
// edit is exactly the bytes that were current then, and undoing is re-loading
// them through the same loadDocument path the open uses. Bounded by total
// bytes, not count: a 16 MB document would make fifty snapshots 800 MB.
export const MAX_UNDO_BYTES = 64 * 1024 * 1024

function trimSnapshots(stack, limit = MAX_UNDO_BYTES) {
  let total = 0
  for (const snap of stack) total += snap.bytes.length
  while (stack.length > 1 && total > limit) {
    total -= stack.shift().bytes.length
  }
  return stack
}

// W4f-9: an operand is a decimal literal or nothing. Number.parseFloat read
// "10abc" as 10 and "1,5" as 1, so a typo drew a wrong line instead of a
// refusal; the strict reading (the same grammar the prompt's point
// expressions use) fails closed on anything that is not a finite number.
const DECIMAL_LITERAL = /^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/
export function readNumber(raw) {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null
  if (typeof raw !== 'string') return null
  const text = raw.trim()
  if (!text || text.length > 64 || !DECIMAL_LITERAL.test(text)) return null
  const n = Number(text)
  return Number.isFinite(n) ? n : null
}

function fmtDelta(raw) {
  return readNumber(raw)
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

/**
 * Selection identity across an edit (ACCEPTANCE, required state): the
 * selection SURVIVES when the entity survives the re-parse, and CLEARS when
 * it does not. Pure so the rule is checkable on its own.
 */
export function surviveSelection(previousId, entities) {
  if (!previousId) return ''
  return (entities || []).some((entity) => entity.id === previousId) ? previousId : ''
}

/** The W4d Draw group's operations: creation needs no selection. */
export const CREATE_OPS = Object.freeze(['createLine', 'createCircle', 'createArc', 'createPolyline', 'createRectangle'])
// W4g-4: edits that MAKE an entity (a displaced copy, a mirrored copy, the
// segments of an explode) report what they made by id like the Draw group
// does; the selection lands on it.
export const CREATING_EDITS = Object.freeze(['copy', 'mirror', 'explode'])
// RECTANG is a closed four-point polyline to the engine: the store lowers it
// before the post, so the worker's op vocabulary is unchanged.
const WORKER_OP = Object.freeze({ createRectangle: 'createPolyline' })

// Client-side bound on a typed point list. The engine bounds harder
// (100,000); past this a "polyline" is a paste, not a drawing gesture.
export const MAX_CREATE_POINTS = 1000

/**
 * "x,y x,y ..." (pairs split on whitespace or ';') -> a flat finite
 * [x0, y0, x1, y1, ...], or null for anything malformed or oversized.
 */
export function parsePointList(raw) {
  const text = String(raw ?? '').trim()
  if (!text) return null
  const points = []
  for (const pair of text.split(/[;\s]+/)) {
    if (!pair) continue
    const parts = pair.split(',')
    if (parts.length !== 2) return null
    const x = readNumber(parts[0])
    const y = readNumber(parts[1])
    if (x === null || y === null) return null
    points.push(x, y)
    if (points.length > MAX_CREATE_POINTS * 2) return null
  }
  return points.length >= 4 ? points : null
}

/**
 * Create-input validation (W4d Draw group), refused here rather than at the
 * engine: `{ payload }` or `{ refusal }` with the operator-facing sentence.
 * The engine re-validates (finite, radius > 0, sweep, bounds) and refuses
 * with a typed reason; this layer exists so a typo costs a sentence, not a
 * round trip.
 */
export function buildCreatePayload(op, { x, y, x2, y2, r, a0, a1, pts, closed, layer } = {}) {
  const layerName = String(layer ?? '').trim()
  if (op === 'createLine') {
    const [x1, y1, xx2, yy2] = [x, y, x2, y2].map(fmtDelta)
    if ([x1, y1, xx2, yy2].some((v) => v === null)) return { refusal: 'Line refused: x, y, x2 and y2 must all be numbers.' }
    if (x1 === xx2 && y1 === yy2) return { refusal: 'Line refused: the two points must differ.' }
    return { payload: { x1, y1, x2: xx2, y2: yy2, layer: layerName } }
  }
  if (op === 'createCircle') {
    const [cx, cy, radius] = [x, y, r].map(fmtDelta)
    if ([cx, cy, radius].some((v) => v === null)) return { refusal: 'Circle refused: x, y and r must all be numbers.' }
    if (radius <= 0) return { refusal: 'Circle refused: r must be greater than 0.' }
    return { payload: { cx, cy, radius, layer: layerName } }
  }
  if (op === 'createArc') {
    const [cx, cy, radius, startDeg, endDeg] = [x, y, r, a0, a1].map(fmtDelta)
    if ([cx, cy, radius, startDeg, endDeg].some((v) => v === null)) {
      return { refusal: 'Arc refused: x, y, r, start and end must all be numbers.' }
    }
    if (radius <= 0) return { refusal: 'Arc refused: r must be greater than 0.' }
    if ((endDeg - startDeg) % 360 === 0) return { refusal: 'Arc refused: start and end must differ (degrees).' }
    return { payload: { cx, cy, radius, startDeg, endDeg, layer: layerName } }
  }
  if (op === 'createPolyline') {
    const points = parsePointList(pts)
    if (!points) return { refusal: `Polyline refused: enter at least two points as x,y pairs (at most ${MAX_CREATE_POINTS}).` }
    return { payload: { points, closed: closed === true || closed === 'true', layer: layerName } }
  }
  if (op === 'createRectangle') {
    // W4g-4 RECTANG: two opposite corners -> the closed polyline the engine
    // draws (corner, corner, corner, corner). A zero-width or zero-height
    // rectangle is a line, not a rectangle, and is refused.
    const [x1, y1, xx2, yy2] = [x, y, x2, y2].map(fmtDelta)
    if ([x1, y1, xx2, yy2].some((v) => v === null)) return { refusal: 'Rectangle refused: x, y, x2 and y2 must all be numbers.' }
    if (x1 === xx2 || y1 === yy2) return { refusal: 'Rectangle refused: the corners must differ in both x and y.' }
    return { payload: { points: [x1, y1, xx2, y1, xx2, yy2, x1, yy2], closed: true, layer: layerName } }
  }
  return { refusal: `Draw refused: unknown operation ${op}.` }
}

/**
 * Edit-input validation, refused here rather than at the engine. Returns
 * either `{ payload }` or `{ refusal }` with the exact operator-facing
 * sentence — never both, never a throw.
 */
export function buildEditPayload(op, entityId, { dx, dy, vertexIndex, layer, x1, y1, x2, y2, keep, cx, cy, deg, factor } = {}) {
  const payload = { entityId }
  if (op === 'move' || op === 'copy') {
    const deltaX = fmtDelta(dx)
    const deltaY = fmtDelta(dy)
    if (deltaX === null || deltaY === null) return { refusal: `${op === 'copy' ? 'Copy' : 'Move'} refused: dx and dy must both be numbers.` }
    payload.dx = deltaX
    payload.dy = deltaY
  }
  // W4g-4: the reference's Modify verbs the crate carries. Each refuses
  // here with the operator-facing sentence; the engine validates again.
  if (op === 'mirror') {
    const [ax, ay, bx, by] = [x1, y1, x2, y2].map(fmtDelta)
    if ([ax, ay, bx, by].some((v) => v === null)) return { refusal: 'Mirror refused: x1, y1, x2 and y2 must all be numbers.' }
    if (ax === bx && ay === by) return { refusal: 'Mirror refused: the two points of the mirror line must differ.' }
    payload.x1 = ax
    payload.y1 = ay
    payload.x2 = bx
    payload.y2 = by
    payload.keep = keep === true || keep === 'true'
  }
  if (op === 'rotate' || op === 'scale') {
    const [bx, by] = [cx, cy].map(fmtDelta)
    if (bx === null || by === null) return { refusal: `${op === 'rotate' ? 'Rotate' : 'Scale'} refused: the base point x and y must both be numbers.` }
    payload.cx = bx
    payload.cy = by
    if (op === 'rotate') {
      const angle = fmtDelta(deg)
      if (angle === null) return { refusal: 'Rotate refused: the angle must be a number (degrees).' }
      payload.deg = angle
    } else {
      const f = fmtDelta(factor)
      if (f === null) return { refusal: 'Scale refused: the factor must be a number.' }
      if (f <= 0) return { refusal: 'Scale refused: the factor must be greater than 0.' }
      payload.factor = f
    }
  }
  if (op === 'moveVertex' || op === 'addVertex' || op === 'deleteVertex') {
    // W4f-9: digits only; parseInt read "3abc" as 3.
    const vi = /^\s*\d{1,9}\s*$/.test(String(vertexIndex ?? '')) ? Number.parseInt(vertexIndex, 10) : NaN
    if (!Number.isInteger(vi) || vi < 0) {
      return { refusal: `${op} refused: vertex must be a non-negative integer.` }
    }
    payload.vertexIndex = vi
    if (op === 'moveVertex') {
      const deltaX = fmtDelta(dx)
      const deltaY = fmtDelta(dy)
      if (deltaX === null || deltaY === null) return { refusal: 'Move vertex refused: dx and dy must both be numbers.' }
      payload.dx = deltaX
      payload.dy = deltaY
    }
    if (op === 'addVertex') {
      const x = fmtDelta(dx)
      const y = fmtDelta(dy)
      if (x === null || y === null) return { refusal: 'Add vertex refused: x and y must both be numbers.' }
      payload.x = x
      payload.y = y
    }
  }
  if (op === 'setLayer') {
    const trimmed = String(layer ?? '').trim()
    if (!trimmed) return { refusal: 'Set layer refused: enter a layer name.' }
    payload.layer = trimmed
  }
  return { payload }
}

/**
 * The store. ONE per engine session; CadEditSurface is its only consumer
 * today.
 *
 * @param createWorker REQUIRED factory for the engine worker. This module
 *        never names the worker path (license fence) — the caller supplies
 *        the one legal spawn.
 * @param saveTarget   `{ headVersion, save(bytes, parent, digest) }` when the
 *        host can persist a new version; null means download-only.
 * @param drawingId    the current drawing identity (DrawingIdentityProvider).
 *        A CHANGE resets the session: no engine state from one document may
 *        survive into another.
 */
export default function useEngineSession({
  createWorker,
  saveTarget = null,
  onSaved = null,
  drawingId = null,
} = {}) {
  if (typeof createWorker !== 'function') {
    throw new TypeError('useEngineSession requires a createWorker factory (the license fence keeps the worker path at the call site)')
  }

  const [session, setSession] = useState(INITIAL_SESSION)
  // Latest-value mirror, so an event handler can READ the session without a
  // side effect inside a state updater (which StrictMode would run twice).
  const sessionRef = useRef(session)
  sessionRef.current = session
  const boundaryRef = useRef(null)
  // Bumped by every reset, drawing switch and unmount. Async legs captured
  // before an await compare against it and abandon if it moved.
  const generationRef = useRef(0)
  const createWorkerRef = useRef(createWorker)
  createWorkerRef.current = createWorker
  const saveTargetRef = useRef(saveTarget)
  saveTargetRef.current = saveTarget
  const onSavedRef = useRef(onSaved)
  onSavedRef.current = onSaved
  // In-flight latch for the version write. See save().
  const savingRef = useRef(false)
  // W4f slice F: the undo machinery. `current` is the bytes the engine holds
  // right now (the opened file, then each applied edit's written bytes);
  // `undo`/`redo` hold {bytes, op}; `reload` names an undo/redo re-load in
  // flight so the documentLoaded that answers it is read as a step back or
  // forward, not as a fresh open.
  const historyRef = useRef({ original: null, current: null, undo: [], redo: [], reload: null })
  const clearHistory = () => {
    historyRef.current = { original: null, current: null, undo: [], redo: [], reload: null }
  }

  const patch = useCallback((next) => {
    setSession((current) => Object.freeze({ ...current, ...next }))
  }, [])

  const teardown = useCallback(() => {
    generationRef.current += 1
    boundaryRef.current?.terminate()
    boundaryRef.current = null
    clearHistory()
  }, [])

  // Worker death. Not an engine message — the boundary never sees this — so
  // it is handled here, where worker lifetime lives. The boundary is dropped
  // so the NEXT open spawns a fresh worker: that is the recovery.
  const onWorkerDied = useCallback(() => {
    teardown()
    setSession((current) => Object.freeze({
      ...INITIAL_SESSION,
      documentId: current.documentId,
      status: CRASH_STATUS,
      errorKind: SESSION_ERROR.CRASHED,
    }))
  }, [teardown])
  const onWorkerDiedRef = useRef(onWorkerDied)
  onWorkerDiedRef.current = onWorkerDied

  const ensureBoundary = useCallback(() => {
    if (boundaryRef.current) return boundaryRef.current
    const spawn = () => {
      const worker = createWorkerRef.current()
      // Death watch only — no engine payload is read here. See the license
      // fence note in this file's header.
      worker?.addEventListener?.('error', (event) => onWorkerDiedRef.current(event))
      worker?.addEventListener?.('messageerror', (event) => onWorkerDiedRef.current(event))
      return worker
    }
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: spawn })
    const generation = generationRef.current
    boundary.onMessage((message) => {
      // A message from a superseded session (a switch raced the worker's
      // reply) must never seat state over the current document.
      if (generation !== generationRef.current) return
      if (message.type === 'ready') return
      if (message.type === 'documentLoaded') {
        const entities = message.entities ?? NO_ENTITIES
        const others = (message.unsupported ?? []).length
        const history = historyRef.current
        const reload = history.reload
        if (reload) {
          // W4f slice F: this load answers an undo/redo. The engine now holds
          // the snapshot; the selection survives by id where it can; the
          // bytes count as edited unless they ARE the opened file.
          history.reload = null
          history.current = reload.bytes
          const edited = reload.bytes !== history.original
          setSession((current) => Object.freeze({
            ...current,
            entities,
            entityCount: message.entityCount ?? 0,
            selectedId: surviveSelection(current.selectedId, entities),
            savedBytes: edited ? reload.bytes : null,
            busy: false,
            engineParsed: true,
            geometrySource: GEOMETRY_SOURCE.ENGINE_REPARSE,
            errorKind: null,
            undoDepth: history.undo.length,
            redoDepth: history.redo.length,
            status: `${reload.kind === 'undo' ? 'Undid' : 'Redid'} ${reload.op}: `
              + `${message.entityCount} entities, ${reload.bytes.length} bytes.`,
          }))
          return
        }
        // A fresh open: the history starts here.
        history.undo = []
        history.redo = []
        history.current = history.original
        patch({
          entities,
          entityCount: message.entityCount ?? 0,
          selectedId: '',
          savedBytes: null,
          committedBytes: null,
          busy: false,
          engineParsed: true,
          geometrySource: GEOMETRY_SOURCE.ENGINE_PARSE,
          errorKind: null,
          undoDepth: 0,
          redoDepth: 0,
          status: `Loaded ${message.documentId}: ${message.entityCount} entities`
            + (others ? ` (${others} preserved as read-only kinds).` : '.'),
        })
        return
      }
      if (message.type === 'editApplied') {
        if (!message.ok) {
          patch({
            busy: false,
            errorKind: SESSION_ERROR.REFUSED,
            status: `Edit refused (${message.op}): ${message.reason ?? 'unknown reason'}`,
          })
          return
        }
        const entities = message.entities ?? NO_ENTITIES
        // A create reports what it drew BY ID (the worker found it again by
        // handle in the re-parse); the selection lands on it. A create whose
        // entity the writer dropped is a defect and reads as one.
        const isCreate = CREATE_OPS.includes(message.op)
          || (CREATING_EDITS.includes(message.op) && Object.prototype.hasOwnProperty.call(message, 'createdId'))
        const createdId = isCreate && message.createdId !== null && message.createdId !== undefined
          ? String(message.createdId)
          : ''
        const createLost = isCreate && !createdId
        const reparsed = `Re-parsed from the written bytes: ${message.entityCount} entities, ${message.byteLength} bytes.`
        // W4f slice F: the state BEFORE this edit is a snapshot; a new edit
        // ends any redo branch. Done once here (outside the updater, which
        // StrictMode runs twice).
        const history = historyRef.current
        if (message.bytes && history.current) {
          history.undo.push({ bytes: history.current, op: message.op })
          trimSnapshots(history.undo)
          history.redo = []
        }
        if (message.bytes) history.current = message.bytes
        setSession((current) => Object.freeze({
          ...current,
          busy: false,
          entities,
          entityCount: message.entityCount ?? 0,
          savedBytes: message.bytes ?? null,
          selectedId: createdId || surviveSelection(current.selectedId, entities),
          undoDepth: history.undo.length,
          redoDepth: history.redo.length,
          engineParsed: true,
          // The written bytes, read back: what a reader of the saved file
          // would actually see, never an optimistic prediction.
          geometrySource: GEOMETRY_SOURCE.ENGINE_REPARSE,
          errorKind: createLost ? SESSION_ERROR.REFUSED : null,
          status: createLost
            ? `${message.op} applied, but the new entity was not found after re-parse. ${reparsed}`
            : createdId
              ? `${message.op} applied: entity ${createdId} drawn. ${reparsed}`
              : `${message.op} applied. ${reparsed}`,
        }))
        return
      }
      if (message.type === 'error') {
        // W4f slice F: a refused undo/redo re-load (the engine could not
        // parse the snapshot) leaves the engine holding NO document (the
        // worker drops it on a failed load), so the history is moot: clear
        // it, count zero, and say which step failed. Leaving `reload` set
        // would silently kill every later step (kimi, #970).
        const failedStep = historyRef.current.reload
        clearHistory()
        patch({
          busy: false,
          entities: NO_ENTITIES,
          entityCount: 0,
          selectedId: '',
          savedBytes: null,
          committedBytes: null,
          // Nothing passed through the engine: no engine-truth readout is owed.
          engineParsed: false,
          geometrySource: null,
          errorKind: SESSION_ERROR.ENGINE,
          undoDepth: 0,
          redoDepth: 0,
          status: failedStep
            ? `${failedStep.kind === 'undo' ? 'Undo' : 'Redo'} of ${failedStep.op} failed: the engine refused the snapshot (${message.message}). Open the drawing again.`
            : `Engine refused: ${message.message}`,
        })
      }
    })
    boundary.start()
    boundary.post({ type: 'init' })
    boundaryRef.current = boundary
    return boundary
  }, [patch])

  // W4g-1b: the ONE load path. `open(file)` reads a chosen file into it;
  // `openBytes(bytes, name)` is what the head opener calls with bytes it
  // already holds (no File object, no second read). Same size ceiling, same
  // history floor, same boundary message. Fails closed on any other shape.
  const openBytes = useCallback((bytes, name) => {
    // toString, not instanceof: bytes from another realm (a test harness, a
    // worker) are still bytes.
    if (Object.prototype.toString.call(bytes) !== '[object Uint8Array]' || typeof name !== 'string' || !name) return
    if (bytes.length > MAX_DOCUMENT_BYTES) {
      patch({
        errorKind: SESSION_ERROR.LIMIT,
        status: `Refused ${name}: ${bytes.length} bytes exceeds the ${MAX_DOCUMENT_BYTES}-byte limit.`,
      })
      return
    }
    patch({ busy: true, documentId: name, errorKind: null, status: `Opening ${name}...` })
    const boundary = ensureBoundary()
    // W4f slice F: the opened bytes are the floor of the undo history.
    clearHistory()
    historyRef.current.original = bytes
    if (!boundary.post({ type: 'loadDocument', documentId: name, bytes })) {
      patch({
        busy: false,
        errorKind: SESSION_ERROR.TRANSPORT,
        status: `Could not send ${name} to the engine.`,
      })
    }
  }, [ensureBoundary, patch])

  const open = useCallback(async (file) => {
    if (!file) return
    if (file.size > MAX_DOCUMENT_BYTES) {
      patch({
        errorKind: SESSION_ERROR.LIMIT,
        status: `Refused ${file.name}: ${file.size} bytes exceeds the ${MAX_DOCUMENT_BYTES}-byte limit.`,
      })
      return
    }
    const generation = generationRef.current
    patch({ busy: true, documentId: file.name, errorKind: null, status: `Reading ${file.name}...` })
    let bytes
    try {
      bytes = new Uint8Array(await file.arrayBuffer())
    } catch {
      if (generation !== generationRef.current) return
      patch({ busy: false, errorKind: SESSION_ERROR.READ, status: `Could not read ${file.name}.` })
      return
    }
    // The read outlived this session (drawing switch, reset, unmount): drop
    // the bytes rather than post a document nobody asked for any more.
    if (generation !== generationRef.current) return
    openBytes(bytes, file.name)
  }, [openBytes, patch])

  const select = useCallback((entityId) => {
    setSession((current) => (current.selectedId === entityId
      ? current
      : Object.freeze({ ...current, selectedId: entityId })))
  }, [])

  const applyEdit = useCallback((op, inputs) => {
    // Nothing selected is not an error, it is a no-op: the affordances that
    // dispatch an edit are disabled until something is.
    if (!sessionRef.current.selectedId) return
    const { payload, refusal } = buildEditPayload(op, sessionRef.current.selectedId, inputs)
    if (refusal) {
      patch({ errorKind: SESSION_ERROR.REFUSED, status: refusal })
      return
    }
    const boundary = boundaryRef.current
    if (!boundary) {
      patch({ errorKind: SESSION_ERROR.TRANSPORT, status: 'Edit refused: no document is open.' })
      return
    }
    patch({ busy: true, errorKind: null })
    if (!boundary.post({ type: 'applyEdit', op, payload })) {
      patch({
        busy: false,
        errorKind: SESSION_ERROR.TRANSPORT,
        status: `Edit refused (${op}): the boundary rejected the message.`,
      })
    }
  }, [patch])

  // W4d Draw group: creation needs an open, engine-parsed document and no
  // selection. Refused here for malformed input (a sentence, no round trip),
  // refused as TRANSPORT when nothing is open.
  const create = useCallback((op, inputs) => {
    if (!CREATE_OPS.includes(op)) {
      patch({ errorKind: SESSION_ERROR.REFUSED, status: `Draw refused: unknown operation ${op}.` })
      return
    }
    const { payload, refusal } = buildCreatePayload(op, inputs)
    if (refusal) {
      patch({ errorKind: SESSION_ERROR.REFUSED, status: refusal })
      return
    }
    const boundary = boundaryRef.current
    if (!boundary || !sessionRef.current.engineParsed) {
      patch({ errorKind: SESSION_ERROR.TRANSPORT, status: 'Draw refused: no document is open.' })
      return
    }
    patch({ busy: true, errorKind: null })
    if (!boundary.post({ type: 'applyEdit', op: WORKER_OP[op] || op, payload })) {
      patch({
        busy: false,
        errorKind: SESSION_ERROR.TRANSPORT,
        status: `Draw refused (${op}): the boundary rejected the message.`,
      })
    }
  }, [patch])

  // The persistence leg: post the EXACT edited bytes with a client-computed
  // digest; the server recomputes, parses, and compare-and-sets against the
  // head. A 409 (head moved) reads back as a plain instruction to refresh.
  const save = useCallback(async () => {
    const target = saveTargetRef.current
    const bytes = sessionRef.current.savedBytes
    // `busy` alone cannot gate this: two clicks in one tick both read the
    // pre-render value. A version write is not an operation to do twice, so
    // the in-flight latch is a ref, set before the first await.
    if (!bytes || !target || sessionRef.current.busy || savingRef.current) return null
    savingRef.current = true
    patch({ busy: true, errorKind: null, status: 'Saving to the project as a new version...' })
    const generation = generationRef.current
    try {
      const digest = await sha256Hex(bytes)
      const receipt = await target.save(bytes, target.headVersion, digest)
      // A save that outlived its document must not report a version onto a
      // session that has since switched drawings.
      if (generation !== generationRef.current) return null
      const nv = receipt?.new_version?.version ?? receipt?.head
      patch({
        busy: false,
        receipt,
        savedVersion: nv ?? null,
        committedBytes: bytes,
        errorKind: null,
        status: `Saved as version ${nv} (parent ${receipt?.new_version?.parent}), `
          + `digest ${String(receipt?.source_sha256 || digest).slice(0, 12)}…, `
          + `engine cost $${receipt?.cost?.engine_usd ?? 0}.`,
      })
      onSavedRef.current?.(receipt)
      return receipt
    } catch (error) {
      if (generation !== generationRef.current) return null
      patch({
        busy: false,
        errorKind: SESSION_ERROR.SAVE,
        status: error?.status === 409
          ? `Save refused: ${error.message}`
          : `Save failed: ${error?.message || error}`,
      })
      return null
    } finally {
      savingRef.current = false
    }
  }, [patch])

  // W4f slice F: step back (or forward) by re-loading the neighbouring
  // snapshot through the open path. A no-op while busy or with nothing to
  // step to; the documentLoaded reply is read by the `reload` marker.
  const step = useCallback((kind) => {
    const history = historyRef.current
    const from = kind === 'undo' ? history.undo : history.redo
    const to = kind === 'undo' ? history.redo : history.undo
    const boundary = boundaryRef.current
    if (sessionRef.current.busy || history.reload || !boundary || !from.length || !history.current) return false
    const snap = from.pop()
    to.push({ bytes: history.current, op: snap.op })
    trimSnapshots(to)
    history.reload = { kind, op: snap.op, bytes: snap.bytes }
    patch({ busy: true, errorKind: null, undoDepth: history.undo.length, redoDepth: history.redo.length })
    if (!boundary.post({ type: 'loadDocument', documentId: sessionRef.current.documentId, bytes: snap.bytes })) {
      // Put the snapshot back: nothing moved.
      history.reload = null
      to.pop()
      from.push(snap)
      patch({
        busy: false,
        errorKind: SESSION_ERROR.TRANSPORT,
        undoDepth: history.undo.length,
        redoDepth: history.redo.length,
        status: `${kind === 'undo' ? 'Undo' : 'Redo'} refused: the boundary rejected the message.`,
      })
      return false
    }
    return true
  }, [patch])
  const undo = useCallback(() => step('undo'), [step])
  const redo = useCallback(() => step('redo'), [step])

  const reset = useCallback(() => {
    teardown()
    setSession(INITIAL_SESSION)
  }, [teardown])

  // Drawing switch mid-edit (ACCEPTANCE, required state): the session resets
  // and the worker is torn down, so no entity list, selection or byte buffer
  // from one document can bleed into another. The FIRST observation is the
  // session learning which drawing it is on, not a switch.
  const previousDrawingRef = useRef(undefined)
  useEffect(() => {
    const previous = previousDrawingRef.current
    previousDrawingRef.current = drawingId
    if (previous === undefined || previous === drawingId) return
    reset()
  }, [drawingId, reset])

  useEffect(() => () => { teardown() }, [teardown])

  const actions = useMemo(() => ({
    open, openBytes, select, applyEdit, create, save, reset, undo, redo,
  }), [applyEdit, create, open, openBytes, redo, reset, save, select, undo])

  const selected = useMemo(
    () => session.entities.find((entity) => entity.id === session.selectedId) || null,
    [session.entities, session.selectedId],
  )

  return {
    ...session,
    selected,
    // A crash is the one error a retry can clear on its own: the next open
    // spawns a fresh worker.
    recoverable: session.errorKind === SESSION_ERROR.CRASHED,
    reparsed: session.geometrySource === GEOMETRY_SOURCE.ENGINE_REPARSE,
    // W4g-1b: edited since the last save (or since the open, when nothing
    // was saved). By reference: an undo back to the committed snapshot is
    // clean, a new edit after a save is dirty.
    dirty: session.savedBytes !== null && session.savedBytes !== session.committedBytes,
    actions,
  }
}
