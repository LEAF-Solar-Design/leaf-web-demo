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

function projectDocument(doc) {
  // The wrapper's editableEntities() returns plain JSON-compatible objects.
  // The id the surface keys on is the engine HANDLE: the one identity that
  // survives a write/re-parse. It used to be the document-order index, and
  // a delete renumbers: after deleting index 0 the survivor became id 0 and
  // the selection "survived" onto a different entity (found by the W4d e2e
  // row on the real stack). Edits still address the wrapper by index, which
  // the worker resolves from the handle at dispatch time (entityIndex).
  const projection = doc.editableEntities()
  const entities = projection.map((entity) => {
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
      // W4g-5d: a TEXT's own value, height and rotation (degrees); null
      // for every other kind. The server's intake keeps none of these, so
      // this projection is where the browser reads them back.
      text: entity.text ?? null,
      height: entity.height ?? null,
      rotationDeg: entity.rotationDeg ?? null,
      // W4g-7b-01c: read-only INSERT references retain their own transform.
      kind: entity.kind,
      name: entity.name,
      ip: entity.ip,
      scale: entity.scale,
      columns: entity.columns,
      rows: entity.rows,
      columnSpacing: entity.columnSpacing,
      rowSpacing: entity.rowSpacing,
      // W4g-6d: one bulge per vertex for a polyline, null for every other kind.
      bulges: Array.isArray(entity.bulges) ? entity.bulges : null,
      // W4g-4b: an ELLIPSE's axis endpoint (relative to the centre) and ratio.
      majorAxis: Array.isArray(entity.majorAxis) ? entity.majorAxis : null,
      ratio: entity.ratio ?? null,
      startDeg: entity.startDeg ?? null,
      endDeg: entity.endDeg ?? null,
    }
  })
  return { entities, blocks: projection.blocks ?? [] }
}

function loadedResponse(documentId, doc) {
  const { entities, blocks } = projectDocument(doc)
  return {
    type: 'documentLoaded',
    documentId,
    entityCount: entities.length,
    entities,
    blocks,
    blockBasePatched: doc.blockBasePatched ?? false,
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
// W4g-6e: a bulge list at the boundary is absent (every segment straight), an
// Array or a typed array (one number per vertex; the crate checks the count and
// finiteness, and a hole or a non-number becomes NaN and is refused), never
// anything else: a value of another shape is refused, not read as straight.
function bulgeList(raw) {
  if (raw == null) return new Float64Array(0)
  if (Array.isArray(raw)) return Float64Array.from(raw, (v) => (typeof v === 'number' ? v : NaN))
  if (ArrayBuffer.isView(raw) && !(raw instanceof DataView)) return Float64Array.from(raw)
  throw new Error('bulges_not_a_list')
}

const CREATE_OPS = Object.freeze({
  createLine: (doc, p) => doc.createLine(Number(p.x1), Number(p.y1), Number(p.x2), Number(p.y2), String(p.layer ?? '')),
  createCircle: (doc, p) => doc.createCircle(Number(p.cx), Number(p.cy), Number(p.radius), String(p.layer ?? '')),
  createArc: (doc, p) => doc.createArc(
    Number(p.cx), Number(p.cy), Number(p.radius), Number(p.startDeg), Number(p.endDeg), String(p.layer ?? '')),
  createPolyline: (doc, p) => doc.createPolyline(
    Float64Array.from(Array.isArray(p.points) ? p.points : []), Boolean(p.closed), String(p.layer ?? ''), bulgeList(p.bulges)),
  // W4g-5d: TEXT. The wrapper refuses a non-finite number, a height that
  // is not positive, an empty or over-long value and any control character.
  createText: (doc, p) => doc.createText(
    Number(p.x), Number(p.y), Number(p.height), Number(p.rotationDeg), String(p.text ?? ''), String(p.layer ?? '')),
  // W4g-4b: POINT and ELLIPSE (the axis endpoint RELATIVE to the centre, the
  // minor-to-major ratio); the wrapper refuses before it writes.
  createPoint: (doc, p) => doc.createPoint(Number(p.x), Number(p.y), String(p.layer ?? '')),
  createEllipse: (doc, p) => doc.createEllipse(
    Number(p.cx), Number(p.cy), Number(p.ax), Number(p.ay), Number(p.ratio), String(p.layer ?? '')),
})
// The op string off the boundary is looked up in a Map of the table's OWN
// entries, never as a computed property: a prototype name such as
// `constructor` finds nothing, and no call is ever made through a name the
// boundary chose (CodeQL js/unvalidated-dynamic-method-call).
const CREATE_TABLE = new Map(Object.entries(CREATE_OPS))

// W4g-6: the most steps one `batch` carries. FILLET and CHAMFER cut two
// entities and create one; a TRIM that splits keeps one and creates one.
// Mirrored by the store's MAX_BATCH_STEPS (intersect.js).
const MAX_BATCH_STEPS = 4

/**
 * One op against the held document. Returns { createdHandle, createdHandles }
 * (null when the op made nothing) or THROWS the typed reason. Every wrapper
 * validates before it writes, so a throw means the document is untouched.
 * Shared by the single-op path and the batch, so the two can never drift.
 */
function applyOne(doc, op, payload) {
  const p = payload && typeof payload === 'object' ? payload : {}
  const create = CREATE_TABLE.get(op)
  if (typeof create === 'function') {
    if (typeof doc[op] !== 'function') throw new Error(`engine_lacks_create:${op}`)
    return { createdHandle: handleId(create(doc, p), 'create_returned_no_handle'), createdHandles: null }
  }
  const index = entityIndex(doc, p)
  if (index === null) throw new Error('bad_entity_id')
  const need = (fn) => { if (typeof doc[fn] !== 'function') throw new Error(`engine_lacks_op:${op}`) }
  let createdHandle = null
  let createdHandles = null
  if (op === 'delete') doc.deleteEntity(index)
  else if (op === 'move') doc.translateEntity(index, Number(p.dx), Number(p.dy))
  else if (op === 'moveVertex') doc.moveVertex(index, Number(p.vertexIndex), Number(p.dx), Number(p.dy))
  else if (op === 'addVertex') doc.addVertexAfter(index, Number(p.vertexIndex), Number(p.x), Number(p.y))
  else if (op === 'deleteVertex') doc.deleteVertex(index, Number(p.vertexIndex))
  else if (op === 'setLayer') doc.setEntityLayer(index, String(p.layer ?? ''))
  // W4g-4: the reference's Modify verbs the crate carries. COPY,
  // MIRROR-with-source and EXPLODE create: their new handle(s) ride the
  // same createdId leg as the Draw group so the selection lands on what
  // was made. Each wrapper refuses before it writes.
  else if (op === 'copy') {
    need('copyEntity')
    createdHandle = handleId(doc.copyEntity(index, Number(p.dx), Number(p.dy)), 'create_returned_no_handle')
  } else if (op === 'mirror') {
    need('mirrorEntity')
    const keep = p.keep === true
    const made = doc.mirrorEntity(index, Number(p.x1), Number(p.y1), Number(p.x2), Number(p.y2), keep)
    if (keep) createdHandle = handleId(made, 'create_returned_no_handle')
  } else if (op === 'rotate') {
    need('rotateEntity')
    doc.rotateEntity(index, Number(p.cx), Number(p.cy), Number(p.deg))
  } else if (op === 'scale') {
    need('scaleEntity')
    doc.scaleEntity(index, Number(p.cx), Number(p.cy), Number(p.factor))
  } else if (op === 'explode') {
    need('explodeEntity')
    const parts = doc.explodeEntity(index)
    if (!Array.isArray(parts) || parts.length === 0) throw new Error('explode_returned_no_parts')
    createdHandle = handleId(parts[0], 'create_returned_no_handle')
    createdHandles = parts.map((h) => handleId(h, 'create_returned_no_handle'))
  } else if (op === 'arrayRect' || op === 'arrayPolar') {
    // ONE engine op for the whole array: every applied edit re-parses the
    // document and hands the bytes back, so N client-side copies would
    // cost N round trips and N undo steps. The wrapper bounds the count
    // and refuses before it writes.
    need(op === 'arrayRect' ? 'arrayRectEntity' : 'arrayPolarEntity')
    const made = op === 'arrayRect'
      ? doc.arrayRectEntity(index, Number(p.rows), Number(p.cols), Number(p.rowGap), Number(p.colGap))
      : doc.arrayPolarEntity(index, Number(p.count), Number(p.cx), Number(p.cy), Number(p.totalDeg))
    if (!Array.isArray(made) || made.length === 0) throw new Error('array_returned_no_copies')
    createdHandle = handleId(made[0], 'create_returned_no_handle')
    createdHandles = made.map((h) => handleId(h, 'create_returned_no_handle'))
  } else if (op === 'setVertices') {
    // W4g-6: an entity's own geometry replaced (a LINE's two points, a
    // polyline's list and closed flag); the wrapper bounds and refuses first.
    need('setVertices')
    // W4g-6d: `bulges` is empty (every segment straight) or one per point;
    // the wrapper refuses a list of the wrong length or a non-finite value.
    doc.setVertices(index, Float64Array.from(Array.isArray(p.points) ? p.points : []), Boolean(p.closed), Float64Array.from(Array.isArray(p.bulges) ? p.bulges : []))
  } else if (op === 'setArc') {
    need('setArc')
    doc.setArc(index, Number(p.cx), Number(p.cy), Number(p.radius), Number(p.startDeg), Number(p.endDeg))
  } else throw new Error(`unknown_op:${op}`)
  return { createdHandle, createdHandles }
}

async function applyEdit(engine, message) {
  const { op, payload } = message
  if (!current) return refused(op, 'no_document_loaded')
  const doc = current.doc
  let createdHandle = null
  // Every op that makes MORE than one entity (explode's parts, an array's
  // copies, a batch's creates) reports them here, in document order.
  let createdHandles = null
  if (op === 'batch') {
    // W4g-6: one verb, several steps, ONE turn. The steps run in order
    // against the held document, each addressed by handle (so a delete
    // inside the batch cannot skew a later step); the bytes before the
    // first step are the snapshot a refusal restores. So a batch is atomic,
    // all of it or none, and costs exactly one write-back (one undo step)
    // either way.
    const steps = Array.isArray(payload?.steps) ? payload.steps : null
    if (!steps || steps.length === 0) return refused(op, 'batch_empty')
    if (steps.length > MAX_BATCH_STEPS) return refused(op, 'batch_too_many_steps')
    const snapshot = engine.writeDxf(doc)
    const restore = () => { current = { documentId: current.documentId, doc: reparseDocument(engine, snapshot, doc) } }
    const made = []
    for (let i = 0; i < steps.length; i += 1) {
      const step = steps[i] && typeof steps[i] === 'object' ? steps[i] : {}
      const stepOp = String(step.op ?? '')
      if (stepOp === 'batch') { restore(); return refused(op, `step_${i}_batch_nested`) }
      try {
        const r = applyOne(doc, stepOp, step.payload)
        if (r.createdHandle !== null) made.push(r.createdHandle)
        if (r.createdHandles !== null) for (const h of r.createdHandles) made.push(h)
      } catch (error) {
        restore()
        return refused(op, `step_${i}_${stepOp}:${error instanceof Error ? error.message : String(error)}`)
      }
    }
    // The selection lands on the LAST entity a batch made (a fillet's arc,
    // a chamfer's line, a split's far part); every one rides createdIds.
    if (made.length) { createdHandle = made[made.length - 1]; createdHandles = made }
  } else {
    try {
      const r = applyOne(doc, op, payload)
      createdHandle = r.createdHandle
      createdHandles = r.createdHandles
    } catch (error) {
      // Every wrapper validates before it writes, so a refusal here means the
      // document was NOT mutated and stays held (a refused create used to
      // drop it, which left the store believing in a document the worker no
      // longer had). Surface the typed reason string as-is.
      return refused(op, error instanceof Error ? error.message : String(error))
    }
  }
  // Write-back leg: serialize, reparse, report from the REPARSE — the UI
  // renders what the written bytes actually say.
  const written = engine.writeDxf(doc)
  const blockBasePatched = doc.blockBasePatched ?? false
  const reparsed = reparseDocument(engine, written, doc)
  let projection
  try {
    projection = projectDocument(reparsed)
  } catch (error) {
    current = null
    return refused(op, error instanceof Error ? error.message : String(error))
  }
  const { entities, blocks } = projection
  current = { documentId: current.documentId, doc: reparsed }
  const reply = {
    type: 'editApplied',
    op,
    ok: true,
    entityCount: entities.length,
    entities,
    blocks,
    blockBasePatched,
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

function reparseDocument(engine, bytes, previous) {
  const doc = engine.parseDxf(bytes)
  // A write cannot recover a binary base or an unmatched definition marker.
  if (previous.blockBasesUnknown === true) doc.blockBasesUnknown = true
  if (typeof doc.inheritBlockBaseUnknowns === 'function') doc.inheritBlockBaseUnknowns(previous)
  return doc
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
      if (raw.blockBasesUnknown === true) doc.blockBasesUnknown = true
    } catch (error) {
      current = null
      const reason = error instanceof Error ? error.message : String(error)
      if (reason.startsWith('block names collide case-insensitively: ') || reason.startsWith('block definitions collapsed on load: ')) {
        return { type: 'documentLoaded', documentId, entityCount: 0, entities: [], blocks: [],
          blockBasePatched: false, writable: false, refusal: reason, unsupported: [] }
      }
      return { type: 'error', message: `parse_failed:${reason}` }
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
