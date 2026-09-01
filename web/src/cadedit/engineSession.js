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
export const SESSION_ERROR = Object.freeze({
  REFUSED: 'refused',     // the engine refused one edit; the document stands
  ENGINE: 'engine',       // the engine refused the document itself
  TRANSPORT: 'transport', // the boundary would not carry the message
  READ: 'read',           // the browser could not read the chosen file
  LIMIT: 'limit',         // the file is over the byte cap
  SAVE: 'save',           // the version write failed or was refused
  CRASHED: 'crashed',     // the worker died under us
})

const NO_ENTITIES = Object.freeze([])

const INITIAL_SESSION = Object.freeze({
  documentId: '',
  entities: NO_ENTITIES,
  entityCount: 0,
  selectedId: '',
  status: '',
  savedBytes: null,
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
})

const CRASH_STATUS = 'Engine stopped unexpectedly. Open a drawing again to restart it.'

function fmtDelta(raw) {
  const n = Number.parseFloat(raw)
  return Number.isFinite(n) ? n : null
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

/**
 * Edit-input validation, refused here rather than at the engine. Returns
 * either `{ payload }` or `{ refusal }` with the exact operator-facing
 * sentence — never both, never a throw.
 */
export function buildEditPayload(op, entityId, { dx, dy, vertexIndex, layer } = {}) {
  const payload = { entityId }
  if (op === 'move') {
    const deltaX = fmtDelta(dx)
    const deltaY = fmtDelta(dy)
    if (deltaX === null || deltaY === null) return { refusal: 'Move refused: dx and dy must both be numbers.' }
    payload.dx = deltaX
    payload.dy = deltaY
  }
  if (op === 'moveVertex' || op === 'addVertex' || op === 'deleteVertex') {
    const vi = Number.parseInt(vertexIndex, 10)
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

  const patch = useCallback((next) => {
    setSession((current) => Object.freeze({ ...current, ...next }))
  }, [])

  const teardown = useCallback(() => {
    generationRef.current += 1
    boundaryRef.current?.terminate()
    boundaryRef.current = null
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
        patch({
          entities,
          entityCount: message.entityCount ?? 0,
          selectedId: '',
          savedBytes: null,
          busy: false,
          engineParsed: true,
          geometrySource: GEOMETRY_SOURCE.ENGINE_PARSE,
          errorKind: null,
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
        setSession((current) => Object.freeze({
          ...current,
          busy: false,
          entities,
          entityCount: message.entityCount ?? 0,
          savedBytes: message.bytes ?? null,
          selectedId: surviveSelection(current.selectedId, entities),
          engineParsed: true,
          // The written bytes, read back: what a reader of the saved file
          // would actually see, never an optimistic prediction.
          geometrySource: GEOMETRY_SOURCE.ENGINE_REPARSE,
          errorKind: null,
          status: `${message.op} applied. Re-parsed from the written bytes: `
            + `${message.entityCount} entities, ${message.byteLength} bytes.`,
        }))
        return
      }
      if (message.type === 'error') {
        patch({
          busy: false,
          entities: NO_ENTITIES,
          entityCount: 0,
          selectedId: '',
          savedBytes: null,
          // Nothing passed through the engine: no engine-truth readout is owed.
          engineParsed: false,
          geometrySource: null,
          errorKind: SESSION_ERROR.ENGINE,
          status: `Engine refused: ${message.message}`,
        })
      }
    })
    boundary.start()
    boundary.post({ type: 'init' })
    boundaryRef.current = boundary
    return boundary
  }, [patch])

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
    const boundary = ensureBoundary()
    if (!boundary.post({ type: 'loadDocument', documentId: file.name, bytes })) {
      patch({
        busy: false,
        errorKind: SESSION_ERROR.TRANSPORT,
        status: `Could not send ${file.name} to the engine.`,
      })
    }
  }, [ensureBoundary, patch])

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
    open, select, applyEdit, save, reset,
  }), [applyEdit, open, reset, save, select])

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
    actions,
  }
}
