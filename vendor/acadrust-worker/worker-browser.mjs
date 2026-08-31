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

function projectEntities(doc) {
  // The wrapper's editableEntities() already returns plain JSON-compatible
  // objects; re-key `index` as the string id the surface's list keys on.
  return doc.editableEntities().map((entity) => ({
    id: String(entity.index),
    type: entity.type,
    layer: entity.layer,
    closed: entity.closed,
    editable: entity.editable,
    vertices: entity.vertices,
  }))
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

function entityIndex(payload) {
  const raw = payload?.entityId
  const index = Number.parseInt(String(raw), 10)
  return Number.isInteger(index) && index >= 0 ? index : null
}

async function applyEdit(engine, message) {
  const { op, payload } = message
  if (!current) return refused(op, 'no_document_loaded')
  const index = entityIndex(payload)
  if (index === null) return refused(op, 'bad_entity_id')
  const doc = current.doc
  try {
    if (op === 'delete') doc.deleteEntity(index)
    else if (op === 'move') doc.translateEntity(index, Number(payload.dx), Number(payload.dy))
    else if (op === 'moveVertex') doc.moveVertex(index, Number(payload.vertexIndex), Number(payload.dx), Number(payload.dy))
    else if (op === 'addVertex') doc.addVertexAfter(index, Number(payload.vertexIndex), Number(payload.x), Number(payload.y))
    else if (op === 'deleteVertex') doc.deleteVertex(index, Number(payload.vertexIndex))
    else if (op === 'setLayer') doc.setEntityLayer(index, String(payload.layer ?? ''))
    else return refused(op, `unknown_op:${op}`)
  } catch (error) {
    // The wrapper validates before it writes, so a refusal here means the
    // document was NOT mutated. Surface the typed reason string as-is.
    return refused(op, error instanceof Error ? error.message : String(error))
  }

  // Write-back leg: serialize, reparse, report from the REPARSE — the UI
  // renders what the written bytes actually say.
  const written = engine.writeDxf(doc)
  const reparsed = engine.parseDxf(written)
  const entities = projectEntities(reparsed)
  current = { documentId: current.documentId, doc: reparsed }
  return {
    type: 'editApplied',
    op,
    ok: true,
    entityCount: entities.length,
    entities,
    bytes: written,
    byteLength: written.length,
  }
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
    return loadedResponse(documentId, doc)
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
