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
import { offsetEntity } from './offset.js'
import { MAX_BATCH_STEPS, MAX_INTERSECT_POINTS, chamferLines, extendEntity, filletLines, trimEntity } from './intersect.js'
import { clipboardRecord, describeRecord, pasteOp } from './clipboard.js'
import { diffPlan } from './mutationDiff.js'

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
  // W4g-3b: the entity list the HEAD document loaded with (null for a hand
  // import, which has no head to diff against). A save posts the diff of the
  // current list against it as the mutation plan; a successful save moves it
  // to the list just saved. The snapshot stack never touches it: an undo back
  // to the loaded state diffs to nothing.
  committedEntities: null,
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
  // W4g-5c: the session clipboard, ONE frozen record of copied geometry.
  // Not bytes and not a live entity: an entity would go stale the moment
  // the document is edited, since the list is replaced wholesale on every
  // apply. Survives a document switch on purpose, the way every clipboard
  // a drafter already uses does; cleared only by reset.
  clipboard: null,
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
export const CREATE_OPS = Object.freeze(['createLine', 'createCircle', 'createArc', 'createPolyline', 'createRectangle', 'createText', 'createPoint', 'createEllipse'])
// W4g-4: edits that MAKE an entity (a displaced copy, a mirrored copy, the
// segments of an explode) report what they made by id like the Draw group
// does; the selection lands on it.
/// The most copies one ARRAY may add. The engine carries the same number,
/// so a count the prompt refuses could not have reached the document either.
/// The most characters one TEXT may carry; the engine carries the same number.
export const MAX_TEXT_CHARS = 1024

export const MAX_ARRAY_COPIES = 1000

export const CREATING_EDITS = Object.freeze(['copy', 'mirror', 'explode', 'arrayRect', 'arrayPolar', 'batch'])
// W4g-6: the verbs whose geometry is planned in the browser (intersect.js)
// and applied by the engine as ONE batch. `edge` names the second entity
// the prompt asks for; `point` names the point on the selection.
export const INTERSECT_VERBS = Object.freeze({
  trim: { name: 'Trim', edge: 'cutting edge', point: 'the point on the part to remove:' },
  extend: { name: 'Extend', edge: 'boundary edge', point: 'the point near the end to extend:' },
  fillet: { name: 'Fillet', edge: 'second object', point: 'the point on the first line:' },
  chamfer: { name: 'Chamfer', edge: 'second line', point: 'the point on the first line:' },
})
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
export function buildCreatePayload(op, { x, y, x2, y2, r, a0, a1, pts, closed, layer, height, rot, text, ratio } = {}) {
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
  // W4g-5d: TEXT. The same bounds the engine enforces, read here first so a
  // bad value refuses on the prompt with a sentence and never round-trips: a
  // DXF group value is one line, so a control character (a pasted newline,
  // a tab) is refused rather than split into a broken record.
  if (op === 'createText') {
    const [px, py] = [x, y].map(fmtDelta)
    if (px === null || py === null) return { refusal: 'Text refused: x and y must both be numbers.' }
    const h = fmtDelta(height)
    if (h === null) return { refusal: 'Text refused: the height must be a number.' }
    if (h <= 0) return { refusal: 'Text refused: the height must be greater than 0.' }
    const angle = fmtDelta(rot)
    if (angle === null) return { refusal: 'Text refused: the rotation must be a number (degrees).' }
    const value = String(text ?? '').replace(/[\r\n]+$/, '')
    if (!value.trim()) return { refusal: 'Text refused: enter the text to place.' }
    if ([...value].length > MAX_TEXT_CHARS) return { refusal: `Text refused: at most ${MAX_TEXT_CHARS} characters.` }
    // eslint-disable-next-line no-control-regex
    // C1 controls too (U+0080..U+009F, a NEL from a paste): the crate's
    // char::is_control covers the whole Cc category, and the prompt must
    // refuse everything the engine would, never the reverse (kimi on #1028).
    if (/[\u0000-\u001f\u007f-\u009f]/.test(value)) return { refusal: 'Text refused: one line only, with no control characters.' }
    return { payload: { x: px, y: py, height: h, rotationDeg: angle, text: value, layer: layerName } }
  }
  if (op === 'createPoint') {
    // W4g-4b POINT: one location.
    const [px, py] = [x, y].map(fmtDelta)
    if (px === null || py === null) return { refusal: 'Point refused: x and y must both be numbers.' }
    return { payload: { x: px, y: py, layer: layerName } }
  }
  if (op === 'createEllipse') {
    // W4g-4b ELLIPSE: the centre, the axis ENDPOINT (absolute, as picked) and
    // the minor-to-major ratio in (0, 1]; the engine takes the axis relative
    // to the centre, so the difference is sent, never the endpoint.
    const [cx, cy, ex, ey] = [x, y, x2, y2].map(fmtDelta)
    if ([cx, cy, ex, ey].some((v) => v === null)) return { refusal: 'Ellipse refused: the centre x, y and the axis endpoint x2, y2 must all be numbers.' }
    if (cx === ex && cy === ey) return { refusal: 'Ellipse refused: the axis endpoint must differ from the centre.' }
    const k = fmtDelta(ratio)
    if (k === null) return { refusal: 'Ellipse refused: the ratio must be a number.' }
    if (k <= 0 || k > 1) return { refusal: 'Ellipse refused: the ratio (minor to major) must be greater than 0 and at most 1.' }
    return { payload: { cx, cy, ax: ex - cx, ay: ey - cy, ratio: k, layer: layerName } }
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
 * W4g-4b MATCHPROP: ONE setLayer step on the destination with the source's
 * layer, from the session's own entity list; `{ steps }` or `{ refusal }`.
 * A destination already on that layer is a refusal (nothing would change),
 * a read-only one too. Pure so a row can drive it without a worker.
 */
export function planMatchprop(session, inputs = {}) {
  const { entities, selectedId } = session
  const checked = buildEditPayload('matchprop', selectedId, inputs)
  if (checked.refusal) return { refusal: checked.refusal }
  const source = (entities || []).find((candidate) => candidate.id === selectedId)
  if (!source) return { refusal: 'Match refused: the selected entity is no longer in the document.' }
  const target = (entities || []).find((candidate) => candidate.id === checked.payload.edge)
  if (!target) return { refusal: 'Match refused: the destination object is no longer in the document.' }
  if (target.editable === false) return { refusal: 'Match refused: the destination object is read-only in the browser engine.' }
  const layer = String(source.layer ?? '').trim()
  if (!layer) return { refusal: 'Match refused: the selection has no layer to copy.' }
  if (String(target.layer ?? '') === layer) return { refusal: `Match refused: the destination is already on layer ${layer}.` }
  return { steps: [{ op: 'setLayer', entityId: target.id, layer }] }
}

/**
 * W4g-6: the batch an intersection verb lowers to, from the session's own
 * entity list and the prompt's operands: `{ steps }` in intersect.js's terms
 * or `{ refusal }`. Pure so a row can drive it without a worker.
 */
export function planIntersectVerb(op, session, inputs = {}) {
  const verb = INTERSECT_VERBS[op]
  if (!verb) return { refusal: `Edit refused: unknown operation ${op}.` }
  const { entities, selectedId } = session
  const checked = buildEditPayload(op, selectedId, inputs)
  if (checked.refusal) return { refusal: checked.refusal }
  const target = (entities || []).find((candidate) => candidate.id === selectedId)
  if (!target) return { refusal: `${verb.name} refused: the selected entity is no longer in the document.` }
  const edge = (entities || []).find((candidate) => candidate.id === checked.payload.edge)
  if (!edge) return { refusal: `${verb.name} refused: the ${verb.edge} is no longer in the document.` }
  // The typed path reaches exactly the candidates a canvas pick can name: a
  // read-only entity is never one (FILLET and CHAMFER rewrite the edge).
  if (edge.editable === false) return { refusal: `${verb.name} refused: the ${verb.edge} is read-only in the browser engine.` }
  const { x, y } = checked.payload
  if (op === 'trim') return trimEntity(target, edge, x, y)
  if (op === 'extend') return extendEntity(target, edge, x, y)
  if (op === 'fillet') return filletLines(target, edge, fmtDelta(inputs.r), x, y, fmtDelta(inputs.ex), fmtDelta(inputs.ey))
  return chamferLines(target, edge, fmtDelta(inputs.d1), fmtDelta(inputs.d2), x, y, fmtDelta(inputs.ex), fmtDelta(inputs.ey))
}

/**
 * W4g-6: intersect.js's steps lowered to the worker's payloads, every one
 * validated by the SAME builders a single op goes through (a create through
 * buildCreatePayload; a geometry replacement bounded here). `{ steps }` of
 * `{ op, payload }`, or `{ refusal }` naming the first bad step.
 */
export function lowerSteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) return { refusal: 'Edit refused: the plan has no steps.' }
  if (steps.length > MAX_BATCH_STEPS) return { refusal: `Edit refused: the plan has more than ${MAX_BATCH_STEPS} steps.` }
  const lowered = []
  for (const step of steps) {
    const op = String(step?.op ?? '')
    if (CREATE_OPS.includes(op)) {
      const { payload, refusal } = buildCreatePayload(op, step.inputs || {})
      if (refusal) return { refusal }
      lowered.push({ op, payload })
      continue
    }
    const entityId = String(step?.entityId ?? '')
    if (!entityId) return { refusal: `Edit refused: step ${op} names no entity.` }
    if (op === 'delete') {
      lowered.push({ op, payload: { entityId } })
    } else if (op === 'setLayer') {
      // W4g-4b: MATCHPROP's one step, the same shape the single op posts.
      const layer = String(step.layer ?? '').trim()
      if (!layer) return { refusal: 'Edit refused: a layer step names no layer.' }
      lowered.push({ op, payload: { entityId, layer } })
    } else if (op === 'setVertices') {
      // A trim keeps at most the entity's own points plus its two cut points,
      // so the bound is the kernel's, plus two, not the create bound.
      const pts = Array.isArray(step.points) ? step.points : null
      if (!pts || pts.length < 2 || pts.length > MAX_INTERSECT_POINTS + 2) return { refusal: `Edit refused: a geometry step needs 2 to ${MAX_INTERSECT_POINTS + 2} points.` }
      const flat = []
      for (const pt of pts) {
        if (!Array.isArray(pt) || !Number.isFinite(pt[0]) || !Number.isFinite(pt[1])) return { refusal: 'Edit refused: a geometry step has a point that is not a number.' }
        flat.push(pt[0], pt[1])
      }
      // W4g-6d: a polyline step may carry one bulge per point (a corner
      // fillet's arc, and every bulge the polyline already had); absent or
      // empty means every segment straight. Anything else is refused here,
      // before the engine sees it.
      const bulges = step.bulges == null ? [] : step.bulges
      if (!Array.isArray(bulges) || (bulges.length !== 0 && bulges.length !== pts.length) || bulges.some((b) => !Number.isFinite(b))) {
        return { refusal: 'Edit refused: a geometry step needs one bulge per point, or none.' }
      }
      // Straight plans keep the wire shape they had: `bulges` rides only when one is set.
      lowered.push({ op, payload: { entityId, points: flat, closed: step.closed === true, ...(bulges.some((b) => b !== 0) ? { bulges } : {}) } })
    } else if (op === 'setArc') {
      const [cx, cy, radius, startDeg, endDeg] = [step.x, step.y, step.r, step.a0, step.a1].map(fmtDelta)
      if ([cx, cy, radius, startDeg, endDeg].some((v) => v === null)) return { refusal: 'Edit refused: an arc step has a value that is not a number.' }
      if (radius <= 0) return { refusal: 'Edit refused: an arc step needs a radius greater than 0.' }
      if ((endDeg - startDeg) % 360 === 0) return { refusal: 'Edit refused: an arc step needs a start and end that differ.' }
      lowered.push({ op, payload: { entityId, cx, cy, radius, startDeg, endDeg } })
    } else return { refusal: `Edit refused: unknown step ${op}.` }
  }
  return { steps: lowered }
}

/**
 * Edit-input validation, refused here rather than at the engine. Returns
 * either `{ payload }` or `{ refusal }` with the exact operator-facing
 * sentence — never both, never a throw.
 */
export function buildEditPayload(op, entityId, { dx, dy, vertexIndex, layer, x1, y1, x2, y2, keep, cx, cy, deg, factor, rows, cols, rowGap, colGap, count, totalDeg, edge, ex, ey, x, y, r, d1, d2 } = {}) {
  const payload = { entityId }
  // W4g-6: the intersection verbs validate their OPERANDS here, so the
  // prompt holds Run with the sentence as the drafter types; whether the
  // geometry works out (a crossing, a corner) is intersect.js's answer at
  // run time, as OFFSET's is. The payload is never posted as-is: the run
  // path plans a batch from the two entities.
  if (op === 'matchprop') {
    // W4g-4b MATCHPROP: the selection is the source, the pick names the
    // destination; the layer is copied (the reference copies colour,
    // linetype and lineweight too, which wait on the contract).
    const edgeId = String(edge ?? '').trim()
    if (!edgeId) return { refusal: 'Match refused: select the destination object by clicking it on the drawing.' }
    if (edgeId === String(entityId ?? '')) return { refusal: 'Match refused: the destination must be a different entity from the selection.' }
    return { payload: { ...payload, edge: edgeId } }
  }
  if (INTERSECT_VERBS[op]) {
    const verb = INTERSECT_VERBS[op]
    const edgeId = String(edge ?? '').trim()
    if (!edgeId) return { refusal: `${verb.name} refused: select the ${verb.edge} by clicking it on the drawing.` }
    // W4g-6d: FILLET / CHAMFER may name the selection itself as the second
    // object when it is a polyline (its own corner); the kernel decides by
    // kind. TRIM / EXTEND never cut an entity against itself.
    if (edgeId === String(entityId ?? '') && op !== 'fillet' && op !== 'chamfer') return { refusal: `${verb.name} refused: the ${verb.edge} must be a different entity from the selection.` }
    const px = fmtDelta(x)
    const py = fmtDelta(y)
    if (px === null || py === null) return { refusal: `${verb.name} refused: ${verb.point} x and y must both be numbers.` }
    if (op === 'fillet') {
      const radius = fmtDelta(r)
      if (radius === null) return { refusal: 'Fillet refused: the radius must be a number.' }
      if (radius < 0) return { refusal: 'Fillet refused: the radius must be 0 or more.' }
    }
    if (op === 'chamfer') {
      const first = fmtDelta(d1)
      const second = fmtDelta(d2)
      if (first === null || second === null) return { refusal: 'Chamfer refused: both distances must be numbers.' }
      if (first < 0 || second < 0) return { refusal: 'Chamfer refused: both distances must be 0 or more.' }
    }
    if (op === 'fillet' || op === 'chamfer') {
      const qx = fmtDelta(ex)
      const qy = fmtDelta(ey)
      if (qx === null || qy === null) return { refusal: `${verb.name} refused: the point on the ${verb.edge} (edge x, edge y) must both be numbers.` }
    }
    return { payload: { ...payload, edge: edgeId, x: px, y: py } }
  }
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
  // W4g-5b: ARRAY is ONE engine op, never N copies, because every applied
  // edit re-parses the whole document. The counts are read the strict way
  // (whole numbers only) and bounded here as well as in the engine, so a
  // typo refuses on the prompt instead of asking the engine for a million
  // entities.
  if (op === 'arrayRect' || op === 'arrayPolar') {
    const whole = (value) => (/^\s*\d{1,7}\s*$/.test(String(value ?? '')) ? Number.parseInt(value, 10) : NaN)
    if (op === 'arrayRect') {
      const r = whole(rows)
      const c = whole(cols)
      if (!Number.isInteger(r) || !Number.isInteger(c)) return { refusal: 'Array refused: rows and columns must be whole numbers.' }
      if (r < 1 || c < 1) return { refusal: 'Array refused: rows and columns must be at least 1.' }
      if (r * c - 1 < 1) return { refusal: 'Array refused: 1 row by 1 column is the source alone, so there is nothing to copy.' }
      if (r * c - 1 > MAX_ARRAY_COPIES) return { refusal: `Array refused: that is more than ${MAX_ARRAY_COPIES} copies.` }
      const rg = fmtDelta(rowGap)
      const cg = fmtDelta(colGap)
      if (rg === null || cg === null) return { refusal: 'Array refused: the row and column spacing must both be numbers.' }
      if (rg === 0 && cg === 0) return { refusal: 'Array refused: with no spacing every copy lands on the source.' }
      payload.rows = r
      payload.cols = c
      payload.rowGap = rg
      payload.colGap = cg
    } else {
      const n = whole(count)
      if (!Number.isInteger(n)) return { refusal: 'Polar array refused: the count must be a whole number.' }
      if (n < 2) return { refusal: 'Polar array refused: the count includes the source, so it must be at least 2.' }
      if (n - 1 > MAX_ARRAY_COPIES) return { refusal: `Polar array refused: that is more than ${MAX_ARRAY_COPIES} copies.` }
      const bx = fmtDelta(cx)
      const by = fmtDelta(cy)
      if (bx === null || by === null) return { refusal: 'Polar array refused: the centre x and y must both be numbers.' }
      const sweep = fmtDelta(totalDeg)
      if (sweep === null) return { refusal: 'Polar array refused: the angle to fill must be a number (degrees).' }
      if (sweep === 0) return { refusal: 'Polar array refused: an angle of 0 puts every copy on the source.' }
      // Past one turn the sweep wraps and copies land back on the source
      // (3 items over 720 degrees puts both on the original). The engine
      // refuses it too; this is the sentence a drafter reads.
      if (Math.abs(sweep) > 360) return { refusal: 'Polar array refused: the angle to fill cannot be more than one full turn.' }
      payload.count = n
      payload.cx = bx
      payload.cy = by
      payload.totalDeg = sweep
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
  // W4g-3b: whether the load in flight is the head (see openBytes).
  const committedLoadRef = useRef(false)
  const onSavedRef = useRef(onSaved)
  onSavedRef.current = onSaved
  // In-flight latch for the version write. See save().
  const savingRef = useRef(false)
  // W4g-6: the verb behind an in-flight batch, so its reply and its undo
  // step read under the verb's name rather than `batch`.
  const batchVerbRef = useRef(null)
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
          committedEntities: committedLoadRef.current ? entities : null,
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
        // W4g-6: a batch answers as `batch`; the verb that posted it is the
        // name the drafter sees (and the undo stack keeps).
        const label = message.op === 'batch' ? (batchVerbRef.current || 'batch') : message.op
        if (message.op === 'batch') batchVerbRef.current = null
        if (!message.ok) {
          patch({
            busy: false,
            errorKind: SESSION_ERROR.REFUSED,
            status: `Edit refused (${label}): ${message.reason ?? 'unknown reason'}`,
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
          history.undo.push({ bytes: history.current, op: label })
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
            ? `${label} applied, but the new entity was not found after re-parse. ${reparsed}`
            : createdId
              ? `${label} applied: entity ${createdId} drawn. ${reparsed}`
              : `${label} applied. ${reparsed}`,
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
          committedEntities: null,
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
  const openBytes = useCallback((bytes, name, opts = null) => {
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
    // W4g-3b: `{ committed: true }` says these bytes ARE the head (the
    // opener's call), so the entity list this load produces becomes the base
    // a save diffs against. Any other shape (a hand import) keeps no base.
    committedLoadRef.current = !!(opts && typeof opts === 'object' && opts.committed === true)
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

  const applyEdit = useCallback((op, inputs) => {
    // Nothing selected is not an error, it is a no-op: the affordances that
    // dispatch an edit are disabled until something is.
    if (!sessionRef.current.selectedId) return
    // W4g-5 OFFSET: a parallel copy is a CREATE whose geometry comes from the
    // selection, the distance and the side clicked, computed here (offset.js)
    // and drawn by the engine's own create op. One round trip, and every
    // refusal is the geometry's own sentence.
    if (op === 'offset') {
      const { entities, selectedId } = sessionRef.current
      const entity = entities.find((candidate) => candidate.id === selectedId)
      if (!entity) {
        patch({ errorKind: SESSION_ERROR.REFUSED, status: 'Offset refused: the selected entity is no longer in the document.' })
        return
      }
      const px = readNumber(inputs?.x)
      const py = readNumber(inputs?.y)
      if (px === null || py === null) {
        patch({ errorKind: SESSION_ERROR.REFUSED, status: 'Offset refused: the side point x and y must both be numbers.' })
        return
      }
      const answer = offsetEntity(entity, readNumber(inputs?.dist), px, py)
      if (answer.refusal) {
        patch({ errorKind: SESSION_ERROR.REFUSED, status: answer.refusal })
        return
      }
      create(answer.op, answer.inputs)
      return
    }
    // W4g-6 TRIM / EXTEND / FILLET / CHAMFER: the geometry is planned here
    // (intersect.js) from the selection and the second entity, and the
    // engine applies the plan as ONE batch: one round trip, one undo step,
    // all of it or none of it.
    if (INTERSECT_VERBS[op] || op === 'matchprop') {
      const planned = op === 'matchprop' ? planMatchprop(sessionRef.current, inputs) : planIntersectVerb(op, sessionRef.current, inputs)
      if (planned.refusal) {
        patch({ errorKind: SESSION_ERROR.REFUSED, status: planned.refusal })
        return
      }
      const lowered = lowerSteps(planned.steps)
      if (lowered.refusal) {
        patch({ errorKind: SESSION_ERROR.REFUSED, status: lowered.refusal })
        return
      }
      const boundary = boundaryRef.current
      if (!boundary) {
        patch({ errorKind: SESSION_ERROR.TRANSPORT, status: 'Edit refused: no document is open.' })
        return
      }
      batchVerbRef.current = op
      patch({ busy: true, errorKind: null })
      if (!boundary.post({ type: 'applyEdit', op: 'batch', payload: { verb: op, steps: lowered.steps } })) {
        batchVerbRef.current = null
        patch({
          busy: false,
          errorKind: SESSION_ERROR.TRANSPORT,
          status: `Edit refused (${op}): the boundary rejected the message.`,
        })
      }
      return
    }
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
  }, [create, patch])

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
    // W4g-3b: the edit as a plan, when the engine holds the head. A diff the
    // contract cannot carry (a kind change, past the operation bound) sends
    // NO plan and names why in the status; the server then takes the DXF
    // sidecar leg and says so in its receipt. A hand import has nothing to
    // diff against and never sends one.
    const { committedEntities, entities } = sessionRef.current
    let plan = null
    let planNote = ''
    if (committedEntities) {
      const diff = diffPlan(committedEntities, entities)
      if (diff.mutations) plan = { mutations: diff.mutations, count: diff.count }
      else planNote = ` No plan sent: ${diff.reason}.`
    }
    try {
      const digest = await sha256Hex(bytes)
      const receipt = await target.save(bytes, target.headVersion, digest, plan)
      // A save that outlived its document must not report a version onto a
      // session that has since switched drawings.
      if (generation !== generationRef.current) return null
      const nv = receipt?.new_version?.version ?? receipt?.head
      const commit = receipt?.commit ? ` through the ${receipt.commit} leg` : ''
      patch({
        busy: false,
        receipt,
        savedVersion: nv ?? null,
        committedBytes: bytes,
        committedEntities: committedEntities ? sessionRef.current.entities : null,
        errorKind: null,
        status: `Saved as version ${nv} (parent ${receipt?.new_version?.parent})${commit}, `
          + `digest ${String(receipt?.source_sha256 || digest).slice(0, 12)}…, `
          + `engine cost $${receipt?.cost?.engine_usd ?? 0}.${planNote}`,
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

  // W4g-5c CUT / COPY / PASTE. No engine op: a copy is a record of the
  // selection's own geometry, and a paste is one create at a base point
  // through the same path the Draw group uses.
  const copyToClipboard = useCallback((cut = false) => {
    const { entities, selectedId } = sessionRef.current
    const entity = entities.find((candidate) => candidate.id === selectedId)
    const verb = cut ? 'Cut' : 'Copy'
    if (!entity) {
      patch({ errorKind: SESSION_ERROR.REFUSED, status: `${verb} refused: select an entity first.` })
      return
    }
    // The record is taken BEFORE anything is deleted, so a cut that cannot be
    // put on the clipboard leaves the drawing exactly as it was.
    const { record, refusal } = clipboardRecord(entity, verb)
    if (refusal) {
      patch({ errorKind: SESSION_ERROR.REFUSED, status: refusal })
      return
    }
    patch({ clipboard: record, errorKind: null, status: `${verb}: ${describeRecord(record)} is on the clipboard.` })
    if (cut) applyEdit('delete', {})
  }, [applyEdit, patch])

  const pasteFromClipboard = useCallback((inputs) => {
    const record = sessionRef.current.clipboard
    const answer = pasteOp(record, readNumber(inputs?.x), readNumber(inputs?.y))
    if (answer.refusal) {
      patch({ errorKind: SESSION_ERROR.REFUSED, status: answer.refusal })
      return
    }
    create(answer.op, answer.inputs)
  }, [create, patch])

  const actions = useMemo(() => ({
    open, openBytes, select, applyEdit, create, save, reset, undo, redo,
    copyToClipboard, pasteFromClipboard,
  }), [applyEdit, copyToClipboard, create, open, openBytes, pasteFromClipboard, redo, reset, save, select, undo])

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
