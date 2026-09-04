// Card F-3: the BROWSER module worker for the real CAD engine — the
// worker-side half CadEditSurface talks to through the unmodified
// EngineBoundary. Speaks exactly the boundary's message vocabulary
// (init -> ready, loadDocument -> documentLoaded, applyEdit -> editApplied,
// dispose, anything unhealthy -> error) and NEVER throws out of the loop.
//
// This file lives inside the license fence's allowed vendor prefix, and a
// page may reference it only through the one legal spawn shape
// (docs/CAD-ENGINE-LICENSE-FENCE.md deny rule 3):
//   new Worker(new URL('.../worker-browser.mjs', import.meta.url), { type: 'module' })
//
// Engine loading: the compiled wasm is served as crate-name-free assets at
// /engine/engine.js + /engine/engine_bg.wasm, staged from the wasm-pack
// --target web output by scripts/stage_cad_engine.mjs (web/ must stay clean
// of the crate name for the fence, so the assets are renamed on stage). The
// import is deliberately non-analyzable (@vite-ignore on a computed string)
// so a build without the artifact still bundles; a runtime miss becomes a
// TYPED engine_unavailable error message, never a half-broken surface.
//
// Contract mirrored from the JS document worker it supersedes on this path:
// one bounded document per worker; a refused edit NEVER half-mutates state
// (every wrapper mutation validates before writing); every successful edit
// re-serializes and REPARSES so the UI always renders what a reader of the
// written bytes would actually see.

const MAX_DOCUMENT_BYTES = 16 * 1024 * 1024

const ENGINE_BASE = '/engine'

let enginePromise = null
function loadEngine() {
  if (enginePromise) return enginePromise
  enginePromise = (async () => {
    const glueUrl = `${ENGINE_BASE}/engine.js`
    let mod
    try {
      mod = await import(/* @vite-ignore */ glueUrl)
    } catch (cause) {
      const reason = new Error(
        'engine_unavailable: the compiled CAD engine is not staged on this '
        + 'deployment (scripts/stage_cad_engine.mjs after the documented '
        + 'wasm-pack --target web build)')
      reason.cause = cause
      throw reason
    }
    await mod.default(`${ENGINE_BASE}/engine_bg.wasm`)
    return mod
  })()
  // A failed load must not poison every later attempt with a stale promise.
  enginePromise.catch(() => { enginePromise = null })
  return enginePromise
}

// The single held document. Replaced by loadDocument, dropped by dispose.
let current = null

function isByteArray(value) {
  return value instanceof Uint8Array
    || (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView(value))
}

const MAX_U64 = 18446744073709551615n

// DXF handles are u64 values. wasm exports them as decimal strings because a
// JavaScript number loses identity above 2^53. Safe integers remain accepted
// for the old JS stand-in; an unsafe numeric handle is refused, never rounded.
function handleId(value, invalidReason = 'bad_entity_handle') {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) throw new Error('handle_precision_lost')
    if (value <= 0) throw new Error(invalidReason)
    return String(value)
  }
  if (typeof value !== 'string') throw new Error(invalidReason)
  const raw = value.trim()
  if (!/^\d+$/.test(raw)) throw new Error(invalidReason)
  const canonical = raw.replace(/^0+/, '') || '0'
  if (canonical.length > 20) throw new Error(invalidReason)
  const parsed = BigInt(canonical)
  if (parsed <= 0n || parsed > MAX_U64) throw new Error(invalidReason)
  return parsed.toString()
}

function projectEntities(doc) {
  // The wrapper's editableEntities() returns plain JSON-compatible objects.
  // The id the surface keys on is the engine HANDLE: the one identity that
  // survives a write/re-parse. It used to be the document-order index, and
  // a delete renumbers: after deleting index 0 the survivor became id 0 and
  // the selection "survived" onto a different entity (found by the W4d e2e
  // row on the real stack). Edits still address the wrapper by index, which
  // the worker resolves from the handle at dispatch time (entityIndex).
  return doc.editableEntities().map((entity) => {
    const handle = handleId(entity.handle)
    return {
      id: handle,
      handle,
      index: entity.index,
      type: entity.type,
      layer: entity.layer,
      closed: entity.closed,
      editable: entity.editable,
      vertices: entity.vertices,
      // W4f: drawable fields for CIRCLE/ARC (null for every other kind), so
      // the viewer can show the engine document; older wrappers without
      // them read as null too.
      radius: entity.radius ?? null,
      startDeg: entity.startDeg ?? null,
      endDeg: entity.endDeg ?? null,
    }
  })
}

function loadedResponse(documentId, doc) {
  const entities = projectEntities(doc)
  return {
    type: 'documentLoaded',
    documentId,
    entityCount: entities.length,
    entities,
    // The whole-document engine reads and rewrites EVERYTHING, so there is
    // no lossy-write refusal class: writable is unconditionally true and
    // per-entity `editable` says which rows the edit ops accept.
    writable: true,
    refusal: null,
    unsupported: entities.filter((e) => !e.editable).map((e) => e.type),
  }
}

function refused(op, reason) {
  return { type: 'editApplied', op, ok: false, reason }
}

// The wrapper addresses entities by CURRENT document-order index; the
// surface addresses them by handle. Resolve at dispatch time against the
// held document, so a renumbering edit can never redirect the next edit onto
// a neighbour. Bounded by the entity count; null for anything unknown.
function entityIndex(doc, payload) {
  const raw = payload?.entityId
  if (raw === undefined || raw === null || raw === '') return null
  const wanted = handleId(raw, 'bad_entity_id')
  const hit = doc.editableEntities().find((entity) => handleId(entity.handle) === wanted)
  return hit && Number.isInteger(hit.index) && hit.index >= 0 ? hit.index : null
}

// W4d Draw group: a create op takes no entityId; each maps to ONE wrapper
// method that validates before it writes and returns the new entity's
// handle. An engine that lacks the method (the JS stand-in) is refused
// EXPLICITLY, as a typed reason — never a TypeError read as a crash, never a
// pretend success.
const CREATE_OPS = Object.freeze({
  createLine: (doc, p) => doc.createLine(Number(p.x1), Number(p.y1), Number(p.x2), Number(p.y2), String(p.layer ?? '')),
  createCircle: (doc, p) => doc.createCircle(Number(p.cx), Number(p.cy), Number(p.radius), String(p.layer ?? '')),
  createArc: (doc, p) => doc.createArc(
    Number(p.cx), Number(p.cy), Number(p.radius), Number(p.startDeg), Number(p.endDeg), String(p.layer ?? '')),
  createPolyline: (doc, p) => doc.createPolyline(
    Float64Array.from(Array.isArray(p.points) ? p.points : []), Boolean(p.closed), String(p.layer ?? '')),
})

async function applyEdit(engine, message) {
  const { op, payload } = message
  if (!current) return refused(op, 'no_document_loaded')
  const doc = current.doc
  let createdHandle = null
  // Every op that makes MORE than one entity (explode's parts, an
  // array's copies) reports them here, in document order.
  let createdHandles = null
  const create = CREATE_OPS[op]
  if (create) {
    if (typeof doc[op] !== 'function') return refused(op, `engine_lacks_create:${op}`)
    try {
      createdHandle = handleId(
        create(doc, payload && typeof payload === 'object' ? payload : {}),
        'create_returned_no_handle',
      )
    } catch (error) {
      // The wrapper validates before it writes: a refusal here means the
      // document was NOT mutated. Surface the typed reason string as-is.
      current = null
      return refused(op, error instanceof Error ? error.message : String(error))
    }
  } else {
    let index
    try {
      index = entityIndex(doc, payload)
    } catch (error) {
      return refused(op, error instanceof Error ? error.message : String(error))
    }
    if (index === null) return refused(op, 'bad_entity_id')
    try {
      if (op === 'delete') doc.deleteEntity(index)
      else if (op === 'move') doc.translateEntity(index, Number(payload.dx), Number(payload.dy))
      else if (op === 'moveVertex') doc.moveVertex(index, Number(payload.vertexIndex), Number(payload.dx), Number(payload.dy))
      else if (op === 'addVertex') doc.addVertexAfter(index, Number(payload.vertexIndex), Number(payload.x), Number(payload.y))
      else if (op === 'deleteVertex') doc.deleteVertex(index, Number(payload.vertexIndex))
      else if (op === 'setLayer') doc.setEntityLayer(index, String(payload.layer ?? ''))
      // W4g-4: the reference's Modify verbs the crate carries. COPY,
      // MIRROR-with-source and EXPLODE create: their new handle(s) ride the
      // same createdId leg as the Draw group so the selection lands on what
      // was made. Each wrapper refuses before it writes.
      else if (op === 'copy') {
        if (typeof doc.copyEntity !== 'function') return refused(op, `engine_lacks_op:${op}`)
        createdHandle = handleId(doc.copyEntity(index, Number(payload.dx), Number(payload.dy)), 'create_returned_no_handle')
      } else if (op === 'mirror') {
        if (typeof doc.mirrorEntity !== 'function') return refused(op, `engine_lacks_op:${op}`)
        const keep = payload.keep === true
        const made = doc.mirrorEntity(index, Number(payload.x1), Number(payload.y1), Number(payload.x2), Number(payload.y2), keep)
        if (keep) createdHandle = handleId(made, 'create_returned_no_handle')
      } else if (op === 'rotate') {
        if (typeof doc.rotateEntity !== 'function') return refused(op, `engine_lacks_op:${op}`)
        doc.rotateEntity(index, Number(payload.cx), Number(payload.cy), Number(payload.deg))
      } else if (op === 'scale') {
        if (typeof doc.scaleEntity !== 'function') return refused(op, `engine_lacks_op:${op}`)
        doc.scaleEntity(index, Number(payload.cx), Number(payload.cy), Number(payload.factor))
      } else if (op === 'explode') {
        if (typeof doc.explodeEntity !== 'function') return refused(op, `engine_lacks_op:${op}`)
        const parts = doc.explodeEntity(index)
        if (!Array.isArray(parts) || parts.length === 0) return refused(op, 'explode_returned_no_parts')
        createdHandle = handleId(parts[0], 'create_returned_no_handle')
        createdHandles = parts.map((h) => handleId(h, 'create_returned_no_handle'))
      } else if (op === 'arrayRect' || op === 'arrayPolar') {
        // ONE engine op for the whole array: every applied edit re-parses the
        // document and hands the bytes back, so N client-side copies would
        // cost N round trips and N undo steps. The wrapper bounds the count
        // and refuses before it writes.
        const fn = op === 'arrayRect' ? 'arrayRectEntity' : 'arrayPolarEntity'
        if (typeof doc[fn] !== 'function') return refused(op, `engine_lacks_op:${op}`)
        const made = op === 'arrayRect'
          ? doc.arrayRectEntity(index, Number(payload.rows), Number(payload.cols), Number(payload.rowGap), Number(payload.colGap))
          : doc.arrayPolarEntity(index, Number(payload.count), Number(payload.cx), Number(payload.cy), Number(payload.totalDeg))
        if (!Array.isArray(made) || made.length === 0) return refused(op, 'array_returned_no_copies')
        createdHandle = handleId(made[0], 'create_returned_no_handle')
        createdHandles = made.map((h) => handleId(h, 'create_returned_no_handle'))
      } else return refused(op, `unknown_op:${op}`)
    } catch (error) {
      // The wrapper validates before it writes, so a refusal here means the
      // document was NOT mutated. Surface the typed reason string as-is.
      return refused(op, error instanceof Error ? error.message : String(error))
    }
  }

  // Write-back leg: serialize, reparse, report from the REPARSE — the UI
  // renders what the written bytes actually say.
  const written = engine.writeDxf(doc)
  const reparsed = engine.parseDxf(written)
  let entities
  try {
    entities = projectEntities(reparsed)
  } catch (error) {
    current = null
    return refused(op, error instanceof Error ? error.message : String(error))
  }
  current = { documentId: current.documentId, doc: reparsed }
  const reply = {
    type: 'editApplied',
    op,
    ok: true,
    entityCount: entities.length,
    entities,
    bytes: written,
    byteLength: written.length,
  }
  if (createdHandle !== null || createdHandles !== null) {
    // Found again BY HANDLE in the re-parse (an index is not an identity),
    // through ONE index of the entity list: a find per created handle is
    // quadratic on an array of a thousand copies. null in a slot means the
    // writer dropped that entity, which the caller treats as a defect, never
    // as success.
    const byHandle = new Map(entities.map((e) => [e.handle, e.id]))
    if (createdHandle !== null) reply.createdId = byHandle.get(createdHandle) ?? null
    if (createdHandles !== null) reply.createdIds = createdHandles.map((h) => byHandle.get(h) ?? null)
  }
  return reply
}

/** Handles one boundary message; returns the reply or null. Never throws. */
export async function handleMessage(raw, engineOverride = null) {
  if (!raw || typeof raw !== 'object') return { type: 'error', message: 'bad_message' }

  if (raw.type === 'init') {
    try {
      engineOverride ?? await loadEngine()
      return { type: 'ready' }
    } catch (error) {
      return { type: 'error', message: error instanceof Error ? error.message : String(error) }
    }
  }

  if (raw.type === 'dispose') {
    current = null
    return null
  }

  let engine
  try {
    engine = engineOverride ?? await loadEngine()
  } catch (error) {
    return { type: 'error', message: error instanceof Error ? error.message : String(error) }
  }

  if (raw.type === 'loadDocument') {
    const { documentId, bytes } = raw
    if (typeof documentId !== 'string' || documentId === '') {
      return { type: 'error', message: 'bad_document_id' }
    }
    if (!isByteArray(bytes)) {
      current = null
      return { type: 'error', message: 'bad_document_bytes' }
    }
    if (bytes.length > MAX_DOCUMENT_BYTES) {
      current = null
      return { type: 'error', message: `document_too_large:${bytes.length}` }
    }
    let doc
    try {
      doc = engine.parseDxf(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes.buffer))
    } catch (error) {
      current = null
      return { type: 'error', message: `parse_failed:${error instanceof Error ? error.message : String(error)}` }
    }
    current = { documentId, doc }
    try {
      return loadedResponse(documentId, doc)
    } catch (error) {
      current = null
      return { type: 'error', message: error instanceof Error ? error.message : String(error) }
    }
  }

  if (raw.type === 'applyEdit') {
    return applyEdit(engine, raw)
  }

  return { type: 'error', message: `unhandled_type:${raw.type}` }
}

// Worker-scope install, guarded exactly like the JS document worker: only a
// real dedicated worker (no window, self.postMessage present) attaches.
if (
  typeof window === 'undefined'
  && typeof self !== 'undefined'
  && typeof self.postMessage === 'function'
) {
  self.addEventListener('message', async (event) => {
    const response = await handleMessage(event.data)
    if (response) self.postMessage(response)
  })
}
